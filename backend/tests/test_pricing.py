"""Money math. This is the module under mutation test, so the assertions are
deliberately tight — a surviving mutant here means a silently wrong price.
"""

from __future__ import annotations

import pytest

from sellowl.models import Condition, VerdictKind
from sellowl.pricing import (
    DEFAULT_SHIPPING,
    FeeConfig,
    build_verdict,
    classify,
    net_proceeds_ebay,
    net_proceeds_local,
    percentiles,
    shipping_estimate,
)

FEES = FeeConfig(ebay_fvf_rate=0.1325, ebay_fixed_fee=0.40, fb_local_rate=0.0)


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
        fees = FeeConfig(fb_local_rate=0.05)
        assert net_proceeds_local(100.0, fees) == pytest.approx(95.0)

    def test_shipping_reduces_net(self) -> None:
        assert net_proceeds_ebay(100.0, 50.0, FEES) < net_proceeds_ebay(100.0, 10.0, FEES)

    def test_ebay_always_nets_less_than_local_at_equal_price(self) -> None:
        assert net_proceeds_ebay(200.0, 20.0, FEES) < net_proceeds_local(200.0, FEES)


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
        assert verdict.reason.endswith("Sell locally, in person.")

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
