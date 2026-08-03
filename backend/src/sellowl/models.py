"""Shared pipeline models.

Third-party scraper JSON is parsed into these at the boundary. Everything
inside the pipeline is typed; only `sources/*` sees raw dicts.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, Field


class Condition(StrEnum):
    """Three buckets on purpose.

    A ten-point vision score is noise and collapses the moment someone asks how
    you got a 7. Three buckets with cited evidence survive the question.
    """

    ROUGH = "rough"
    USABLE = "usable"
    CLEAN = "clean"
    UNKNOWN = "unknown"


class Venue(StrEnum):
    EBAY_SOLD = "ebay_sold"
    FB_LOCAL = "fb_local"


class VerdictKind(StrEnum):
    UNDERPRICED = "underpriced"
    OVERPRICED = "overpriced"
    FAIR = "fair"
    INSUFFICIENT_DATA = "insufficient_data"


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


class VisionResult(BaseModel):
    """Structured output of one Claude vision call over one listing photo."""

    canonical_description: str = ""
    attributes: dict[str, str] = Field(default_factory=dict)
    condition: Condition = Condition.UNKNOWN
    condition_evidence: str = ""
    search_query_broad: str = ""
    search_query_narrow: str = ""


class SpecAdjustment(BaseModel):
    """One numeric-spec difference between a comp and the item being priced
    (see match.NUMERIC_SPEC_POLICIES) — e.g. a 64GB comp's price scaled down
    to approximate what a 4GB item would fetch. Rendered as a table in the UI
    rather than a hover tooltip, so the reasoning behind an adjusted price is
    visible without interaction.

    `scaled` is False for dimensions captured but deliberately not priced
    (form factor: a 140mm fan is a different fan, not more fan). Those still
    appear in the table — the difference is real and worth seeing — but with
    `factor` pinned at 1.0 so nothing implies the price moved.
    """

    feature: str
    comp_amount: str
    item_amount: str
    factor: float
    scaled: bool = True


class Comp(BaseModel):
    """One comparable listing from either venue."""

    external_id: str
    venue: Venue
    title: str
    price: float | None = None
    url: str = ""
    photo_url: str = ""
    city: str = ""
    state: str = ""
    delivery: list[str] = Field(default_factory=list)
    sold_at: datetime | None = None
    is_sold: bool = False
    condition: Condition = Condition.UNKNOWN
    condition_evidence: str = ""
    attributes: dict[str, str] = Field(default_factory=dict)
    description: str = ""
    score: float = 0.0
    # Set when `price` was scaled from a different capacity than the item's
    # own (see match.scale_price_for_capacity) — e.g. a 128GB comp's price
    # scaled down to approximate a 32GB item. Blank when price is as-scraped.
    price_note: str = ""
    spec_adjustments: list[SpecAdjustment] = Field(default_factory=list)
    scraped_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    job_id: str = ""

    @property
    def doc_id(self) -> str:
        return f"{self.venue.value}:{self.external_id}"


class PriceBand(BaseModel):
    """Percentiles over a matched comp set. `n` is the sample size."""

    p25: float
    p50: float
    p75: float
    p90: float
    n: int


class Verdict(BaseModel):
    kind: VerdictKind
    reason: str = ""
    sold_band: PriceBand | None = None
    local_band: PriceBand | None = None
    target_low: float | None = None
    target_high: float | None = None
    target: float | None = None
    recommended_venue: Venue | None = None
    ebay_net: float | None = None
    local_net: float | None = None
    # What the current ask actually nets on eBay today -- the baseline
    # opportunity_usd is computed against (best_net - current_net). Without
    # this exposed, ebay_net (net proceeds AT THE SOLD MEDIAN, a different
    # number) looks like it should reconcile with opportunity_usd and
    # doesn't, which reads as the math not adding up.
    current_net: float | None = None
    opportunity_usd: float | None = None
    shipping_estimate: float | None = None
    # How long this listing has sat unsold, when known. eBay's search output
    # has no "listed on" date, so this is observed across runs (see
    # sightings.py) -- None means this store has only ever been analysed once.
    days_listed: int | None = None
    # Median age, in days, of the actual sales behind sold_band. eBay gates
    # completed listings behind a login, so with no session the band is served
    # from the stored corpus rather than a fresh scrape -- correct, and worth
    # saying out loud, because "what it sold for" quietly becoming "what it
    # sold for a while ago" is the kind of drift a price recommendation must
    # not hide.
    sold_data_age_days: int | None = None
    # Which bucket the sold/local band actually came from: "model+condition"
    # (narrowest, most specific), "condition" (blended across models within
    # the item's condition grade), or "all" (blended across conditions too —
    # not enough same-condition comps to isolate). See match.matched_prices.
    # Exposed so a "clean"-condition item's band that's secretly blended
    # across rough/usable/clean comps doesn't read as more precise than it is.
    sold_band_tier: str | None = None
    local_band_tier: str | None = None
    # The haircut actually applied to local_band's asking-price median before
    # it became local_net (see pricing.FeeConfig.fb_ask_discount) -- None
    # when local_net itself is None (no local comps, or distrusted).
    local_ask_discount: float | None = None


class Item(BaseModel):
    """One of your listings, plus everything the pipeline learns about it."""

    external_id: str
    title: str
    ask_price: float | None = None
    url: str = ""
    photo_url: str = ""
    store_url: str = ""
    job_id: str = ""
    # The seller's own condition string off the store listing, normalized.
    # Real signal available even when vision is off — see vision.condition,
    # which takes priority once a photo grade exists.
    listed_condition: Condition = Condition.UNKNOWN
    # Days this listing has sat unsold, measured from the first time this app
    # saw it (see sightings.py). 0 on a store's first analysis.
    days_listed: int | None = None
    vision: VisionResult | None = None
    comps: list[Comp] = Field(default_factory=list)
    verdict: Verdict | None = None

    @property
    def condition(self) -> Condition:
        if self.vision and self.vision.condition is not Condition.UNKNOWN:
            return self.vision.condition
        return self.listed_condition


class JobStage(BaseModel):
    name: str
    detail: str = ""
    done: int = 0
    total: int = 0


class Job(BaseModel):
    job_id: str
    store_url: str
    metro: str
    status: JobStatus = JobStatus.QUEUED
    stage: JobStage = Field(default_factory=lambda: JobStage(name="queued"))
    error: str = ""
    items: list[Item] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    finished_at: datetime | None = None
