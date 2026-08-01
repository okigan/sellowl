"""Money math. Pure functions only — no I/O, no LLM, no Elasticsearch.

This module is deliberately dependency-free so it can be mutation-tested
properly. A flipped comparison in here silently recommends the wrong price
while every integration test still passes, which is exactly the failure mode
mutation testing exists to catch.

See docs/DESIGN.md § Recommendation.
"""

from __future__ import annotations

from dataclasses import dataclass

from .models import Condition, PriceBand, Venue, Verdict, VerdictKind

# Coarse shipping estimates by vision-derived size class, in USD. The UI
# labels these as estimates because that is what they are.
SHIPPING_BY_SIZE: dict[str, float] = {
    "small": 8.0,
    "medium": 18.0,
    "large": 65.0,
    "xlarge": 140.0,
}
DEFAULT_SHIPPING = 18.0


@dataclass(frozen=True)
class FeeConfig:
    ebay_fvf_rate: float = 0.1325
    ebay_fixed_fee: float = 0.40
    fb_local_rate: float = 0.0


def shipping_estimate(attributes: dict[str, str]) -> float:
    """Estimate shipping from the vision-extracted size class."""
    size = attributes.get("size_class", "").strip().lower()
    return SHIPPING_BY_SIZE.get(size, DEFAULT_SHIPPING)


def percentiles(values: list[float]) -> PriceBand | None:
    """Nearest-rank percentiles over a price list.

    Returns None for an empty list. Uses nearest-rank rather than interpolation
    so every reported number is a price something actually sold for.
    """
    clean = sorted(v for v in values if v > 0)
    if not clean:
        return None

    def at(pct: float) -> float:
        rank = max(1, min(len(clean), round(pct / 100.0 * len(clean) + 0.5)))
        return clean[rank - 1]

    return PriceBand(p25=at(25), p50=at(50), p75=at(75), p90=at(90), n=len(clean))


def net_proceeds_ebay(price: float, shipping: float, fees: FeeConfig) -> float:
    """What you actually keep after eBay's cut and shipping the thing."""
    return price * (1.0 - fees.ebay_fvf_rate) - fees.ebay_fixed_fee - shipping


def net_proceeds_local(price: float, fees: FeeConfig) -> float:
    """In-person local sale: no shipping, and no fee by default."""
    return price * (1.0 - fees.fb_local_rate)


# A local ask band this many times "spikier" than the sold band (whose own
# MIN_COMPS gate already makes it the more trustworthy anchor) is a strong
# signal that at least one comp is the wrong item, not a real price signal.
LOCAL_SPREAD_GUARD_MULT = 3.0
LOCAL_SPREAD_GUARD_FLOOR = 4.0


def local_band_is_trustworthy(local_band: PriceBand | None, sold_band: PriceBand) -> bool:
    """Reject a local band whose spread is implausibly wide vs. sold reality.

    Facebook Marketplace text-matching is noisier than eBay sold comps (which
    just passed their own MIN_COMPS check): a full water-cooling loop and an
    industrial coolant system can both match "water cooling tube" on shared
    vocabulary alone under title-only matching. A single comp for the wrong
    item must not be allowed to set the recommendation on its own.
    """
    if local_band is None or local_band.n < 2:
        return True  # nothing to compare a single price against
    if local_band.p25 <= 0:
        return False
    local_spread = local_band.p90 / local_band.p25
    sold_spread = sold_band.p90 / sold_band.p25 if sold_band.p25 > 0 else 1.0
    return local_spread <= max(LOCAL_SPREAD_GUARD_MULT * sold_spread, LOCAL_SPREAD_GUARD_FLOOR)


def classify(ask_price: float | None, band: PriceBand) -> VerdictKind:
    """Where the current ask sits relative to the comp band."""
    if ask_price is None:
        return VerdictKind.INSUFFICIENT_DATA
    if ask_price < band.p25:
        return VerdictKind.UNDERPRICED
    if ask_price > band.p75:
        return VerdictKind.OVERPRICED
    return VerdictKind.FAIR


def build_verdict(
    *,
    ask_price: float | None,
    sold_prices: list[float],
    local_prices: list[float],
    attributes: dict[str, str],
    condition: Condition,
    fees: FeeConfig,
    min_comps: int,
) -> Verdict:
    """Turn matched comp prices into a recommendation.

    `sold_prices` should already be filtered to the item's condition bucket by
    the caller; this function does not re-filter, it only reports the condition
    it was told about.
    """
    sold_band = percentiles(sold_prices)
    local_band = percentiles(local_prices)

    if sold_band is None or sold_band.n < min_comps:
        found = sold_band.n if sold_band else 0
        return Verdict(
            kind=VerdictKind.INSUFFICIENT_DATA,
            reason=(
                f"Only {found} sold comp(s) matched; need {min_comps} before quoting a price band."
            ),
            sold_band=sold_band,
            local_band=local_band,
        )

    shipping = shipping_estimate(attributes)
    ebay_net = net_proceeds_ebay(sold_band.p50, shipping, fees)
    local_trusted = local_band_is_trustworthy(local_band, sold_band)
    local_net = net_proceeds_local(local_band.p50, fees) if local_band and local_trusted else None

    # eBay wins ties: national reach beats a marginal local premium.
    if local_net is not None and local_net > ebay_net:
        venue = Venue.FB_LOCAL
        best_net = local_net
    else:
        venue = Venue.EBAY_SOLD
        best_net = ebay_net

    kind = classify(ask_price, sold_band)

    current_net: float | None = None
    opportunity: float | None = None
    if ask_price is not None:
        current_net = net_proceeds_ebay(ask_price, shipping, fees)
        opportunity = best_net - current_net

    reason = _reason(kind, condition, sold_band, local_band, venue)
    if local_band is not None and not local_trusted:
        reason += " (Local asks looked scattered/mismatched — ignored for pricing.)"

    # Target tracks whichever venue is actually recommended, not always the
    # eBay sold band: showing "$17" as the target right next to "sell local"
    # (where asks run $50) contradicted the recommendation on its face.
    target_band = local_band if venue is Venue.FB_LOCAL and local_band is not None else sold_band

    return Verdict(
        kind=kind,
        reason=reason,
        sold_band=sold_band,
        local_band=local_band,
        target_low=target_band.p25,
        target_high=target_band.p75,
        target=target_band.p50,
        recommended_venue=venue,
        ebay_net=ebay_net,
        local_net=local_net,
        current_net=current_net,
        opportunity_usd=opportunity,
        shipping_estimate=shipping,
    )


def _reason(
    kind: VerdictKind,
    condition: Condition,
    sold_band: PriceBand,
    local_band: PriceBand | None,
    venue: Venue,
) -> str:
    """One sentence that never contradicts itself.

    `kind` (under/over/fair) is judged only against eBay sold comps; the
    recommended venue can independently be local, whenever local asks net
    more than eBay would. Reporting those two facts without connecting them
    reads as a contradiction ("fair" priced, yet a large "opportunity" and
    "sell local") — so every branch below states the eBay judgment AND,
    whenever local is the actual recommendation, the local premium driving
    it, in the same sentence.
    """
    grade = condition.value
    local_wins = venue is Venue.FB_LOCAL
    local_premium = ""
    if local_wins and local_band is not None:
        if local_band.p50 > sold_band.p50:
            why = f"Local asks run higher (median ${local_band.p50:,.0f})"
        else:
            # Local can still win on a lower or similar gross price: no eBay
            # fee, no shipping. Saying "local asks run higher" here would be
            # false — the median is right there in local_band for anyone to
            # check against this sentence.
            why = "Selling locally skips eBay's fee and shipping"
        local_premium = f" {why} though — sell locally, in person, for more."
    match kind:
        case VerdictKind.UNDERPRICED:
            base = (
                f"Below the {grade} band (p25 ${sold_band.p25:,.0f}) on eBay. "
                f"Median sold there is ${sold_band.p50:,.0f} across {sold_band.n} comps."
            )
            return base + (local_premium or " Sell on eBay.")
        case VerdictKind.OVERPRICED:
            base = f"Above the {grade} band (p75 ${sold_band.p75:,.0f}) on eBay."
            if local_wins:
                return base + " eBay buyers won't pay this." + local_premium
            return base + f" Reduce toward ${sold_band.p50:,.0f} to actually move it."
        case VerdictKind.FAIR:
            base = (
                f"In line with what this sells for on eBay "
                f"(${sold_band.p25:,.0f}–${sold_band.p75:,.0f})."
            )
            return base + (local_premium or " Sell on eBay.")
        case VerdictKind.INSUFFICIENT_DATA:
            return "Not enough matched comps to quote a band."
