"""Comp matching: hybrid retrieval plus drift guards.

The retrieval half is an Elasticsearch query; the fusion and guard halves are
pure functions so they can be tested and mutated without a cluster. Semantic
search will happily match a photo of a guitar to *every* guitar, so the guards
are not optional decoration — they are what makes the band trustworthy.

See docs/DESIGN.md § Matching.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from .models import Comp

# Attributes where a disagreement means it is a different product, not a
# variant. Compared only when both sides actually have the attribute.
#
# size_class and category are deliberately excluded, both for the same
# reason: they are coarse, subjective LLM judgments whose exact wording can
# vary between two separate calls on the exact same real product (small vs.
# medium; "cooling fan" vs. "PC case fan") -- treating that phrasing drift as
# a hard conflict is exactly what dropped nearly every comp once vision
# started actually populating attributes on both sides (see git history).
# size_class still feeds shipping-cost estimation (pricing.shipping_estimate)
# and category is still shown for audit; neither gates matching.
#
# Numeric spec attributes (capacity, and any future one — volume, wattage,
# pack count...) are handled separately, in SCALABLE_NUMERIC_ATTRIBUTES below,
# not here: a difference there does not necessarily mean "different product",
# it means "same product line, price should be scaled", so a plain string
# hard-reject would throw away a usable comp instead of adjusting it.
HARD_ATTRIBUTES = ("material", "brand", "era")

# Attribute keys whose value is a free-text "amount + unit" spec (a package's
# "4GB", "64 GB", "500ml", "2-pack") rather than a category label. Generic on
# purpose: any attribute matching that shape can reuse the same parse-and-
# scale mechanism below just by being added here and to the vision prompt —
# no new parsing or pricing code needed per attribute.
SCALABLE_NUMERIC_ATTRIBUTES = ("capacity",)

# Rough heuristic, not calibrated against real data: bigger specs typically
# cost less per unit (a 64GB drive rarely costs 16x a 4GB one), so scale
# sub-linearly rather than 1:1. See docs/DESIGN.md § Matching if this needs
# real calibration later.
QUANTITY_SCALE_EXPONENT = 0.6

_QUANTITY_RE = re.compile(r"^\s*([\d.]+)\s*-?\s*([a-zA-Z]*)")


@dataclass(frozen=True)
class Quantity:
    """A parsed (amount, unit) pair, e.g. "64GB" -> Quantity(64.0, "gb")."""

    amount: float
    unit: str


def parse_quantity(value: str) -> Quantity | None:
    """Parse a free-text amount+unit spec. None if it doesn't look like one."""
    if not value:
        return None
    match = _QUANTITY_RE.match(value.strip())
    if not match:
        return None
    try:
        amount = float(match.group(1))
    except ValueError:
        return None
    if amount <= 0:
        return None
    return Quantity(amount, match.group(2).strip().lower().rstrip("s"))


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


def quantity_scale_factor(item_attrs: dict[str, str], comp_attrs: dict[str, str]) -> float | None:
    """Combined price-scaling factor across SCALABLE_NUMERIC_ATTRIBUTES.

    1.0 when none of those attributes are present on both sides, or present
    but equal (no adjustment needed). A ratio (raised to
    QUANTITY_SCALE_EXPONENT) when they differ but parse with the same unit —
    the caller applies this to the comp's price rather than rejecting it.
    None when a spec is present on both sides but cannot be reconciled
    (different units, or either side doesn't parse) — a real product
    difference the caller should reject, not silently ignore or mis-scale.
    """
    factor = 1.0
    for key in SCALABLE_NUMERIC_ATTRIBUTES:
        mine_raw = item_attrs.get(key, "").strip()
        theirs_raw = comp_attrs.get(key, "").strip()
        if not mine_raw or not theirs_raw:
            continue
        mine = parse_quantity(mine_raw)
        theirs = parse_quantity(theirs_raw)
        if mine is None or theirs is None or mine.unit != theirs.unit:
            return None
        if mine.amount != theirs.amount:
            factor *= (mine.amount / theirs.amount) ** QUANTITY_SCALE_EXPONENT
    return factor


def apply_guards(
    comps: list[Comp],
    *,
    item_attrs: dict[str, str],
    score_floor: float = 0.0,
    require_price: bool = True,
) -> list[Comp]:
    """Drop comps below the score floor, with a conflicting hard attribute,
    or an irreconcilable numeric spec; scale price for a reconcilable one."""
    kept: list[Comp] = []
    for comp in comps:
        if require_price and (comp.price is None or comp.price <= 0):
            continue
        if comp.score < score_floor:
            continue
        if not attributes_agree(item_attrs, comp.attributes):
            continue
        factor = quantity_scale_factor(item_attrs, comp.attributes)
        if factor is None:
            continue
        if factor != 1.0 and comp.price is not None:
            scaled = comp.price * factor
            comp = comp.model_copy(
                update={
                    "price": scaled,
                    "price_note": f"scaled from ${comp.price:.2f} (x{factor:.2f})",
                }
            )
        kept.append(comp)
    return kept


def condition_matched_prices(
    comps: list[Comp], condition_value: str, min_comps: int = 1
) -> list[float]:
    """Prices of comps in the same condition bucket.

    Falls back to every priced comp when the item's own grade is unknown, or
    when fewer than `min_comps` comps share it -- a wider, blended band beats
    no band at all, and the UI says which happened. Before condition grading
    had real data on both sides (vision on), this fallback only ever fired on
    a literal zero; with real per-comp grades, an exact-condition bucket can
    legitimately land below the quoting threshold even though plenty of
    comps exist overall, and narrowing to too few of them was producing
    "insufficient data" that a blended band would have avoided.
    """
    priced = [c for c in comps if c.price is not None and c.price > 0]
    if condition_value and condition_value != "unknown":
        same = [c.price for c in priced if c.condition.value == condition_value]
        if len(same) >= min_comps:
            # `priced` already guarantees price is not None; filtered again so
            # mypy can narrow `list[float | None]` to `list[float]`.
            return [p for p in same if p is not None]
    return [c.price for c in priced if c.price is not None]
