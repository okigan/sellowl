"""Retrieval fusion and the drift guards.

Semantic search will match a photo of a guitar to every guitar. These are the
tests for the code that stops that becoming a price band.
"""

from __future__ import annotations

from sellowl.match import (
    apply_guards,
    attributes_agree,
    build_fallback_queries,
    build_rrf_query,
    condition_matched_prices,
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
