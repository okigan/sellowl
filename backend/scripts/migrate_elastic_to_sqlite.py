"""Backfill the SQLite comp store from the Elasticsearch index.

Run from backend/:  uv run python scripts/migrate_elastic_to_sqlite.py

The Elastic index accumulated comps across every job ever run; a fresh SQLite
file starts empty. That difference is not cosmetic -- it is most of why
matching looked worse after the switch. A thin corpus means the right comps
simply aren't there to retrieve, and (before the relevance floor) the pricing
stage would confidently price whatever least-wrong junk came back instead.

Idempotent: comps upsert by `doc_id`, so re-running adds only what's new.
Embeddings are computed locally as part of the upsert, which is the slow part
-- the SQLite store owns its vectors rather than relying on Elastic's
`semantic_text` managed inference.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, "src")

from elasticsearch import AsyncElasticsearch

from sellowl.config import Settings
from sellowl.index import doc_to_comp
from sellowl.jobs import make_embedder
from sellowl.sqlite_store import SqliteCompStore

PAGE = 500


async def main() -> None:
    settings = Settings()
    if not settings.elastic_configured:
        raise SystemExit("ELASTICSEARCH_* not set; nothing to migrate from.")

    es = AsyncElasticsearch(settings.elasticsearch_endpoint, api_key=settings.elasticsearch_api_key)
    store = SqliteCompStore(Path(settings.sqlite_db_path), embedder=make_embedder(settings))
    await store.ensure_indices()

    total = (await es.count(index=settings.index_comps))["count"]
    print(f"{settings.index_comps}: {total} docs -> {settings.sqlite_db_path}")

    migrated = skipped = 0
    search_after = None
    while True:
        body: dict = {
            "size": PAGE,
            "query": {"match_all": {}},
            "sort": [{"_doc": "asc"}],
        }
        if search_after:
            body["search_after"] = search_after
        resp = await es.search(index=settings.index_comps, body=body)
        hits = resp["hits"]["hits"]
        if not hits:
            break
        search_after = hits[-1]["sort"]

        comps = []
        for hit in hits:
            try:
                comps.append(doc_to_comp(hit["_source"], 0.0))
            except Exception as exc:  # noqa: BLE001 - one bad doc must not stop a backfill
                skipped += 1
                print(f"  skipped {hit.get('_id')}: {exc}")
        if comps:
            await store.upsert_comps(comps)
            migrated += len(comps)
        print(f"  {migrated}/{total} migrated", end="\r", flush=True)

    await es.close()
    await store.close()
    print(f"\ndone: {migrated} migrated, {skipped} skipped")


asyncio.run(main())
