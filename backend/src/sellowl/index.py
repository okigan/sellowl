"""Elasticsearch: mappings, bulk upsert, hybrid retrieval.

Two implementations behind one protocol. `ElasticCompStore` is the real one.
`MemoryCompStore` exists so the pipeline and its tests can run end to end
without a cluster — it reuses the same `rrf_fuse` as the fallback path, so the
fusion logic under test is the fusion logic that ships.
"""

from __future__ import annotations

from typing import Any, Protocol

from elasticsearch import AsyncElasticsearch

from .logging import get_logger
from .match import build_fallback_queries, build_rrf_query, rrf_fuse
from .models import Comp, Venue

log = get_logger(__name__)

COMPS_MAPPING: dict[str, Any] = {
    "properties": {
        "venue": {"type": "keyword"},
        "external_id": {"type": "keyword"},
        "url": {"type": "keyword", "index": False},
        "title": {"type": "text", "copy_to": "content_semantic"},
        "title_kw": {"type": "keyword"},
        "description": {"type": "text", "copy_to": "content_semantic"},
        "content_semantic": {"type": "semantic_text"},
        "price": {"type": "double"},
        "sold_at": {"type": "date"},
        "is_sold": {"type": "boolean"},
        "condition": {"type": "keyword"},
        "condition_evidence": {"type": "text"},
        "attributes": {"type": "flattened"},
        "city": {"type": "keyword"},
        "state": {"type": "keyword"},
        "delivery": {"type": "keyword"},
        "photo_url": {"type": "keyword", "index": False},
        "scraped_at": {"type": "date"},
        "job_id": {"type": "keyword"},
    }
}


def comp_to_doc(comp: Comp) -> dict[str, Any]:
    return {
        "venue": comp.venue.value,
        "external_id": comp.external_id,
        "url": comp.url,
        "title": comp.title,
        "title_kw": comp.title[:256],
        "description": comp.description,
        "price": comp.price,
        "sold_at": comp.sold_at.isoformat() if comp.sold_at else None,
        "is_sold": comp.is_sold,
        "condition": comp.condition.value,
        "condition_evidence": comp.condition_evidence,
        "attributes": comp.attributes,
        "city": comp.city,
        "state": comp.state,
        "delivery": comp.delivery,
        "photo_url": comp.photo_url,
        "scraped_at": comp.scraped_at.isoformat(),
        "job_id": comp.job_id,
    }


class CompStore(Protocol):
    async def ensure_indices(self) -> None: ...
    async def upsert_comps(self, comps: list[Comp]) -> int: ...
    async def find_comps(
        self, *, bm25_query: str, semantic_query: str, venue: Venue, size: int, job_id: str
    ) -> list[Comp]: ...
    async def close(self) -> None: ...


class ElasticCompStore:
    def __init__(self, endpoint: str, api_key: str, index: str, rrf_enabled: bool = True) -> None:
        self._client = AsyncElasticsearch(endpoint, api_key=api_key, request_timeout=30)
        self._index = index
        self._rrf = rrf_enabled
        self._by_id: dict[str, Comp] = {}

    async def ensure_indices(self) -> None:
        if not await self._client.indices.exists(index=self._index):
            await self._client.indices.create(index=self._index, mappings=COMPS_MAPPING)
            log.info("index_created", index=self._index)

    async def upsert_comps(self, comps: list[Comp]) -> int:
        if not comps:
            return 0
        operations: list[dict[str, Any]] = []
        for comp in comps:
            self._by_id[comp.doc_id] = comp
            operations.append({"index": {"_index": self._index, "_id": comp.doc_id}})
            operations.append(comp_to_doc(comp))
        resp = await self._client.bulk(operations=operations, refresh=True)
        if resp.get("errors"):
            failed = [
                item["index"]["error"]
                for item in resp.get("items", [])
                if item.get("index", {}).get("error")
            ]
            log.warning("bulk_upsert_partial_failure", failures=len(failed), first_error=failed[:1])
        return len(comps)

    async def find_comps(
        self, *, bm25_query: str, semantic_query: str, venue: Venue, size: int, job_id: str
    ) -> list[Comp]:
        hits: list[tuple[str, float]]
        if self._rrf:
            try:
                hits = await self._search_rrf(bm25_query, semantic_query, venue, size)
            except Exception as exc:  # noqa: BLE001 - old cluster, no retriever support
                log.warning("rrf_retriever_failed", error=str(exc), fallback="python_fusion")
                self._rrf = False
                hits = await self._search_fused(bm25_query, semantic_query, venue, size)
        else:
            hits = await self._search_fused(bm25_query, semantic_query, venue, size)

        results: list[Comp] = []
        for doc_id, score in hits[:size]:
            comp = self._by_id.get(doc_id)
            if comp is None:
                continue
            results.append(comp.model_copy(update={"score": score}))
        return results

    async def _search_rrf(
        self, bm25_query: str, semantic_query: str, venue: Venue, size: int
    ) -> list[tuple[str, float]]:
        body = build_rrf_query(
            bm25_query=bm25_query,
            semantic_query=semantic_query,
            size=size,
            venue=venue.value,
        )
        resp = await self._client.search(index=self._index, **body)
        return [(h["_id"], float(h.get("_score") or 0.0)) for h in resp["hits"]["hits"]]

    async def _search_fused(
        self, bm25_query: str, semantic_query: str, venue: Venue, size: int
    ) -> list[tuple[str, float]]:
        bm25_body, semantic_body = build_fallback_queries(
            bm25_query=bm25_query,
            semantic_query=semantic_query,
            size=size,
            venue=venue.value,
        )
        rankings: list[list[str]] = []
        for body in (bm25_body, semantic_body):
            try:
                resp = await self._client.search(index=self._index, **body)
            except Exception as exc:  # noqa: BLE001 - semantic may be unavailable
                log.warning("retrieval_leg_failed", error=str(exc))
                continue
            rankings.append([h["_id"] for h in resp["hits"]["hits"]])
        fused = rrf_fuse(rankings)
        return sorted(fused.items(), key=lambda kv: kv[1], reverse=True)

    async def close(self) -> None:
        await self._client.close()


class MemoryCompStore:
    """In-process stand-in: token overlap for BM25, same for semantic.

    Not a semantic engine — it exists so the pipeline is runnable and testable
    without a cluster. Real runs use ElasticCompStore.
    """

    def __init__(self) -> None:
        self._comps: dict[str, Comp] = {}

    async def ensure_indices(self) -> None:
        return None

    async def upsert_comps(self, comps: list[Comp]) -> int:
        for comp in comps:
            self._comps[comp.doc_id] = comp
        return len(comps)

    async def find_comps(
        self, *, bm25_query: str, semantic_query: str, venue: Venue, size: int, job_id: str
    ) -> list[Comp]:
        pool = [c for c in self._comps.values() if c.venue is venue]
        rankings = [
            _rank_by_overlap(pool, bm25_query),
            _rank_by_overlap(pool, semantic_query),
        ]
        fused = rrf_fuse(rankings)
        ordered = sorted(fused.items(), key=lambda kv: kv[1], reverse=True)[:size]
        return [
            self._comps[doc_id].model_copy(update={"score": score})
            for doc_id, score in ordered
            if doc_id in self._comps
        ]

    async def close(self) -> None:
        return None


def _rank_by_overlap(comps: list[Comp], query: str) -> list[str]:
    terms = {t for t in query.lower().split() if len(t) > 2}
    scored: list[tuple[str, int]] = []
    for comp in comps:
        haystack = f"{comp.title} {comp.description}".lower()
        overlap = sum(1 for t in terms if t in haystack)
        if overlap:
            scored.append((comp.doc_id, overlap))
    scored.sort(key=lambda kv: kv[1], reverse=True)
    return [doc_id for doc_id, _ in scored]
