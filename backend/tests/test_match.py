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
    capacity_from_text,
    condition_matched_prices,
    matched_prices,
    parse_quantity,
    quantity_scale_adjustments,
    quantity_scale_factor,
    rrf_fuse,
    specs_from_text,
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

    def test_model_is_not_a_hard_attribute(self) -> None:
        """Same reasoning as category: model-name text read off packaging is
        inconsistent between two independent vision calls ("Aegis Secure Key
        3" vs. "Aegis Secure Key 3z" for the same physical line) -- captured
        for audit, not enforced, until there's a real corpus showing the
        vocabulary is consistent enough to gate on."""
        assert attributes_agree(
            {"brand": "Apricorn", "model": "Aegis Secure Key 3"},
            {"brand": "Apricorn", "model": "Aegis Secure Key 3NX"},
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


class TestCapacityFromText:
    def test_finds_capacity_mid_title(self) -> None:
        """Vision extraction has run-to-run variance on whether it surfaces
        this attribute at all; the title usually just says it."""
        title = "Apricorn Aegis Secure Key 4GB Keypad Hardware Encrypted USB 2.0 FIPS Black"
        assert capacity_from_text(title) == "4GB"

    def test_normalizes_case(self) -> None:
        assert capacity_from_text("Apricorn 16gb Aegis Secure Key") == "16GB"

    def test_no_match_is_none(self) -> None:
        assert capacity_from_text("Apricorn Aegis Padlock 3.0") is None

    def test_pack_count_is_no_longer_a_capacity(self) -> None:
        """Pack count is its own dimension now -- it prices near-linearly,
        capacity prices sub-linearly, so conflating them mis-scaled both."""
        assert capacity_from_text("Enermax Case Fan 3-Pack with Controller") is None
        assert specs_from_text("Enermax Case Fan 3-Pack with Controller") == {"pack_count": "3PACK"}


class TestSpecsFromText:
    def test_splits_dimensions_by_unit(self) -> None:
        assert specs_from_text("Apricorn Aegis Secure Key 4GB") == {"capacity": "4GB"}
        assert specs_from_text("AmazonBasics RCA Cable 8 ft Gold") == {"length": "8FT"}
        assert specs_from_text("Enermax T.B.RGB 3-Pack") == {"pack_count": "3PACK"}

    def test_finds_several_dimensions_in_one_title(self) -> None:
        found = specs_from_text("Thermaltake Case Fan 3-Pack 6ft cable 500GB bundle")
        assert found == {"pack_count": "3PACK", "length": "6FT", "capacity": "500GB"}

    def test_first_hit_per_dimension_wins(self) -> None:
        assert specs_from_text("Cable 6ft and also 15ft")["length"] == "6FT"

    def test_ignores_numbers_that_are_not_specs(self) -> None:
        """Real observed vision/title junk that used to parse as a spec."""
        assert specs_from_text("Makeblock Ultimate 2.0 - 10-in-1 Robot Kit") == {}
        assert specs_from_text("Kit with 130 projects and 70 parts") == {}

    def test_millimetres_are_a_form_factor_not_a_length(self) -> None:
        """A secondhand title's "120mm" is a fan/radiator size, not cable
        footage. Routing it to `length` priced a fan's size like cable
        footage AND disagreed with what vision does with the same value, so
        one listing got two answers depending on which source won."""
        assert specs_from_text("Enermax 120mm Case Fan") == {"form_factor": "120MM"}
        assert specs_from_text("Thermaltake 360mm Radiator") == {"form_factor": "360MM"}
        assert specs_from_text("RCA Cable 6 ft") == {"length": "6FT"}

    def test_no_match_is_empty(self) -> None:
        assert specs_from_text("Apricorn Aegis Padlock 3.0") == {}


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
        assert down < 1.0 < up

    def test_converts_across_units_in_one_family(self) -> None:
        """1TB vs 500GB is a 2x difference, not an irreconcilable one."""
        assert quantity_scale_factor({"capacity": "1TB"}, {"capacity": "500GB"}) > 1.0
        assert quantity_scale_factor({"capacity": "1000GB"}, {"capacity": "1TB"}) == 1.0

    def test_unparsable_value_is_neutral_not_a_rejection(self) -> None:
        """ "Missing information" must never masquerade as "different
        product" -- that silently deleted comps whose spec read
        "unspecified" or "40+ parts"."""
        assert quantity_scale_factor({"capacity": "4GB"}, {"capacity": "large"}) == 1.0
        assert quantity_scale_factor({"capacity": "4GB"}, {"capacity": "unspecified"}) == 1.0

    def test_different_families_are_neutral_not_a_rejection(self) -> None:
        assert quantity_scale_factor({"capacity": "4GB"}, {"capacity": "2-pack"}) == 1.0

    def test_pack_count_scales_more_steeply_than_capacity(self) -> None:
        """A 3-pack really is worth ~3x a single; a 3x capacity is worth far
        less than 3x. Same ratio, deliberately different exponents."""
        pack = quantity_scale_factor({"pack_count": "3-pack"}, {"pack_count": "1-pack"})
        capacity = quantity_scale_factor({"capacity": "3GB"}, {"capacity": "1GB"})
        assert pack > capacity > 1.0

    def test_length_scales_more_gently_than_capacity(self) -> None:
        length = quantity_scale_factor({"length": "3ft"}, {"length": "1ft"})
        capacity = quantity_scale_factor({"capacity": "3GB"}, {"capacity": "1GB"})
        assert 1.0 < length < capacity

    def test_form_factor_never_moves_the_price(self) -> None:
        """120mm vs 140mm is a different fan, not more fan."""
        assert quantity_scale_factor({"form_factor": "120mm"}, {"form_factor": "140mm"}) == 1.0
        assert quantity_scale_factor({"form_factor": "120mm"}, {"form_factor": "360mm"}) == 1.0

    def test_several_dimensions_compound(self) -> None:
        both = quantity_scale_factor(
            {"capacity": "8GB", "pack_count": "2-pack"},
            {"capacity": "4GB", "pack_count": "1-pack"},
        )
        capacity_only = quantity_scale_factor({"capacity": "8GB"}, {"capacity": "4GB"})
        assert both > capacity_only > 1.0


class TestQuantityScaleAdjustments:
    def test_no_numeric_attributes_present_is_empty(self) -> None:
        assert quantity_scale_adjustments({"brand": "Apricorn"}, {"brand": "Apricorn"}) == []

    def test_equal_capacity_is_empty(self) -> None:
        assert quantity_scale_adjustments({"capacity": "4GB"}, {"capacity": "4GB"}) == []

    def test_different_capacity_reports_both_amounts_and_factor(self) -> None:
        adjustments = quantity_scale_adjustments({"capacity": "4GB"}, {"capacity": "64GB"})
        assert len(adjustments) == 1
        adjustment = adjustments[0]
        assert adjustment.feature == "capacity"
        assert adjustment.item_amount == "4GB"
        assert adjustment.comp_amount == "64GB"
        assert 0 < adjustment.factor < 1.0
        assert adjustment.scaled is True

    def test_irreconcilable_is_empty_not_a_rejection(self) -> None:
        assert quantity_scale_adjustments({"capacity": "4GB"}, {"capacity": "2-pack"}) == []

    def test_form_factor_is_reported_but_marked_unscaled(self) -> None:
        """The difference is real and worth showing; pricing it is not."""
        adjustments = quantity_scale_adjustments({"form_factor": "120mm"}, {"form_factor": "360mm"})
        assert len(adjustments) == 1
        assert adjustments[0].feature == "form factor"
        assert adjustments[0].scaled is False
        assert adjustments[0].factor == 1.0

    def test_reports_one_entry_per_differing_dimension(self) -> None:
        adjustments = quantity_scale_adjustments(
            {"capacity": "4GB", "pack_count": "1-pack", "length": "6ft"},
            {"capacity": "64GB", "pack_count": "3-pack", "length": "6ft"},
        )
        assert {a.feature for a in adjustments} == {"capacity", "pack count"}

    def test_product_of_factors_matches_quantity_scale_factor(self) -> None:
        adjustments = quantity_scale_adjustments({"capacity": "4GB"}, {"capacity": "64GB"})
        product = 1.0
        for adjustment in adjustments:
            product *= adjustment.factor
        assert product == quantity_scale_factor({"capacity": "4GB"}, {"capacity": "64GB"})


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
        assert len(kept[0].spec_adjustments) == 1
        adjustment = kept[0].spec_adjustments[0]
        assert adjustment.feature == "capacity"
        assert adjustment.comp_amount == "64GB"
        assert adjustment.item_amount == "4GB"
        assert adjustment.factor < 1.0

    def test_matching_capacity_leaves_price_and_note_untouched(self) -> None:
        kept = apply_guards(
            [comp("a", price=20.0, attrs={"capacity": "4GB"})],
            item_attrs={"capacity": "4GB"},
        )
        assert len(kept) == 1
        assert kept[0].price == 20.0
        assert kept[0].price_note == ""
        assert kept[0].spec_adjustments == []

    def test_keeps_comps_whose_spec_cannot_be_compared(self) -> None:
        """Regression: an unparsable or different-family spec used to delete
        the comp outright. Real vision output is full of "unspecified" and
        "40+ parts", and every one of those was quietly costing a comp."""
        for junk in ("2-pack", "unspecified", "40+ parts", "10-in-1"):
            kept = apply_guards(
                [comp("a", price=100.0, attrs={"capacity": junk})],
                item_attrs={"capacity": "4GB"},
            )
            assert len(kept) == 1, f"{junk!r} should not delete a comp"
            assert kept[0].price == 100.0, f"{junk!r} should not move the price"

    def test_scales_a_pack_count_difference(self) -> None:
        """The dimension the user could not see working: a 3-pack comp is a
        usable signal for a single unit once divided down."""
        kept = apply_guards(
            [comp("a", price=30.0, attrs={"pack_count": "3-pack"})],
            item_attrs={"pack_count": "1-pack"},
        )
        assert len(kept) == 1
        assert kept[0].price is not None and kept[0].price < 15.0
        assert kept[0].spec_adjustments[0].feature == "pack count"

    def test_scales_a_cable_length_difference(self) -> None:
        kept = apply_guards(
            [comp("a", price=20.0, attrs={"length": "15ft"})],
            item_attrs={"length": "6ft"},
        )
        assert len(kept) == 1
        assert kept[0].price is not None and kept[0].price < 20.0
        assert kept[0].spec_adjustments[0].feature == "length"

    def test_form_factor_difference_is_shown_but_never_priced(self) -> None:
        kept = apply_guards(
            [comp("a", price=20.0, attrs={"form_factor": "360mm"})],
            item_attrs={"form_factor": "120mm"},
        )
        assert len(kept) == 1
        assert kept[0].price == 20.0
        assert kept[0].price_note == ""
        assert kept[0].spec_adjustments[0].scaled is False


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


class TestMatchedPrices:
    """The model-aware version: prefers same-model+same-condition, then
    same-condition, then everything -- and reports which tier it actually
    used, so the caller can tell the user their band isn't blended across
    conditions/models when it isn't, and *is* when it is.
    """

    def test_prefers_model_and_condition_when_enough_data(self) -> None:
        comps = [
            comp(
                "m1", price=50.0, condition=Condition.USABLE, attrs={"model": "Aegis Secure Key 3"}
            ),
            comp(
                "m2", price=55.0, condition=Condition.USABLE, attrs={"model": "Aegis Secure Key 3"}
            ),
            comp(
                "other-model",
                price=200.0,
                condition=Condition.USABLE,
                attrs={"model": "Aegis Padlock"},
            ),
        ]
        result = matched_prices(
            comps, condition_value="usable", model_value="Aegis Secure Key 3", min_comps=2
        )
        assert result.tier == "model+condition"
        assert sorted(result.prices) == [50.0, 55.0]

    def test_falls_back_to_condition_when_model_bucket_too_small(self) -> None:
        comps = [
            comp(
                "m1", price=50.0, condition=Condition.USABLE, attrs={"model": "Aegis Secure Key 3"}
            ),
            comp(
                "c1", price=60.0, condition=Condition.USABLE, attrs={"model": "Aegis Secure Key 4"}
            ),
            comp("c2", price=65.0, condition=Condition.USABLE, attrs={}),
        ]
        result = matched_prices(
            comps, condition_value="usable", model_value="Aegis Secure Key 3", min_comps=2
        )
        assert result.tier == "condition"
        assert sorted(result.prices) == [50.0, 60.0, 65.0]

    def test_falls_back_to_all_when_condition_bucket_too_small(self) -> None:
        comps = [
            comp("a", price=10.0, condition=Condition.CLEAN),
            comp("b", price=20.0, condition=Condition.ROUGH),
        ]
        result = matched_prices(comps, condition_value="usable", min_comps=1)
        assert result.tier == "all"
        assert sorted(result.prices) == [10.0, 20.0]

    def test_no_model_value_skips_the_narrow_tier(self) -> None:
        comps = [
            comp("a", price=10.0, condition=Condition.USABLE, attrs={"model": "X"}),
            comp("b", price=20.0, condition=Condition.USABLE, attrs={"model": "Y"}),
        ]
        result = matched_prices(comps, condition_value="usable", min_comps=1)
        assert result.tier == "condition"

    def test_condition_matched_prices_shim_matches_the_condition_tier(self) -> None:
        """Back-compat: the plain function returns exactly matched_prices's
        .prices, without the caller needing to know about tiers at all."""
        comps = [
            comp("a", price=10.0, condition=Condition.USABLE),
            comp("b", price=20.0, condition=Condition.CLEAN),
        ]
        assert (
            condition_matched_prices(comps, "usable", min_comps=1)
            == matched_prices(comps, condition_value="usable", min_comps=1).prices
        )


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
