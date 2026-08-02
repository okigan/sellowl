"""A `CompStore` that needs no cluster: SQLite FTS5 + brute-force vectors.

The third implementation of the `CompStore` protocol (see index.py), and the
first step of docs/MIGRATION.md's "migrate off Elastic" path. It runs
side-by-side with `ElasticCompStore` behind `SEARCH_BACKEND` so match quality
can be compared on the same store before anything is switched over.

Why this shape:

- **BM25 comes free.** SQLite's FTS5 implements Okapi BM25 and ships in the
  Python standard library. That is the same ranking function Elastic uses for
  the keyword half, with no service to run.
- **Brute-force beats an index at this scale.** A job retrieves over hundreds
  of comps, not millions. Scanning every vector is milliseconds and avoids
  taking on an ANN index (and its dependency, and its tuning) to solve a
  problem this app doesn't have. MIGRATION.md called this out as the
  lowest-dependency option; it holds up.
- **Fusion is already ours.** `match.rrf_fuse` was written as the fallback
  for clusters without `retriever`/`rrf` support, which means the Python side
  of hybrid search predates this migration and is already exercised. This
  store reuses it rather than reimplementing anything.

The honest limitation: unlike Elastic, this is single-writer and local to one
process's filesystem. That matches how job state already works (see
DESIGN.md § Jobs) and would need revisiting before any multi-instance deploy.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path
from typing import Any

from .embeddings import Embedder, HashingEmbedder, cosine
from .index import doc_to_comp
from .logging import get_logger
from .match import RANK_WINDOW, rrf_fuse
from .models import Comp, Venue

log = get_logger(__name__)

DEFAULT_DB_PATH = Path(".cache") / "comps.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS comps (
    doc_id   TEXT PRIMARY KEY,
    venue    TEXT NOT NULL,
    source   TEXT NOT NULL,
    vector   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS comps_venue ON comps(venue);
CREATE VIRTUAL TABLE IF NOT EXISTS comps_fts USING fts5(
    doc_id UNINDEXED,
    text,
    tokenize = 'porter unicode61'
);
"""


def _searchable(comp: Comp) -> str:
    attributes = " ".join(f"{k} {v}" for k, v in comp.attributes.items())
    return " ".join(filter(None, (comp.title, comp.description, attributes)))


def _fts_query(text: str) -> str:
    """FTS5 MATCH syntax is a query language, not a string.

    Raw user/LLM text routinely contains characters FTS5 treats as operators
    ('3/4"OD', 'AT&T', 'a-b'), which raise OperationalError mid-query. Quote
    every term and OR them: any term may match, more matches rank higher.
    """
    terms = [t.replace('"', "") for t in text.split()]
    return " OR ".join(f'"{t}"' for t in terms if t)


class SqliteCompStore:
    def __init__(
        self,
        db_path: Path = DEFAULT_DB_PATH,
        *,
        embedder: Embedder | None = None,
    ) -> None:
        self._db_path = db_path
        self._embedder = embedder or HashingEmbedder()
        self._conn: sqlite3.Connection | None = None

    def _connect(self) -> sqlite3.Connection:
        if self._conn is None:
            self._db_path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(self._db_path, check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
        return self._conn

    async def ensure_indices(self) -> None:
        await asyncio.to_thread(self._ensure_indices_sync)

    def _ensure_indices_sync(self) -> None:
        conn = self._connect()
        conn.executescript(_SCHEMA)
        conn.commit()

    async def upsert_comps(self, comps: list[Comp]) -> int:
        if not comps:
            return 0
        vectors = await self._embedder.embed([_searchable(c) for c in comps])
        return await asyncio.to_thread(self._upsert_sync, comps, vectors)

    def _upsert_sync(self, comps: list[Comp], vectors: list[list[float]]) -> int:
        conn = self._connect()
        with conn:
            for comp, vector in zip(comps, vectors, strict=True):
                conn.execute(
                    "INSERT INTO comps(doc_id, venue, source, vector) VALUES(?,?,?,?) "
                    "ON CONFLICT(doc_id) DO UPDATE SET "
                    "venue=excluded.venue, source=excluded.source, vector=excluded.vector",
                    (
                        comp.doc_id,
                        comp.venue.value,
                        comp.model_dump_json(),
                        json.dumps(vector),
                    ),
                )
                # FTS5 has no upsert; delete-then-insert keeps it in step.
                conn.execute("DELETE FROM comps_fts WHERE doc_id = ?", (comp.doc_id,))
                conn.execute(
                    "INSERT INTO comps_fts(doc_id, text) VALUES(?,?)",
                    (comp.doc_id, _searchable(comp)),
                )
        return len(comps)

    async def find_comps(
        self, *, bm25_query: str, semantic_query: str, venue: Venue, size: int, job_id: str
    ) -> list[Comp]:
        query_vector = await self._embedder.embed_query(semantic_query)
        return await asyncio.to_thread(self._find_sync, bm25_query, query_vector, venue, size)

    def _find_sync(
        self, bm25_query: str, query_vector: list[float], venue: Venue, size: int
    ) -> list[Comp]:
        conn = self._connect()
        keyword_ranking = self._bm25_ranking(conn, bm25_query, venue)
        semantic_ranking, sources, similarity = self._semantic_ranking(conn, query_vector, venue)

        # A relevance floor, not just a ranking. RRF returns the best N of
        # whatever exists, however bad -- so when the right comps simply
        # aren't in the corpus (a failed scrape, a novel item), it hands back
        # the least-wrong junk and the pricing stage prices it as fact. A USB
        # security key was matched against breadboard kits and a water pump
        # this way. Nothing here may be priced unless it is plausibly the
        # same kind of thing; returning too few comps is honest, because the
        # MIN_COMPS guard turns that into "insufficient data".
        floor = self._embedder.relevance_floor
        fused = rrf_fuse([keyword_ranking, semantic_ranking])
        ordered = sorted(fused.items(), key=lambda kv: kv[1], reverse=True)

        out: list[Comp] = []
        rejected = 0
        for doc_id, score in ordered:
            if similarity.get(doc_id, 0.0) < floor:
                rejected += 1
                continue
            source = sources.get(doc_id) or self._source_for(conn, doc_id)
            if source is not None:
                out.append(doc_to_comp(source, score))
            if len(out) >= size:
                break
        if rejected:
            log.info("comps_below_relevance_floor", rejected=rejected, kept=len(out), floor=floor)
        return out

    def _bm25_ranking(self, conn: sqlite3.Connection, query: str, venue: Venue) -> list[str]:
        match = _fts_query(query)
        if not match:
            return []
        try:
            rows = conn.execute(
                "SELECT f.doc_id FROM comps_fts f JOIN comps c ON c.doc_id = f.doc_id "
                "WHERE comps_fts MATCH ? AND c.venue = ? ORDER BY bm25(comps_fts) LIMIT ?",
                (match, venue.value, RANK_WINDOW),
            ).fetchall()
        except sqlite3.OperationalError as exc:  # a bad query must not kill a job
            log.warning("fts_query_failed", query=query[:80], error=str(exc))
            return []
        return [row["doc_id"] for row in rows]

    def _semantic_ranking(
        self, conn: sqlite3.Connection, query_vector: list[float], venue: Venue
    ) -> tuple[list[str], dict[str, dict[str, Any]], dict[str, float]]:
        rows = conn.execute(
            "SELECT doc_id, source, vector FROM comps WHERE venue = ?", (venue.value,)
        ).fetchall()
        scored: list[tuple[str, float]] = []
        sources: dict[str, dict[str, Any]] = {}
        similarity: dict[str, float] = {}
        for row in rows:
            sources[row["doc_id"]] = json.loads(row["source"])
            sim = cosine(query_vector, json.loads(row["vector"]))
            similarity[row["doc_id"]] = sim
            scored.append((row["doc_id"], sim))
        scored.sort(key=lambda kv: kv[1], reverse=True)
        return [doc_id for doc_id, _ in scored[:RANK_WINDOW]], sources, similarity

    def _source_for(self, conn: sqlite3.Connection, doc_id: str) -> dict[str, Any] | None:
        row = conn.execute("SELECT source FROM comps WHERE doc_id = ?", (doc_id,)).fetchone()
        return json.loads(row["source"]) if row else None

    async def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None
