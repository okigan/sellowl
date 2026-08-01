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


# Common spec units worth pulling straight out of a title. Not exhaustive —
# just the units actually seen on comps during dev (storage, volume, packs).
_CAPACITY_HINT_RE = re.compile(
    r"\b(\d+(?:\.\d+)?)\s*-?\s*(GB|TB|MB|ML|L|OZ|PACK|PK)\b", re.IGNORECASE
)


def capacity_from_text(text: str) -> str | None:
    """Best-effort capacity/spec straight from a title or description.

    Vision extraction has run-to-run variance — the same photo can come back
    with or without a `capacity` attribute across two separate calls, even
    when the number is stated plainly in the title ("...4GB Keypad..."). This
    is a cheap fallback for exactly that case, not a replacement for the
    vision attribute when it *is* present.
    """
    match = _CAPACITY_HINT_RE.search(text)
    if not match:
        return None
    return f"{match.group(1)}{match.group(2).upper()}"


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


def _value_agrees(mine: str, theirs: str) -> bool:
    """True if two free-text attribute values plausibly describe the same
    thing: neither empty, and either is a substring of the other. Shared by
    the hard-attribute conflict check and the model-preference bucketing
    below -- the same phrasing-variance problem shows up in both places.
    """
    mine, theirs = mine.strip().lower(), theirs.strip().lower()
    return bool(mine) and bool(theirs) and (mine in theirs or theirs in mine)


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
        mine = item_attrs.get(key, "")
        theirs = comp_attrs.get(key, "")
        if mine.strip() and theirs.strip() and not _value_agrees(mine, theirs):
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


@dataclass(frozen=True)
class MatchedPrices:
    """Prices feeding a price band, and which bucket they actually came from.

    The tier matters for honest communication: a "clean"-condition item's
    band silently blended across rough/usable/clean comps (because too few
    shared its exact condition) is a materially weaker claim than a band
    built from same-condition, same-model comps, and a user comparing their
    own ask against it deserves to know which one they're looking at.
    """

    prices: list[float]
    tier: str  # "model+condition" | "condition" | "all"


def matched_prices(
    comps: list[Comp],
    *,
    condition_value: str = "",
    model_value: str = "",
    min_comps: int = 1,
) -> MatchedPrices:
    """Prices of comps in the narrowest trustworthy bucket.

    Tries, in order, only as narrow as the data actually supports:

    1. Same condition AND same model/feature-line ("model+condition") --
       the most specific comparison available. Model text is free-form and
       inconsistent between two vision calls ("Aegis Secure Key 3" vs. "...
       3NX"), which is why this is a *preference* tried first, not a hard
       filter applied everywhere (see HARD_ATTRIBUTES's docstring for why
       exact-match gating on this class of attribute backfires) — the model
       dimension is used to narrow the band when there's enough data to do
       so safely, not to silently reject comps that don't match it.
    2. Same condition only ("condition") -- the previous behavior.
    3. Every priced comp regardless of condition or model ("all") -- a
       wider, blended band beats no band at all.

    Each tier is used only when it has at least `min_comps` prices; a
    narrower bucket that's too small to trust falls through to the next.
    Before condition grading had real per-comp data (vision off), tier 2
    only ever differed from tier 3 on a literal zero comps; with real
    grades, a same-condition bucket can legitimately land below the quoting
    threshold even with plenty of comps overall, and narrowing to too few of
    them was producing "insufficient data" that a blended band would have
    avoided. The model tier is the same shape of problem one level narrower.
    """
    priced = [c for c in comps if c.price is not None and c.price > 0]
    has_condition = bool(condition_value) and condition_value != "unknown"
    condition_bucket = (
        [c for c in priced if c.condition.value == condition_value] if has_condition else []
    )

    if model_value and condition_bucket:
        narrow = [
            c for c in condition_bucket if _value_agrees(model_value, c.attributes.get("model", ""))
        ]
        if len(narrow) >= min_comps:
            return MatchedPrices(
                [c.price for c in narrow if c.price is not None], "model+condition"
            )

    if has_condition and len(condition_bucket) >= min_comps:
        return MatchedPrices(
            [c.price for c in condition_bucket if c.price is not None], "condition"
        )

    return MatchedPrices([c.price for c in priced if c.price is not None], "all")


def condition_matched_prices(
    comps: list[Comp], condition_value: str, min_comps: int = 1
) -> list[float]:
    """Back-compat shim over `matched_prices` for callers that don't need
    to know which bucket tier was actually used."""
    return matched_prices(comps, condition_value=condition_value, min_comps=min_comps).prices
