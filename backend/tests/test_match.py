"""Retrieval fusion and the drift guards.

Semantic search will match a photo of a guitar to every guitar. These are the
tests for the code that stops that becoming a price band.
"""

from __future__ import annotations

from sellowl.match import (
    Quantity,
    apply_guards,
    attributes_agree,
    build_fallback_queries,
    build_rrf_query,
    condition_matched_prices,
    parse_quantity,
    quantity_scale_factor,
    rrf_fuse,
)
from sellowl.models import Comp, Condition, Venue


def comp(
    ext: str,
    *,
    price: float | None = 100.0,
    condition: Condition = Condition.UNKNOWN,
    attrs: dict[str, str] | None = None,
    score: float = 1.0,
    venue: Venue = Venue.EBAY_SOLD,
) -> Comp:
    return Comp(
        external_id=ext,
        venue=venue,
        title=f"item {ext}",
        price=price,
        condition=condition,
        attributes=attrs or {},
        score=score,
    )


class TestRrfFuse:
    def test_empty(self) -> None:
        assert rrf_fuse([]) == {}

    def test_rank_one_scores_highest(self) -> None:
        scores = rrf_fuse([["a", "b", "c"]], rank_constant=20)
        assert scores["a"] > scores["b"] > scores["c"]

    def test_appearing_in_both_lists_beats_appearing_once(self) -> None:
        scores = rrf_fuse([["a", "b"], ["b", "a"]])
        both = scores["a"]
        single = rrf_fuse([["a", "b"]])["a"]
        assert both > single

    def test_consensus_wins_over_a_single_top_hit(self) -> None:
        """The whole point of RRF: agreement across retrievers outranks one
        confident retriever."""
        scores = rrf_fuse([["x", "consensus"], ["y", "consensus"]])
        assert scores["consensus"] > scores["x"]
        assert scores["consensus"] > scores["y"]

    def test_rank_constant_flattens_differences(self) -> None:
        sharp = rrf_fuse([["a", "b"]], rank_constant=1)
        flat = rrf_fuse([["a", "b"]], rank_constant=1000)
        assert sharp["a"] - sharp["b"] > flat["a"] - flat["b"]

    def test_scores_are_positive(self) -> None:
        for score in rrf_fuse([["a", "b", "c"]]).values():
            assert score > 0


class TestAttributesAgree:
    def test_empty_agrees(self) -> None:
        assert attributes_agree({}, {})

    def test_missing_is_not_disagreement(self) -> None:
        """Most comps never get a vision pass; absence of evidence must not
        drop them."""
        assert attributes_agree({"material": "teak"}, {})
        assert attributes_agree({}, {"material": "teak"})

    def test_same_value_agrees(self) -> None:
        assert attributes_agree({"material": "teak"}, {"material": "teak"})

    def test_case_and_whitespace_insensitive(self) -> None:
        assert attributes_agree({"material": "Teak "}, {"material": "teak"})

    def test_hard_conflict_disagrees(self) -> None:
        assert not attributes_agree({"material": "teak"}, {"material": "pine"})

    def test_soft_attribute_conflict_is_tolerated(self) -> None:
        """Colour is not in HARD_ATTRIBUTES: a red one and a blue one are still
        the same product."""
        assert attributes_agree({"color": "red"}, {"color": "blue"})

    def test_any_hard_conflict_is_enough(self) -> None:
        assert not attributes_agree(
            {"material": "teak", "brand": "acme"},
            {"material": "teak", "brand": "other"},
        )

    def test_compound_value_agrees_by_substring(self) -> None:
        """Two independent vision calls describe the same real material in
        different words — real hack-night regression: this was rejecting
        almost every comp once vision started populating both sides."""
        assert attributes_agree({"material": "Plastic, Acrylic"}, {"material": "acrylic"})
        assert attributes_agree({"material": "aluminum"}, {"material": "Aluminum, painted"})

    def test_size_class_is_not_a_hard_attribute(self) -> None:
        """Coarse and subjective between two photos of the same product —
        used for shipping estimation, not identity."""
        assert attributes_agree({"size_class": "small"}, {"size_class": "large"})

    def test_category_is_not_a_hard_attribute(self) -> None:
        """Deliberately not gated: two calls on the exact same real product
        can phrase category differently ("cooling fan" vs. "PC case fan"),
        and gating on it risks the same mass-rejection regression size_class
        caused once vision populated both sides for real. Known tradeoff:
        a same-brand, different-product mismatch (a padlock scored against a
        USB key because both are "Apricorn Aegis") can still slip through —
        category is captured for audit, not enforced here."""
        assert attributes_agree(
            {"brand": "Apricorn", "category": "USB flash drive"},
            {"brand": "Apricorn", "category": "padlock"},
        )


class TestParseQuantity:
    def test_number_and_unit(self) -> None:
        assert parse_quantity("4GB") == Quantity(4.0, "gb")

    def test_space_between_number_and_unit(self) -> None:
        assert parse_quantity("64 GB") == Quantity(64.0, "gb")

    def test_hyphenated_pack_count(self) -> None:
        assert parse_quantity("2-pack") == Quantity(2.0, "pack")

    def test_plural_unit_normalized(self) -> None:
        assert parse_quantity("3 packs") == Quantity(3.0, "pack")

    def test_not_a_quantity_is_none(self) -> None:
        assert parse_quantity("black") is None
        assert parse_quantity("") is None

    def test_zero_is_none(self) -> None:
        assert parse_quantity("0GB") is None


class TestQuantityScaleFactor:
    def test_no_numeric_attributes_present_is_neutral(self) -> None:
        assert quantity_scale_factor({"brand": "Apricorn"}, {"brand": "Apricorn"}) == 1.0

    def test_equal_capacity_is_neutral(self) -> None:
        assert quantity_scale_factor({"capacity": "4GB"}, {"capacity": "4GB"}) == 1.0

    def test_different_capacity_scales_sublinearly(self) -> None:
        """A 64GB comp priced for a 4GB item: scaled down, but by less than
        the raw 16x ratio -- bigger capacities cost less per unit."""
        factor = quantity_scale_factor({"capacity": "4GB"}, {"capacity": "64GB"})
        assert factor is not None
        assert 0 < factor < 1.0

    def test_scale_factor_is_the_inverse_in_reverse(self) -> None:
        """Real hack-night bug this design must avoid: "4GB" is a substring
        of "64GB", so naive substring tolerance would treat them as equal."""
        down = quantity_scale_factor({"capacity": "4GB"}, {"capacity": "64GB"})
        up = quantity_scale_factor({"capacity": "64GB"}, {"capacity": "4GB"})
        assert down is not None and up is not None
        assert down < 1.0 < up

    def test_mismatched_units_is_irreconcilable(self) -> None:
        assert quantity_scale_factor({"capacity": "4GB"}, {"capacity": "2-pack"}) is None

    def test_unparsable_value_is_irreconcilable(self) -> None:
        assert quantity_scale_factor({"capacity": "4GB"}, {"capacity": "large"}) is None


class TestApplyGuards:
    def test_drops_unpriced(self) -> None:
        kept = apply_guards([comp("a", price=None)], item_attrs={})
        assert kept == []

    def test_drops_zero_priced(self) -> None:
        assert apply_guards([comp("a", price=0.0)], item_attrs={}) == []

    def test_keeps_unpriced_when_not_required(self) -> None:
        kept = apply_guards([comp("a", price=None)], item_attrs={}, require_price=False)
        assert len(kept) == 1

    def test_drops_below_score_floor(self) -> None:
        kept = apply_guards([comp("a", score=0.01)], item_attrs={}, score_floor=0.5)
        assert kept == []

    def test_keeps_exactly_at_floor(self) -> None:
        kept = apply_guards([comp("a", score=0.5)], item_attrs={}, score_floor=0.5)
        assert len(kept) == 1

    def test_drops_hard_attribute_conflict(self) -> None:
        kept = apply_guards(
            [comp("a", attrs={"material": "pine"})],
            item_attrs={"material": "teak"},
        )
        assert kept == []

    def test_keeps_the_good_one_among_bad_ones(self) -> None:
        kept = apply_guards(
            [
                comp("bad-price", price=None),
                comp("bad-attr", attrs={"brand": "wrong"}),
                comp("good", attrs={"brand": "acme"}),
            ],
            item_attrs={"brand": "acme"},
        )
        assert [c.external_id for c in kept] == ["good"]

    def test_scales_price_for_a_different_capacity_instead_of_dropping(self) -> None:
        """The real feature this guards against losing: a 64GB comp is a
        usable signal for a 4GB item once its price is scaled down, not a
        different product to throw away."""
        kept = apply_guards(
            [comp("a", price=64.0, attrs={"brand": "Apricorn", "capacity": "64GB"})],
            item_attrs={"brand": "Apricorn", "capacity": "4GB"},
        )
        assert len(kept) == 1
        assert kept[0].price is not None and kept[0].price < 64.0
        assert "scaled" in kept[0].price_note

    def test_matching_capacity_leaves_price_and_note_untouched(self) -> None:
        kept = apply_guards(
            [comp("a", price=20.0, attrs={"capacity": "4GB"})],
            item_attrs={"capacity": "4GB"},
        )
        assert len(kept) == 1
        assert kept[0].price == 20.0
        assert kept[0].price_note == ""

    def test_drops_irreconcilable_capacity_units(self) -> None:
        kept = apply_guards(
            [comp("a", attrs={"capacity": "2-pack"})],
            item_attrs={"capacity": "4GB"},
        )
        assert kept == []


class TestConditionMatchedPrices:
    def test_prefers_the_same_bucket(self) -> None:
        comps = [
            comp("clean1", price=300.0, condition=Condition.CLEAN),
            comp("usable1", price=200.0, condition=Condition.USABLE),
            comp("usable2", price=210.0, condition=Condition.USABLE),
        ]
        assert sorted(condition_matched_prices(comps, "usable")) == [200.0, 210.0]

    def test_falls_back_to_everything_when_bucket_is_empty(self) -> None:
        """A wider band beats no band; the UI says which happened."""
        comps = [comp("c", price=300.0, condition=Condition.CLEAN)]
        assert condition_matched_prices(comps, "rough") == [300.0]

    def test_unknown_condition_uses_everything(self) -> None:
        comps = [
            comp("a", price=100.0, condition=Condition.CLEAN),
            comp("b", price=200.0, condition=Condition.ROUGH),
        ]
        assert sorted(condition_matched_prices(comps, "unknown")) == [100.0, 200.0]

    def test_skips_unpriced(self) -> None:
        comps = [comp("a", price=None, condition=Condition.CLEAN)]
        assert condition_matched_prices(comps, "clean") == []

    def test_falls_back_when_bucket_is_too_small_to_quote(self) -> None:
        """Real hack-night regression: once vision graded both sides, an
        exact-condition bucket of 2 (below min_comps=5) was returned as-is,
        reporting "insufficient data" even though 5 total comps existed."""
        comps = [
            comp("usable1", price=8.99, condition=Condition.USABLE),
            comp("usable2", price=10.99, condition=Condition.USABLE),
            comp("clean1", price=39.0, condition=Condition.CLEAN),
            comp("clean2", price=30.0, condition=Condition.CLEAN),
            comp("clean3", price=15.0, condition=Condition.CLEAN),
        ]
        assert len(condition_matched_prices(comps, "usable")) == 2  # default min_comps=1
        blended = condition_matched_prices(comps, "usable", min_comps=5)
        assert len(blended) == 5
        assert sorted(blended) == [8.99, 10.99, 15.0, 30.0, 39.0]

    def test_still_prefers_the_bucket_when_it_meets_min_comps(self) -> None:
        comps = [
            comp("usable1", price=200.0, condition=Condition.USABLE),
            comp("usable2", price=210.0, condition=Condition.USABLE),
            comp("clean1", price=300.0, condition=Condition.CLEAN),
        ]
        assert sorted(condition_matched_prices(comps, "usable", min_comps=2)) == [200.0, 210.0]


class TestQueryShapes:
    def test_rrf_query_has_both_legs(self) -> None:
        body = build_rrf_query(bm25_query="pyrex 444", semantic_query="green bowl", size=8)
        retrievers = body["retriever"]["rrf"]["retrievers"]
        assert len(retrievers) == 2
        assert body["size"] == 8
        leaves = str(retrievers)
        assert "match" in leaves and "semantic" in leaves

    def test_rrf_query_applies_venue_filter(self) -> None:
        body = build_rrf_query(bm25_query="a", semantic_query="b", size=4, venue="ebay_sold")
        assert "ebay_sold" in str(body)

    def test_fallback_returns_two_bodies(self) -> None:
        bm25, semantic = build_fallback_queries(
            bm25_query="pyrex 444", semantic_query="green bowl", size=8
        )
        assert "match" in str(bm25["query"])
        assert "semantic" in str(semantic["query"])
