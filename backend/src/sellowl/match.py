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

from .models import Comp, SpecAdjustment

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
# Numeric spec attributes (capacity, pack_count, length, form_factor) are
# handled separately, in NUMERIC_SPEC_POLICIES below, not here: a difference
# there does not necessarily mean "different product", it means "same product
# line, price should be scaled", so a plain string hard-reject would throw
# away a usable comp instead of adjusting it.
HARD_ATTRIBUTES = ("material", "brand", "era")


@dataclass(frozen=True)
class SpecPolicy:
    """How one numeric spec dimension relates to price.

    `exponent` is the power the (item / comp) amount ratio is raised to.
    `None` means "capture the difference, never price it": the dimension
    distinguishes product variants rather than measuring more-of-the-same,
    so scaling it would fabricate confidence. See § Matching in DESIGN.md.
    """

    exponent: float | None
    label: str


# Each numeric spec gets its own dimension *and its own price relationship*.
# Lumping them into one `capacity` field with one exponent (what this used to
# do) was wrong in both directions: it scaled a 3-pack as if pack count
# behaved like storage capacity, and it compared a fan's "120mm" against a
# cable's "6 ft" as though they were the same kind of number.
#
# Exponents are uncalibrated starting assumptions, not measurements -- but
# they are deliberately *different* assumptions, because these dimensions
# demonstrably do not price alike:
#   capacity     sub-linear: a 64GB drive is nowhere near 16x a 4GB one.
#   pack_count   near-linear: a 3-pack really does cost ~3x a single, minus
#                a small bulk discount.
#   length       weakly sub-linear: a 15ft cable costs somewhat more than a
#                6ft one, nothing like 2.5x.
#   form_factor  not priced at all. 120mm vs 140mm is a different fan, not
#                more fan; 240mm vs 360mm is a different radiator. Ratio
#                scaling here produces confident nonsense, so it is captured
#                for audit and explicitly excluded from pricing.
NUMERIC_SPEC_POLICIES: dict[str, SpecPolicy] = {
    "capacity": SpecPolicy(exponent=0.6, label="capacity"),
    "pack_count": SpecPolicy(exponent=0.9, label="pack count"),
    "length": SpecPolicy(exponent=0.35, label="length"),
    "form_factor": SpecPolicy(exponent=None, label="form factor"),
}

# Every dimension vision is asked to extract, priced or not.
NUMERIC_SPEC_ATTRIBUTES = tuple(NUMERIC_SPEC_POLICIES)

# Only the ones that actually move a price.
SCALABLE_NUMERIC_ATTRIBUTES = tuple(
    key for key, policy in NUMERIC_SPEC_POLICIES.items() if policy.exponent is not None
)

# Kept for the (rare) caller that wants the historical single exponent.
QUANTITY_SCALE_EXPONENT = 0.6

# Unit spellings seen in real vision output and real listing titles, mapped to
# one canonical unit. Without this, "6 ft" and "6 feet" are two different
# units and the comp gets thrown away for disagreeing with itself.
_UNIT_ALIASES: dict[str, str] = {
    # storage
    "b": "b", "byte": "b",
    "kb": "kb", "kilobyte": "kb",
    "mb": "mb", "megabyte": "mb",
    "gb": "gb", "gig": "gb", "gigabyte": "gb",
    "tb": "tb", "terabyte": "tb",
    # volume
    "ml": "ml", "milliliter": "ml", "millilitre": "ml",
    "l": "l", "liter": "l", "litre": "l",
    "oz": "oz", "ounce": "oz",
    # length
    "mm": "mm", "millimeter": "mm", "millimetre": "mm",
    "cm": "cm", "centimeter": "cm", "centimetre": "cm",
    "m": "m", "meter": "m", "metre": "m",
    "in": "in", "inch": "in", "inche": "in", '"': "in",
    "ft": "ft", "foot": "ft", "feet": "ft", "'": "ft",
    "yd": "yd", "yard": "yd",
    # count
    "pack": "pack", "pk": "pack", "pc": "pack", "pcs": "pack",
    "piece": "pack", "count": "pack", "ct": "pack", "x": "pack",
    "fan pack": "pack", "fan": "pack", "unit": "pack",
}  # fmt: skip

# Canonical unit -> (family, size in that family's base unit). Comparing
# across units inside a family is just arithmetic: 1TB vs 500GB is a 2x
# difference, not an irreconcilable one.
_UNIT_FAMILY: dict[str, tuple[str, float]] = {
    "b": ("storage", 1.0),
    "kb": ("storage", 1e3),
    "mb": ("storage", 1e6),
    "gb": ("storage", 1e9),
    "tb": ("storage", 1e12),
    "ml": ("volume", 1.0),
    "l": ("volume", 1000.0),
    "oz": ("volume", 29.5735),
    "mm": ("length", 1.0),
    "cm": ("length", 10.0),
    "m": ("length", 1000.0),
    "in": ("length", 25.4),
    "ft": ("length", 304.8),
    "yd": ("length", 914.4),
    "pack": ("count", 1.0),
}

# Anchored on purpose -- the value must be *entirely* a number plus a unit.
# An unanchored match is how "10-in-1" became "10 inches" and "1 x 3 ft"
# became "1 x": both parsed, both wrong, both silently repriced a comp.
_QUANTITY_RE = re.compile(r"^\s*([\d.]+)\s*[-\s]?\s*([a-zA-Z'\" ]+?)\s*\.?\s*$")


@dataclass(frozen=True)
class Quantity:
    """A parsed (amount, unit) pair, e.g. "64GB" -> Quantity(64.0, "gb")."""

    amount: float
    unit: str

    @property
    def family(self) -> str:
        return _UNIT_FAMILY[self.unit][0]

    @property
    def base_amount(self) -> float:
        """Amount expressed in the family's base unit, so units compare."""
        return self.amount * _UNIT_FAMILY[self.unit][1]


def parse_quantity(value: str) -> Quantity | None:
    """Parse a free-text amount+unit spec. None if it isn't cleanly one.

    Returning None is the safe answer and it means "no usable information",
    never "different product" -- callers must not drop a comp over it. Real
    vision output is full of values that look numeric but aren't specs
    ("10-in-1", "40+ parts", "130 projects", "unspecified"); each one of
    those used to either delete a good comp or reprice it against a
    hallucinated unit.
    """
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
    raw_unit = match.group(2).strip().lower()
    unit = _UNIT_ALIASES.get(raw_unit) or _UNIT_ALIASES.get(raw_unit.rstrip("s"))
    if unit is None or unit not in _UNIT_FAMILY:
        return None
    return Quantity(amount, unit)


# Which dimension a bare unit found in a title belongs to.
#
# Storage and volume both land in `capacity` -- they answer the same question
# (how much of the stuff does it hold). The length family splits by unit
# rather than family, and that split is load-bearing: millimetres in a
# secondhand-goods title are practically always a *form factor* (a 120mm fan,
# a 360mm radiator, 12mm tubing), while feet/metres/inches are practically
# always a *length* (a 6ft cable). Routing both to `length` priced a fan's
# size as though it were cable footage -- and worse, disagreed with what
# vision does with the same value, so one listing got two different answers
# depending on whether the photo or the title won.
_UNIT_TO_ATTRIBUTE: dict[str, str] = {
    "b": "capacity", "kb": "capacity", "mb": "capacity",
    "gb": "capacity", "tb": "capacity",
    "ml": "capacity", "l": "capacity", "oz": "capacity",
    "mm": "form_factor", "cm": "form_factor",
    "m": "length", "in": "length", "ft": "length", "yd": "length",
    "pack": "pack_count",
}  # fmt: skip

# Units worth pulling straight out of a title, longest-first so "feet" wins
# over "ft" and "gb" isn't eaten by "b".
_TEXT_UNITS = sorted(_UNIT_ALIASES, key=len, reverse=True)
# The trailing lookahead is load-bearing: without it "10-in-1" matches as
# "10 in" (ten inches) and "1 x 3 ft" as "1 x" (a one-pack). A unit followed
# by another number is part of a compound name, not a spec.
_SPEC_HINT_RE = re.compile(
    r"\b(\d+(?:\.\d+)?)\s*-?\s*("
    + "|".join(re.escape(u) for u in _TEXT_UNITS)
    + r")\b(?!\s*[-x/]\s*\d)",
    re.IGNORECASE,
)


def specs_from_text(text: str) -> dict[str, str]:
    """Best-effort numeric specs straight from a title or description.

    Vision extraction has run-to-run variance -- the same photo can come back
    with or without a spec attribute across two separate calls, even when the
    number is stated plainly in the title ("...4GB Keypad...", "...3-Pack..."),
    so this is a cheap fallback for exactly that case. First hit per dimension
    wins; a title's leading spec is nearly always the headline one.
    """
    found: dict[str, str] = {}
    for match in _SPEC_HINT_RE.finditer(text):
        quantity = parse_quantity(f"{match.group(1)}{match.group(2)}")
        if quantity is None:
            continue
        attribute = _UNIT_TO_ATTRIBUTE.get(quantity.unit)
        if attribute is None or attribute in found:
            continue
        found[attribute] = f"{match.group(1)}{match.group(2).upper()}"
    return found


def capacity_from_text(text: str) -> str | None:
    """Back-compat shim: just the `capacity` dimension of `specs_from_text`."""
    return specs_from_text(text).get("capacity")


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


def quantity_scale_adjustments(
    item_attrs: dict[str, str], comp_attrs: dict[str, str]
) -> list[SpecAdjustment]:
    """Per-dimension breakdown of numeric spec differences.

    One entry per dimension in NUMERIC_SPEC_POLICIES that is present on both
    sides, parses cleanly, and differs. Entries carry `scaled=False` when the
    dimension is captured but deliberately not priced (see SpecPolicy), so
    the UI can show the difference without implying it moved the number.

    Never rejects. A spec that doesn't parse, or that parses into a different
    unit family than its counterpart, is *missing information* -- not
    evidence of a different product. Treating it as a rejection is what
    silently deleted comps whose capacity read "unspecified" or "40+ parts",
    and what dropped a "3x" comp for disagreeing with an otherwise identical
    "3-pack" one. Genuine "different product" rejection is HARD_ATTRIBUTES'
    job; this function only ever adjusts prices.
    """
    adjustments: list[SpecAdjustment] = []
    for key, policy in NUMERIC_SPEC_POLICIES.items():
        mine_raw = item_attrs.get(key, "").strip()
        theirs_raw = comp_attrs.get(key, "").strip()
        if not mine_raw or not theirs_raw:
            continue
        mine = parse_quantity(mine_raw)
        theirs = parse_quantity(theirs_raw)
        if mine is None or theirs is None or mine.family != theirs.family:
            continue
        if mine.base_amount == theirs.base_amount:
            continue
        exponent = policy.exponent
        scaled = exponent is not None
        factor = (
            (mine.base_amount / theirs.base_amount) ** exponent if exponent is not None else 1.0
        )
        adjustments.append(
            SpecAdjustment(
                feature=policy.label,
                comp_amount=theirs_raw,
                item_amount=mine_raw,
                factor=factor,
                scaled=scaled,
            )
        )
    return adjustments


def quantity_scale_factor(item_attrs: dict[str, str], comp_attrs: dict[str, str]) -> float:
    """Combined price-scaling factor across every priced numeric dimension.

    1.0 when nothing needs adjusting. See `quantity_scale_adjustments` for
    the per-dimension breakdown this multiplies together.
    """
    factor = 1.0
    for adjustment in quantity_scale_adjustments(item_attrs, comp_attrs):
        factor *= adjustment.factor
    return factor


def apply_guards(
    comps: list[Comp],
    *,
    item_attrs: dict[str, str],
    score_floor: float = 0.0,
    require_price: bool = True,
) -> list[Comp]:
    """Drop comps below the score floor or with a conflicting hard attribute;
    scale the price of the survivors toward this item's own numeric specs.

    Numeric specs never cause a drop -- see `quantity_scale_adjustments`."""
    kept: list[Comp] = []
    for comp in comps:
        if require_price and (comp.price is None or comp.price <= 0):
            continue
        if comp.score < score_floor:
            continue
        if not attributes_agree(item_attrs, comp.attributes):
            continue
        adjustments = quantity_scale_adjustments(item_attrs, comp.attributes)
        if adjustments and comp.price is not None:
            factor = 1.0
            for adjustment in adjustments:
                factor *= adjustment.factor
            update: dict[str, Any] = {"spec_adjustments": adjustments}
            # An unpriced dimension (form factor) still belongs in the table,
            # but must not touch the price or claim it did.
            if factor != 1.0:
                update["price"] = comp.price * factor
                update["price_note"] = f"scaled from ${comp.price:.2f} (x{factor:.2f})"
            comp = comp.model_copy(update=update)
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
