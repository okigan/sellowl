"""Common-sense invariants over build_verdict's whole input space.

The golden-case tests in test_pricing.py check specific scenarios someone
thought to write down. They didn't catch the RCA-cable bug: a $7 item whose
vision-guessed size_class was wrong produced a +$138 "opportunity" because
nothing checked whether the *output* was plausible, only whether each
formula was individually correct. Property-based tests here fuzz the input
space instead of hand-picking cases, looking for outputs no real recommendation
should ever produce, regardless of which formula misbehaves next.
"""

from __future__ import annotations

from hypothesis import given
from hypothesis import strategies as st

from sellowl.models import Condition
from sellowl.pricing import FeeConfig, build_verdict

FEES = FeeConfig()

price = st.floats(min_value=1.0, max_value=2000.0, allow_nan=False, allow_infinity=False)
prices = st.lists(price, min_size=1, max_size=15)
size_class = st.sampled_from(["small", "medium", "large", "xlarge", "", "unknown"])


@given(
    ask_price=st.one_of(st.none(), price),
    sold_prices=prices,
    local_prices=st.one_of(st.just([]), prices),
    size_class=size_class,
    condition=st.sampled_from(list(Condition)),
)
def test_opportunity_never_dwarfs_every_price_in_play(
    ask_price: float | None,
    sold_prices: list[float],
    local_prices: list[float],
    size_class: str,
    condition: Condition,
) -> None:
    """A recommendation can't claim to leave more money on the table than
    every price signal it was given, times a generous margin. If it does,
    some input (like a size_class-driven shipping guess) is being trusted
    far past what it can support."""
    verdict = build_verdict(
        ask_price=ask_price,
        sold_prices=sold_prices,
        local_prices=local_prices,
        attributes={"size_class": size_class},
        condition=condition,
        fees=FEES,
        min_comps=1,
    )
    if verdict.opportunity_usd is None:
        return

    all_prices = [*sold_prices, *local_prices]
    if ask_price is not None:
        all_prices.append(ask_price)
    ceiling = max(all_prices) * 5

    assert abs(verdict.opportunity_usd) <= ceiling, (
        f"opportunity_usd={verdict.opportunity_usd} but every observed price "
        f"was <= {max(all_prices)} -- a single bad attribute swung the "
        f"recommendation far past what the comps support"
    )


@given(
    ask_price=price,
    sold_prices=prices,
    size_class=size_class,
)
def test_shipping_never_exceeds_sanity_ceiling(
    ask_price: float, sold_prices: list[float], size_class: str
) -> None:
    """shipping_estimate on the returned Verdict must never be many times
    larger than every price we actually have for the item -- see
    pricing.sane_shipping and docs/DESIGN.md's size_class writeup."""
    verdict = build_verdict(
        ask_price=ask_price,
        sold_prices=sold_prices,
        local_prices=[],
        attributes={"size_class": size_class},
        condition=Condition.USABLE,
        fees=FEES,
        min_comps=1,
    )
    assert verdict.shipping_estimate is not None
    ceiling = max(ask_price, *sold_prices) * 3.0
    assert verdict.shipping_estimate <= ceiling
