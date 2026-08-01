"""Comp matching: hybrid retrieval plus drift guards.

The retrieval half is an Elasticsearch query; the fusion and guard halves are
pure functions so they can be tested and mutated without a cluster. Semantic
search will happily match a photo of a guitar to *every* guitar, so the guards
are not optional decoration — they are what makes the band trustworthy.

See docs/DESIGN.md § Matching.
"""

from __future__ import annotations

from typing import Any

from .models import Comp

# Attributes where a disagreement means it is a different product, not a
# variant. Compared only when both sides actually have the attribute.
# size_class deliberately excluded: it is a coarse, subjective LLM judgment
# (small/medium/large/xlarge) that varies between two photos of the exact
# same product depending on framing and context — useful for shipping-cost
# estimation (see pricing.shipping_estimate), not reliable as an identity
# signal, and firing on it dropped nearly every comp for every item once
# vision started actually populating attributes on both sides.
HARD_ATTRIBUTES = ("material", "brand", "era")

RANK_CONSTANT = 20
RANK_WINDOW = 50


def build_rrf_query(
    *,
    bm25_query: str,
    semantic_query: str,
    size: int,
    venue: str | None = None,
    semantic_field: str = "content_semantic",
) -> dict[str, Any]:
    """The load-bearing hybrid query.

    BM25 catches model numbers and SKUs when they are present; pure vector
    search cheerfully matches "Pyrex 444" to "Pyrex 441". Semantic catches
    "green pyrex bowl big" against "Spring Blossom 4qt casserole", which BM25
    scores as nearly unrelated. Neither half works alone.
    """
    filters: list[dict[str, Any]] = []
    if venue:
        filters.append({"term": {"venue": venue}})

    def wrap(query: dict[str, Any]) -> dict[str, Any]:
        if not filters:
            return {"standard": {"query": query}}
        return {"standard": {"query": {"bool": {"must": [query], "filter": filters}}}}

    return {
        "retriever": {
            "rrf": {
                "retrievers": [
                    wrap({"match": {"title": {"query": bm25_query}}}),
                    wrap({"semantic": {"field": semantic_field, "query": semantic_query}}),
                ],
                "rank_window_size": RANK_WINDOW,
                "rank_constant": RANK_CONSTANT,
            }
        },
        "size": size,
    }


def build_fallback_queries(
    *,
    bm25_query: str,
    semantic_query: str,
    size: int,
    venue: str | None = None,
    semantic_field: str = "content_semantic",
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Two plain searches, for clusters without `retriever`/`rrf` support.

    Fused by `rrf_fuse` in Python. The argument for hybrid search survives
    either path; only the execution site changes.
    """
    filters: list[dict[str, Any]] = []
    if venue:
        filters.append({"term": {"venue": venue}})

    def body(query: dict[str, Any]) -> dict[str, Any]:
        inner = {"bool": {"must": [query], "filter": filters}} if filters else query
        return {"query": inner, "size": max(size, RANK_WINDOW)}

    return (
        body({"match": {"title": {"query": bm25_query}}}),
        body({"semantic": {semantic_field: {"query": semantic_query}}}),
    )


def rrf_fuse(rankings: list[list[str]], rank_constant: int = RANK_CONSTANT) -> dict[str, float]:
    """Reciprocal rank fusion over several ranked id lists.

    score(d) = sum over rankings of 1 / (k + rank(d)), rank being 1-based.
    """
    scores: dict[str, float] = {}
    for ranking in rankings:
        for position, doc_id in enumerate(ranking, start=1):
            scores[doc_id] = scores.get(doc_id, 0.0) + 1.0 / (rank_constant + position)
    return scores


def attributes_agree(
    item_attrs: dict[str, str],
    comp_attrs: dict[str, str],
    hard_keys: tuple[str, ...] = HARD_ATTRIBUTES,
) -> bool:
    """False when a hard attribute is present on both sides and conflicts.

    A missing attribute is not a disagreement — most comps never get a vision
    pass, and absence of evidence must not drop them. Values agree if either
    is a substring of the other: two independent vision calls describe the
    same real material in different words ("Plastic, Acrylic" vs. "acrylic"),
    and treating that phrasing variance as a hard conflict was rejecting
    almost every genuine match once both sides had real attributes.
    """
    for key in hard_keys:
        mine = item_attrs.get(key, "").strip().lower()
        theirs = comp_attrs.get(key, "").strip().lower()
        if mine and theirs and mine not in theirs and theirs not in mine:
            return False
    return True


def apply_guards(
    comps: list[Comp],
    *,
    item_attrs: dict[str, str],
    score_floor: float = 0.0,
    require_price: bool = True,
) -> list[Comp]:
    """Drop comps that are below the score floor or contradict a hard attribute."""
    kept: list[Comp] = []
    for comp in comps:
        if require_price and (comp.price is None or comp.price <= 0):
            continue
        if comp.score < score_floor:
            continue
        if not attributes_agree(item_attrs, comp.attributes):
            continue
        kept.append(comp)
    return kept


def condition_matched_prices(comps: list[Comp], condition_value: str) -> list[float]:
    """Prices of comps in the same condition bucket.

    Falls back to every priced comp when the item's own grade is unknown or
    nothing matches it — a wider band beats no band, and the UI says which
    happened.
    """
    priced = [c for c in comps if c.price is not None and c.price > 0]
    if condition_value and condition_value != "unknown":
        same = [c.price for c in priced if c.condition.value == condition_value]
        if same:
            # `priced` already guarantees price is not None; filtered again so
            # mypy can narrow `list[float | None]` to `list[float]`.
            return [p for p in same if p is not None]
    return [c.price for c in priced if c.price is not None]
