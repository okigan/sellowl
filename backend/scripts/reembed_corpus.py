"""Re-embed comps whose vectors came from a different embedding space.

Run from backend/:  uv run python scripts/reembed_corpus.py

If the embedding endpoint is unreachable mid-run the app falls back to a
lexical embedder, and anything indexed during that window is stored in the
wrong vector space. Those comps are not comparable to later queries -- they
score like noise and get rejected as irrelevant, which looks exactly like
bad matching.

Idempotent: only rows whose dimension differs from the current embedder are
touched.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, "src")

from sellowl.config import Settings
from sellowl.index import doc_to_comp
from sellowl.jobs import make_embedder
from sellowl.sqlite_store import SqliteCompStore

BATCH = 200


async def main() -> None:
    settings = Settings()
    embedder = make_embedder(settings)
    probe = await embedder.embed_query("dimension probe")
    if embedder.relevance_floor != 0.65 and len(probe) == 256:
        raise SystemExit(
            "The embedding endpoint is unreachable, so this would re-embed "
            "everything into the lexical space -- exactly the problem it is "
            "meant to fix. Bring the endpoint up and re-run."
        )
    want = len(probe)
    print(f"current embedder produces {want}-dim vectors")

    path = Path(settings.sqlite_db_path)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    rows = [
        r
        for r in conn.execute("SELECT doc_id, source, vector FROM comps")
        if len(json.loads(r["vector"])) != want
    ]
    conn.close()
    if not rows:
        print("nothing to repair: every comp is in the current space")
        return
    print(f"re-embedding {len(rows)} comps indexed in another space...")

    store = SqliteCompStore(path, embedder=embedder)
    await store.ensure_indices()
    for i in range(0, len(rows), BATCH):
        comps = [doc_to_comp(json.loads(r["source"]), 0.0) for r in rows[i : i + BATCH]]
        await store.upsert_comps(comps)
        print(f"  {min(i + BATCH, len(rows))}/{len(rows)}", end="\r", flush=True)
    await store.close()
    print(f"\nrepaired {len(rows)} comps")


asyncio.run(main())
