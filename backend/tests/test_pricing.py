"""Money math. This is the module under mutation test, so the assertions are
deliberately tight — a surviving mutant here means a silently wrong price.
"""

from __future__ import annotations

import pytest

from sellowl.models import Condition, VerdictKind
from sellowl.pricing import (
    DEFAULT_SHIPPING,
    STALE_AFTER_DAYS,
    VERY_STALE_DAYS,
    FeeConfig,
    build_verdict,
    classify,
    local_band_is_trustworthy,
    net_proceeds_ebay,
    net_proceeds_local,
    percentiles,
    sane_shipping,
    shipping_estimate,
    staleness_pull,
)

# fb_ask_discount pinned to 1.0 (no haircut) so every existing test below
# keeps testing what it was written to test -- the discount itself has its
# own dedicated tests in TestFbAskDiscount / TestNetProceeds.
FEES = FeeConfig(ebay_fvf_rate=0.1325, ebay_fixed_fee=0.40, fb_local_rate=0.0, fb_ask_discount=1.0)


class TestPercentiles:
    def test_empty_is_none(self) -> None:
        assert percentiles([]) is None

    def test_drops_non_positive(self) -> None:
        band = percentiles([0.0, -5.0, 10.0])
        assert band is not None
        assert band.n == 1
        assert band.p50 == 10.0

    def test_single_value(self) -> None:
        band = percentiles([42.0])
        assert band is not None
        assert (band.p25, band.p50, band.p75, band.p90, band.n) == (42.0, 42.0, 42.0, 42.0, 1)

    def test_ordering_holds(self) -> None:
        band = percentiles([100, 200, 300, 400, 500, 600, 700, 800, 900, 1000])
        assert band is not None
        assert band.p25 <= band.p50 <= band.p75 <= band.p90
        assert band.n == 10

    def test_reports_real_prices_only(self) -> None:
        """Nearest-rank, not interpolation: every number is a price something
        actually sold for."""
        values = [10.0, 20.0, 30.0, 40.0]
        band = percentiles(values)
        assert band is not None
        for value in (band.p25, band.p50, band.p75, band.p90):
            assert value in values

    def test_unsorted_input(self) -> None:
        assert percentiles([300, 100, 200]) == percentiles([100, 200, 300])

    def test_a_dollar_sale_is_a_real_sale(self) -> None:
        """The floor is 'positive', not 'more than a dollar'. Junk drawer lots
        sell for $1 and they are still comps."""
        band = percentiles([1.0, 2.0, 3.0])
        assert band is not None
        assert band.n == 3
        assert band.p25 == 1.0

    def test_exact_ranks_over_a_known_distribution(self) -> None:
        """Pins the nearest-rank arithmetic itself, not just its ordering."""
        band = percentiles([float(i) for i in range(1, 101)])
        assert band is not None
        assert (band.p25, band.p50, band.p75, band.p90) == (26.0, 50.0, 76.0, 90.0)

    def test_exact_ranks_over_a_smaller_distribution(self) -> None:
        band = percentiles([float(i) for i in range(1, 51)])
        assert band is not None
        assert (band.p25, band.p50, band.p75, band.p90) == (13.0, 26.0, 38.0, 46.0)


class TestNetProceeds:
    def test_ebay_subtracts_fee_fixed_and_shipping(self) -> None:
        # 100 * (1 - 0.1325) = 86.75, minus 0.40 fixed, minus 10 shipping
        assert net_proceeds_ebay(100.0, 10.0, FEES) == pytest.approx(76.35)

    def test_local_takes_nothing_by_default(self) -> None:
        assert net_proceeds_local(100.0, FEES) == pytest.approx(100.0)

    def test_local_honours_a_nonzero_rate(self) -> None:
        fees = FeeConfig(fb_local_rate=0.05, fb_ask_discount=1.0)
        assert net_proceeds_local(100.0, fees) == pytest.approx(95.0)

    def test_shipping_reduces_net(self) -> None:
        assert net_proceeds_ebay(100.0, 50.0, FEES) < net_proceeds_ebay(100.0, 10.0, FEES)

    def test_ebay_always_nets_less_than_local_at_equal_price(self) -> None:
        assert net_proceeds_ebay(200.0, 20.0, FEES) < net_proceeds_local(200.0, FEES)


class TestFbAskDiscount:
    """A Facebook comp is only ever an *asking* price -- unlike an eBay sold
    comp (a real transacted price), it isn't what anyone actually paid.
    fb_ask_discount haircuts it before it's treated as achievable proceeds.
    """

    def test_default_discount_reduces_local_net(self) -> None:
        fees = FeeConfig(fb_local_rate=0.0)  # fb_ask_discount defaults to 0.85
        assert net_proceeds_local(100.0, fees) == pytest.approx(85.0)

    def test_discount_and_platform_rate_compound(self) -> None:
        fees = FeeConfig(fb_local_rate=0.05, fb_ask_discount=0.85)
        # 100 * 0.85 (haircut) * 0.95 (platform rate)
        assert net_proceeds_local(100.0, fees) == pytest.approx(80.75)

    def test_no_discount_is_a_no_op(self) -> None:
        fees = FeeConfig(fb_ask_discount=1.0)
        assert net_proceeds_local(100.0, fees) == pytest.approx(100.0)


class TestShippingEstimate:
    def test_known_size_classes(self) -> None:
        assert shipping_estimate({"size_class": "small"}) == 8.0
        assert shipping_estimate({"size_class": "large"}) == 65.0

    def test_case_and_space_insensitive(self) -> None:
        assert shipping_estimate({"size_class": "  LARGE "}) == 65.0

    def test_unknown_falls_back(self) -> None:
        assert shipping_estimate({}) == DEFAULT_SHIPPING
        assert shipping_estimate({"size_class": "enormous"}) == DEFAULT_SHIPPING

    def test_bigger_costs_more(self) -> None:
        assert shipping_estimate({"size_class": "small"}) < shipping_estimate(
            {"size_class": "xlarge"}
        )


class TestSaneShipping:
    def test_passes_through_when_within_ratio(self) -> None:
        assert sane_shipping(18.0, 100.0) == 18.0

    def test_clamps_to_multiple_of_the_highest_reference_price(self) -> None:
        assert sane_shipping(65.0, 10.0) == 30.0

    def test_no_references_leaves_it_unchanged(self) -> None:
        assert sane_shipping(140.0) == 140.0
        assert sane_shipping(140.0, None, None) == 140.0

    def test_ignores_non_positive_references(self) -> None:
        assert sane_shipping(140.0, None, 0.0, -5.0) == 140.0

    def test_picks_the_largest_of_several_references(self) -> None:
        # ceiling = 50 * 3 = 150, above the raw 140 estimate, so it passes through
        assert sane_shipping(140.0, 7.0, 50.0, 20.0) == 140.0
        # a lower reference set brings the ceiling below the raw estimate
        assert sane_shipping(140.0, 7.0, 20.0, 10.0) == 60.0

    def test_regression_xlarge_shipping_on_a_seven_dollar_cable(self) -> None:
        """The bug this guards: an 8ft RCA cable's printed length got read
        as an xlarge shipping box, so a $7 item's net proceeds included a
        flat $140 shipping charge -- turning a small/no-op verdict into a
        fabricated +$138 "opportunity". None of the individual formulas
        were wrong; nothing checked whether the shipping guess was
        plausible for the item it was attached to."""
        shipping = shipping_estimate({"size_class": "xlarge"})
        assert shipping == 140.0
        clamped = sane_shipping(shipping, 7.0, 23.0)
        assert clamped == pytest.approx(69.0)  # 3x the $23 high, not the raw $140


class TestLocalBandIsTrustworthy:
    def test_none_is_trusted(self) -> None:
        assert local_band_is_trustworthy(None, percentiles([25, 35, 89, 120, 50])) is True

    def test_single_comp_is_trusted(self) -> None:
        sold = percentiles([25, 35, 89, 120, 50])
        assert local_band_is_trustworthy(percentiles([150]), sold) is True

    def test_tight_local_band_is_trusted(self) -> None:
        sold = percentiles([25, 35, 89, 120, 50])
        local = percentiles([195, 200, 205])
        assert local_band_is_trustworthy(local, sold) is True

    def test_wildly_wider_local_band_is_not_trusted(self) -> None:
        sold = percentiles([25, 25, 35, 35, 50, 89, 89, 120])
        local = percentiles([50, 50, 100, 150, 260, 300, 300, 1800])
        assert local_band_is_trustworthy(local, sold) is False


class TestClassify:
    @pytest.fixture
    def band(self):  # type: ignore[no-untyped-def]
        return percentiles([100, 150, 200, 250, 300])

    def test_below_p25_is_underpriced(self, band) -> None:  # type: ignore[no-untyped-def]
        assert classify(band.p25 - 0.01, band) is VerdictKind.UNDERPRICED

    def test_above_p75_is_overpriced(self, band) -> None:  # type: ignore[no-untyped-def]
        assert classify(band.p75 + 0.01, band) is VerdictKind.OVERPRICED

    def test_boundaries_are_fair(self, band) -> None:  # type: ignore[no-untyped-def]
        """p25 and p75 themselves are inside the band, not outside it."""
        assert classify(band.p25, band) is VerdictKind.FAIR
        assert classify(band.p75, band) is VerdictKind.FAIR

    def test_no_ask_is_insufficient(self, band) -> None:  # type: ignore[no-untyped-def]
        assert classify(None, band) is VerdictKind.INSUFFICIENT_DATA


class TestBuildVerdict:
    def test_refuses_to_invent_a_median(self) -> None:
        """Two comps is not a market. This guard is never cut."""
        verdict = build_verdict(
            ask_price=85.0,
            sold_prices=[200.0, 220.0],
            local_prices=[],
            attributes={},
            condition=Condition.USABLE,
            fees=FEES,
            min_comps=5,
        )
        assert verdict.kind is VerdictKind.INSUFFICIENT_DATA
        assert verdict.target is None
        assert "need 5" in verdict.reason

    def test_exactly_min_comps_is_enough(self) -> None:
        verdict = build_verdict(
            ask_price=85.0,
            sold_prices=[200.0, 210.0, 220.0, 230.0, 240.0],
            local_prices=[],
            attributes={},
            condition=Condition.USABLE,
            fees=FEES,
            min_comps=5,
        )
        assert verdict.kind is VerdictKind.UNDERPRICED
        assert verdict.target is not None

    def test_the_demo_case(self) -> None:
        """Asking $85 on a thing whose median sold is $210."""
        verdict = build_verdict(
            ask_price=85.0,
            sold_prices=[180, 190, 200, 210, 220, 240, 265],
            local_prices=[190, 210, 240],
            attributes={"size_class": "large"},
            condition=Condition.USABLE,
            fees=FEES,
            min_comps=5,
        )
        assert verdict.kind is VerdictKind.UNDERPRICED
        assert verdict.opportunity_usd is not None
        assert verdict.opportunity_usd > 0
        assert verdict.shipping_estimate == 65.0

    def test_heavy_item_prefers_local(self) -> None:
        """65 dollars of shipping plus 13% is what makes local win."""
        verdict = build_verdict(
            ask_price=100.0,
            sold_prices=[200, 205, 210, 215, 220],
            local_prices=[195, 200, 205],
            attributes={"size_class": "large"},
            condition=Condition.USABLE,
            fees=FEES,
            min_comps=5,
        )
        assert verdict.recommended_venue is not None
        assert verdict.recommended_venue.value == "fb_local"
        assert verdict.local_net is not None and verdict.ebay_net is not None
        assert verdict.local_net > verdict.ebay_net

    def test_small_item_prefers_ebay_when_local_is_weak(self) -> None:
        verdict = build_verdict(
            ask_price=50.0,
            sold_prices=[100, 105, 110, 115, 120],
            local_prices=[40, 45, 50],
            attributes={"size_class": "small"},
            condition=Condition.CLEAN,
            fees=FEES,
            min_comps=5,
        )
        assert verdict.recommended_venue is not None
        assert verdict.recommended_venue.value == "ebay_sold"

    def test_ignores_a_wildly_scattered_local_band(self) -> None:
        """Real hack-night case: title-only matching pulled a full water-cooling
        loop and an unrelated industrial coolant system into the "comps" for a
        $15 tube, inflating the local median to $150 and the opportunity to
        +$155. The spread guard should reject that band and fall back to eBay.
        """
        verdict = build_verdict(
            ask_price=15.0,
            sold_prices=[25, 25, 35, 35, 50, 89, 89, 120],
            local_prices=[50, 50, 100, 150, 260, 300, 300, 1800],
            attributes={},
            condition=Condition.UNKNOWN,
            fees=FEES,
            min_comps=5,
        )
        assert verdict.recommended_venue is not None
        assert verdict.recommended_venue.value == "ebay_sold"
        assert verdict.local_net is None
        assert verdict.opportunity_usd is not None
        assert verdict.opportunity_usd < 30.0
        assert "mismatched" in verdict.reason

    def test_ebay_wins_exact_ties(self) -> None:
        """National reach beats a marginal local premium, so ties go to eBay.

        Constructed so local_net equals ebay_net to the cent: flipping the
        comparison to >= would hand this to local.
        """
        ebay_net = net_proceeds_ebay(100.0, DEFAULT_SHIPPING, FEES)
        verdict = build_verdict(
            ask_price=100.0,
            sold_prices=[100.0] * 5,
            local_prices=[ebay_net],
            attributes={},
            condition=Condition.USABLE,
            fees=FEES,
            min_comps=5,
        )
        assert verdict.local_net == pytest.approx(verdict.ebay_net)
        assert verdict.recommended_venue is not None
        assert verdict.recommended_venue.value == "ebay_sold"

    def test_local_wins_only_when_strictly_better(self) -> None:
        ebay_net = net_proceeds_ebay(100.0, DEFAULT_SHIPPING, FEES)
        verdict = build_verdict(
            ask_price=100.0,
            sold_prices=[100.0] * 5,
            local_prices=[ebay_net + 0.01],
            attributes={},
            condition=Condition.USABLE,
            fees=FEES,
            min_comps=5,
        )
        assert verdict.recommended_venue is not None
        assert verdict.recommended_venue.value == "fb_local"

    def test_insufficient_data_reports_what_it_found(self) -> None:
        verdict = build_verdict(
            ask_price=85.0,
            sold_prices=[200.0, 220.0],
            local_prices=[100.0],
            attributes={},
            condition=Condition.USABLE,
            fees=FEES,
            min_comps=5,
        )
        assert "Only 2 sold comp" in verdict.reason
        # The bands still come back so the UI can show the thin evidence.
        assert verdict.sold_band is not None and verdict.sold_band.n == 2
        assert verdict.local_band is not None and verdict.local_band.n == 1

    def test_insufficient_data_with_no_comps_at_all(self) -> None:
        verdict = build_verdict(
            ask_price=85.0,
            sold_prices=[],
            local_prices=[],
            attributes={},
            condition=Condition.USABLE,
            fees=FEES,
            min_comps=5,
        )
        assert "Only 0 sold comp" in verdict.reason
        assert verdict.sold_band is None
        assert verdict.local_band is None

    def test_bands_are_returned_on_the_happy_path(self) -> None:
        verdict = build_verdict(
            ask_price=85.0,
            sold_prices=[200, 210, 220, 230, 240],
            local_prices=[180, 190],
            attributes={},
            condition=Condition.USABLE,
            fees=FEES,
            min_comps=5,
        )
        assert verdict.sold_band is not None and verdict.sold_band.n == 5
        assert verdict.local_band is not None and verdict.local_band.n == 2

    def test_reason_names_the_recommended_venue(self) -> None:
        verdict = build_verdict(
            ask_price=100.0,
            sold_prices=[200, 205, 210, 215, 220],
            local_prices=[195, 200, 205],
            attributes={"size_class": "large"},
            condition=Condition.USABLE,
            fees=FEES,
            min_comps=5,
        )
        assert verdict.reason.endswith("sell locally, in person, for more.")
        # Local median (200) is actually a bit *below* eBay's (210) here --
        # local wins on avoided fees/shipping, not a higher local price, so
        # the message must not claim "local asks run higher".
        assert "skips eBay's fee and shipping" in verdict.reason
        assert "Local asks run higher" not in verdict.reason

    def test_reason_names_ebay_when_ebay_wins(self) -> None:
        verdict = build_verdict(
            ask_price=210.0,
            sold_prices=[200, 205, 210, 215, 220],
            local_prices=[40.0],
            attributes={"size_class": "small"},
            condition=Condition.CLEAN,
            fees=FEES,
            min_comps=5,
        )
        assert verdict.reason.endswith("Sell on eBay.")

    def test_no_ask_price_leaves_opportunity_none(self) -> None:
        verdict = build_verdict(
            ask_price=None,
            sold_prices=[200, 210, 220, 230, 240],
            local_prices=[],
            attributes={},
            condition=Condition.USABLE,
            fees=FEES,
            min_comps=5,
        )
        assert verdict.opportunity_usd is None
        assert verdict.kind is VerdictKind.INSUFFICIENT_DATA
        # Reached via classify(), not the early min_comps return, so this is
        # the _reason() copy rather than the "Only N sold comp(s)" message.
        assert verdict.reason == "Not enough matched comps to quote a band."

    def test_overpriced_reports_negative_opportunity(self) -> None:
        verdict = build_verdict(
            ask_price=900.0,
            sold_prices=[100, 110, 120, 130, 140],
            local_prices=[],
            attributes={},
            condition=Condition.ROUGH,
            fees=FEES,
            min_comps=5,
        )
        assert verdict.kind is VerdictKind.OVERPRICED
        assert verdict.opportunity_usd is not None
        assert verdict.opportunity_usd < 0
        assert "Reduce" in verdict.reason

    def test_overpriced_but_local_wins_says_sell_local_not_reduce(self) -> None:
        """Real hack-night confusion: "overpriced" (vs. eBay sold comps) can
        co-exist with a positive opportunity and a local recommendation, when
        local asks run well above the eBay sold median. The copy needs to say
        so instead of telling the seller to "reduce" toward a price that isn't
        actually the recommended venue's number.
        """
        verdict = build_verdict(
            ask_price=32.0,
            sold_prices=[10, 12, 13, 14, 14, 18, 20, 21],
            local_prices=[45, 48, 50, 50, 52, 55, 58, 60],
            attributes={},
            condition=Condition.UNKNOWN,
            fees=FEES,
            min_comps=5,
        )
        assert verdict.kind is VerdictKind.OVERPRICED
        assert verdict.recommended_venue is not None
        assert verdict.recommended_venue.value == "fb_local"
        assert verdict.opportunity_usd is not None and verdict.opportunity_usd > 0
        assert "Reduce" not in verdict.reason
        assert "Local asks run higher" in verdict.reason
        assert verdict.reason.endswith("sell locally, in person, for more.")

    def test_fair_but_local_wins_explains_the_opportunity(self) -> None:
        """Real user-reported confusion: "fair" (vs. eBay sold comps) read as
        contradictory next to a large opportunity and "sell local" — the
        message must connect the two in one sentence instead of letting them
        look like they disagree.
        """
        verdict = build_verdict(
            ask_price=18.0,
            sold_prices=[7, 10, 13, 17, 18, 19, 19, 47],
            local_prices=[45, 50, 55, 60, 65, 70, 75, 80],
            attributes={},
            condition=Condition.CLEAN,
            fees=FEES,
            min_comps=5,
        )
        assert verdict.kind is VerdictKind.FAIR
        assert verdict.recommended_venue is not None
        assert verdict.recommended_venue.value == "fb_local"
        assert verdict.opportunity_usd is not None and verdict.opportunity_usd > 0
        assert "In line with what this sells for on eBay" in verdict.reason
        assert "Local asks run higher" in verdict.reason
        assert verdict.reason.endswith("sell locally, in person, for more.")

    def test_local_wins_on_fees_not_price_says_so_accurately(self) -> None:
        """Real bug caught live: local can win purely because it skips eBay's
        fee and shipping, even when the local asking price is *lower* than
        eBay's own median. Claiming "local asks run higher" in that case
        would be false and checkable-as-wrong against the local band shown
        right next to it.
        """
        verdict = build_verdict(
            ask_price=8.0,
            sold_prices=[6, 6, 7, 7, 8, 17],
            local_prices=[5, 5, 5, 20],
            attributes={"size_class": "small"},
            condition=Condition.UNKNOWN,
            fees=FEES,
            min_comps=5,
        )
        assert verdict.local_band is not None
        assert verdict.sold_band is not None
        assert verdict.local_band.p50 < verdict.sold_band.p50
        assert verdict.recommended_venue is not None
        assert verdict.recommended_venue.value == "fb_local"
        assert "Local asks run higher" not in verdict.reason
        assert "skips eBay's fee and shipping" in verdict.reason

    def test_no_local_comps_leaves_local_net_none(self) -> None:
        verdict = build_verdict(
            ask_price=100.0,
            sold_prices=[100, 110, 120, 130, 140],
            local_prices=[],
            attributes={},
            condition=Condition.USABLE,
            fees=FEES,
            min_comps=5,
        )
        assert verdict.local_net is None
        assert verdict.local_band is None
        assert verdict.recommended_venue is not None
        assert verdict.recommended_venue.value == "ebay_sold"

    def test_target_range_brackets_the_target(self) -> None:
        verdict = build_verdict(
            ask_price=150.0,
            sold_prices=[100, 150, 200, 250, 300, 350],
            local_prices=[],
            attributes={},
            condition=Condition.USABLE,
            fees=FEES,
            min_comps=5,
        )
        assert verdict.target_low is not None
        assert verdict.target is not None
        assert verdict.target_high is not None
        assert verdict.target_low <= verdict.target <= verdict.target_high

    def test_target_tracks_the_recommended_venue_not_always_ebay(self) -> None:
        """Real user-reported confusion: the UI showed "Target $17" right
        next to "sell local" (where asks run $50) -- self-contradictory on
        its face. When local is the actual recommendation, target must be
        the local band, not the eBay sold band, so the displayed number
        matches the recommended action.
        """
        verdict = build_verdict(
            ask_price=18.0,
            sold_prices=[7, 10, 13, 17, 18, 19, 19, 47],
            local_prices=[45, 50, 55, 60, 65, 70, 75, 80],
            attributes={},
            condition=Condition.CLEAN,
            fees=FEES,
            min_comps=5,
        )
        assert verdict.recommended_venue is not None
        assert verdict.recommended_venue.value == "fb_local"
        assert verdict.local_band is not None
        assert verdict.target == verdict.local_band.p50
        assert verdict.target_low == verdict.local_band.p25
        assert verdict.target_high == verdict.local_band.p75
        assert verdict.sold_band is not None
        assert verdict.target != verdict.sold_band.p50

    def test_target_stays_on_sold_band_when_ebay_wins(self) -> None:
        verdict = build_verdict(
            ask_price=210.0,
            sold_prices=[200, 205, 210, 215, 220],
            local_prices=[40.0],
            attributes={"size_class": "small"},
            condition=Condition.CLEAN,
            fees=FEES,
            min_comps=5,
        )
        assert verdict.recommended_venue is not None
        assert verdict.recommended_venue.value == "ebay_sold"
        assert verdict.sold_band is not None
        assert verdict.target == verdict.sold_band.p50

    def test_reason_names_the_condition_grade(self) -> None:
        verdict = build_verdict(
            ask_price=10.0,
            sold_prices=[100, 110, 120, 130, 140],
            local_prices=[],
            attributes={},
            condition=Condition.CLEAN,
            fees=FEES,
            min_comps=5,
        )
        assert "clean" in verdict.reason


class TestBuildVerdictDiscountAndBlending:
    """Two rethought pieces, exercised through the actual verdict rather than
    the pure helper functions alone: the fb_ask_discount haircut, and
    disclosure when a band is blended across every condition."""

    def test_local_ask_discount_is_exposed_and_applied(self) -> None:
        fees = FeeConfig(ebay_fvf_rate=0.1325, ebay_fixed_fee=0.40, fb_ask_discount=0.5)
        verdict = build_verdict(
            ask_price=100.0,
            sold_prices=[200, 205, 210, 215, 220],
            local_prices=[600, 650, 700],  # high enough that local still wins post-haircut
            attributes={"size_class": "large"},
            condition=Condition.USABLE,
            fees=fees,
            min_comps=5,
        )
        assert verdict.recommended_venue is not None
        assert verdict.local_band is not None
        # net_proceeds_local applies the 0.5 haircut before the (0%) platform rate.
        assert verdict.local_net == pytest.approx(verdict.local_band.p50 * 0.5)
        assert verdict.local_ask_discount == pytest.approx(0.5)
        assert "negotiation discount" in verdict.reason

    def test_no_discount_note_when_discount_is_1(self) -> None:
        verdict = build_verdict(
            ask_price=100.0,
            sold_prices=[200, 205, 210, 215, 220],
            local_prices=[195, 200, 205],
            attributes={"size_class": "large"},
            condition=Condition.USABLE,
            fees=FEES,  # fb_ask_discount=1.0
            min_comps=5,
        )
        assert "negotiation discount" not in verdict.reason
        assert verdict.local_ask_discount == pytest.approx(1.0)

    def test_discount_is_none_when_local_net_is_none(self) -> None:
        verdict = build_verdict(
            ask_price=100.0,
            sold_prices=[200, 205, 210, 215, 220],
            local_prices=[],
            attributes={},
            condition=Condition.USABLE,
            fees=FEES,
            min_comps=5,
        )
        assert verdict.local_net is None
        assert verdict.local_ask_discount is None

    def test_blended_sold_band_is_disclosed_in_the_reason(self) -> None:
        verdict = build_verdict(
            ask_price=100.0,
            sold_prices=[200, 205, 210, 215, 220],
            local_prices=[],
            attributes={},
            condition=Condition.USABLE,
            fees=FEES,
            min_comps=5,
            sold_tier="all",
        )
        assert verdict.sold_band_tier == "all"
        assert "blends every condition" in verdict.reason

    def test_condition_tier_is_not_disclosed_as_blended(self) -> None:
        verdict = build_verdict(
            ask_price=100.0,
            sold_prices=[200, 205, 210, 215, 220],
            local_prices=[],
            attributes={},
            condition=Condition.USABLE,
            fees=FEES,
            min_comps=5,
            sold_tier="condition",
        )
        assert verdict.sold_band_tier == "condition"
        assert "blends every condition" not in verdict.reason


class TestVerdictReconciliation:
    """The number the UI can't independently check is exactly the number a
    confused user (or a reviewer) tries to check by hand: does
    opportunity_usd == (whichever venue's net) - (what you net today)?
    Before Verdict.current_net was exposed, the only "net" fields shown were
    ebay_net (net proceeds AT THE SOLD MEDIAN — a different price entirely
    from your actual ask) and local_net, neither of which is one of the two
    numbers opportunity_usd is actually computed from. That looked like the
    math didn't add up because it genuinely could not be verified from what
    was shown. This is now a permanent, real-numbers golden check.
    """

    @pytest.mark.parametrize(
        ("ask", "sold", "local", "size_class"),
        [
            # eBay wins: small item, weak local comps.
            (50.0, [100, 105, 110, 115, 120], [40, 45, 50], "small"),
            # Local wins on genuinely higher local asks.
            (100.0, [200, 205, 210, 215, 220], [195, 200, 205], "large"),
            # Local wins purely by avoiding eBay's fee/shipping, at a LOWER
            # local price than eBay's own median — the case that read as
            # most contradictory before current_net was exposed.
            (8.0, [6, 6, 7, 7, 8, 17], [5, 5, 5, 20], "small"),
            # Overpriced, local wins.
            (32.0, [10, 12, 13, 14, 14, 18, 20, 21], [45, 48, 50, 50, 52, 55, 58, 60], "medium"),
            # Overpriced, eBay wins (local comps too weak to flip it).
            (900.0, [100, 110, 120, 130, 140], [], "medium"),
        ],
    )
    def test_opportunity_reconciles_exactly(
        self, ask: float, sold: list[float], local: list[float], size_class: str
    ) -> None:
        verdict = build_verdict(
            ask_price=ask,
            sold_prices=sold,
            local_prices=local,
            attributes={"size_class": size_class},
            condition=Condition.USABLE,
            fees=FEES,
            min_comps=5,
        )
        assert verdict.current_net is not None
        assert verdict.current_net == pytest.approx(
            net_proceeds_ebay(ask, shipping_estimate({"size_class": size_class}), FEES)
        )
        assert verdict.recommended_venue is not None
        best_net = (
            verdict.local_net if verdict.recommended_venue.value == "fb_local" else verdict.ebay_net
        )
        assert best_net is not None
        assert verdict.opportunity_usd == pytest.approx(best_net - verdict.current_net)


class TestStalenessPull:
    def test_unknown_age_never_pulls(self) -> None:
        """A store's first analysis knows nothing about age; it must not
        invent a discount out of that ignorance."""
        assert staleness_pull(None) == 0.0

    def test_fresh_listing_is_not_pulled(self) -> None:
        assert staleness_pull(0) == 0.0
        assert staleness_pull(STALE_AFTER_DAYS) == 0.0

    def test_pull_grows_with_age(self) -> None:
        assert 0.0 < staleness_pull(45) < staleness_pull(75) < 1.0

    def test_caps_at_one(self) -> None:
        assert staleness_pull(VERY_STALE_DAYS) == 1.0
        assert staleness_pull(10_000) == 1.0


class TestStalenessInVerdict:
    def _verdict(self, days: int | None) -> object:
        return build_verdict(
            ask_price=100.0,
            sold_prices=[80.0, 90.0, 100.0, 110.0, 120.0],
            local_prices=[],
            attributes={"size_class": "small"},
            condition=Condition.USABLE,
            fees=FEES,
            min_comps=1,
            days_listed=days,
        )

    def test_stale_listing_targets_lower_than_a_fresh_one(self) -> None:
        fresh = self._verdict(0)
        stale = self._verdict(VERY_STALE_DAYS)
        assert stale.target is not None and fresh.target is not None
        assert stale.target < fresh.target

    def test_target_never_falls_below_the_observed_band(self) -> None:
        """Time on market moves the recommendation *within* what real sales
        support -- it must never invent a price below all of them."""
        for days in (0, 45, 90, 5000):
            verdict = self._verdict(days)
            assert verdict.target is not None and verdict.target_low is not None
            assert verdict.target >= verdict.target_low

    def test_unknown_age_matches_fresh(self) -> None:
        assert self._verdict(None).target == self._verdict(0).target

    def test_reason_explains_the_trim(self) -> None:
        verdict = self._verdict(120)
        assert "120 days" in verdict.reason
        assert verdict.days_listed == 120

    def test_fresh_listing_says_nothing_about_age(self) -> None:
        assert "days without selling" not in self._verdict(3).reason


class TestSoldDataAge:
    """eBay gates completed listings behind a login, so without a session the
    sold band is served from the stored corpus. That fallback is correct --
    old real sales beat no sales -- but it must be visible: "sells for $17"
    and "sold for $17 three months ago" are different claims."""

    def _verdict(self, age: int | None) -> object:
        return build_verdict(
            ask_price=20.0,
            sold_prices=[18.0, 19.0, 20.0, 21.0, 22.0],
            local_prices=[],
            attributes={"size_class": "small"},
            condition=Condition.USABLE,
            fees=FEES,
            min_comps=1,
            sold_data_age_days=age,
        )

    def test_age_is_reported_when_known(self) -> None:
        assert self._verdict(97).sold_data_age_days == 97

    def test_absent_when_unknown(self) -> None:
        assert self._verdict(None).sold_data_age_days is None

    def test_age_does_not_move_the_price(self) -> None:
        """Disclosure, not an adjustment: how old the data is says nothing
        about what the item is worth, only about our confidence."""
        fresh, stale = self._verdict(2), self._verdict(400)
        assert fresh.target == stale.target
        assert fresh.opportunity_usd == stale.opportunity_usd
