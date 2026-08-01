"""Parsers, against recorded actor output.

Fixtures double as documentation of the real output shapes, which the actor
READMEs get wrong. Tests never hit live actors: they cost money and they flake.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from sellowl.models import Condition, Venue
from sellowl.sources.local import local_actor_payload, parse_local_comps, search_url
from sellowl.sources.parse import as_date, as_price, as_text, dig, first
from sellowl.sources.sold import (
    condition_from_text,
    parse_sold_comps,
    sold_actor_payload,
    sold_search_url,
)
from sellowl.sources.store import (
    parse_store_items,
    seller_from_url,
    seller_search_url,
    store_actor_payload,
    upstream_error,
)

FIXTURES = Path(__file__).parent / "fixtures"


def load(name: str) -> list[dict[str, Any]]:
    data = json.loads((FIXTURES / name).read_text())
    assert isinstance(data, list)
    return data


class TestDig:
    def test_nested(self) -> None:
        assert dig({"a": {"b": {"c": 1}}}, "a.b.c") == 1

    def test_list_index(self) -> None:
        assert dig({"images": [{"url": "x"}]}, "images.0.url") == "x"

    def test_missing_returns_none(self) -> None:
        assert dig({"a": 1}, "a.b.c") is None
        assert dig({}, "nope") is None

    def test_out_of_range_index(self) -> None:
        assert dig({"a": []}, "a.5") is None

    def test_non_digit_index_into_list(self) -> None:
        assert dig({"a": [1, 2]}, "a.key") is None

    def test_first_skips_empties(self) -> None:
        assert first({"a": "", "b": [], "c": "found"}, "a", "b", "c") == "found"

    def test_first_all_missing(self) -> None:
        assert first({}, "a", "b") is None


class TestAsPrice:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (65, 65.0),
            (65.5, 65.5),
            ("65.00", 65.0),
            ("$1,234.56", 1234.56),
            ("US $20.00", 20.0),
            ({"value": "99.99"}, 99.99),
            ({"amount": "240.00"}, 240.0),
            ({"formatted_amount": "$65"}, 65.0),
            (None, None),
            ("", None),
            ("free", None),
            (0, None),
            (-5, None),
            (True, None),
            ({}, None),
        ],
    )
    def test_shapes(self, value: Any, expected: float | None) -> None:
        assert as_price(value) == expected


class TestAsDate:
    def test_iso_with_z(self) -> None:
        assert as_date("2026-07-14T00:00:00Z") == datetime(2026, 7, 14, tzinfo=UTC)

    def test_epoch_seconds(self) -> None:
        parsed = as_date(1751500800)
        assert parsed is not None and parsed.year == 2025

    def test_epoch_millis(self) -> None:
        parsed = as_date(1751500800000)
        assert parsed is not None and parsed.year == 2025

    def test_human_format(self) -> None:
        assert as_date("Jun 20, 2026") == datetime(2026, 6, 20, tzinfo=UTC)

    def test_garbage_is_none(self) -> None:
        assert as_date("not a date") is None
        assert as_date(None) is None
        assert as_date(True) is None

    def test_always_tz_aware(self) -> None:
        parsed = as_date("2026-07-14")
        assert parsed is not None and parsed.tzinfo is not None


class TestAsText:
    def test_strips(self) -> None:
        assert as_text("  hi  ") == "hi"

    def test_list_takes_first(self) -> None:
        assert as_text(["a", "b"]) == "a"

    def test_none_is_empty(self) -> None:
        assert as_text(None) == ""


class TestStoreParsing:
    def test_parses_the_real_shape(self) -> None:
        items = parse_store_items(load("ebay_store.json"), "https://ebay.com/usr/x", limit=10)
        by_id = {i.external_id: i for i in items}
        assert by_id["800284334679"].ask_price == 14.99
        assert by_id["800284334679"].photo_url.endswith("s-l225.jpg")
        assert by_id["306499332211"].ask_price == 12.0

    def test_parses_the_sellers_own_condition(self) -> None:
        """Real signal available even without vision — see Item.listed_condition."""
        items = parse_store_items(load("ebay_store.json"), "https://ebay.com/usr/x", limit=10)
        by_id = {i.external_id: i for i in items}
        assert by_id["800284334679"].listed_condition is Condition.CLEAN  # "Brand new"
        assert by_id["306499332211"].listed_condition is Condition.USABLE  # "Pre-owned"

    def test_prefers_numeric_price_over_display_string(self) -> None:
        items = parse_store_items(
            [{"title": "x", "price": "$1,999.00", "priceValue": 12.5}], "s", 5
        )
        assert items[0].ask_price == 12.5

    def test_skips_titleless_rows(self) -> None:
        items = parse_store_items(load("ebay_store.json"), "s", limit=10)
        assert all(i.title for i in items)
        assert "307021352999" not in {i.external_id for i in items}

    def test_respects_limit(self) -> None:
        assert len(parse_store_items(load("ebay_store.json"), "s", limit=2)) == 2

    def test_empty_input(self) -> None:
        assert parse_store_items([], "s", limit=10) == []


class TestSellerExtraction:
    @pytest.mark.parametrize(
        ("url", "expected"),
        [
            ("https://www.ebay.com/usr/pragm_14", "pragm_14"),
            ("https://www.ebay.com/usr/pragm_14?_tab=shop", "pragm_14"),
            ("https://www.ebay.com/str/mycoolstore", "mycoolstore"),
            ("https://www.ebay.com/sch/i.html?_ssn=pragm_14&_sop=12", "pragm_14"),
            ("https://www.ebay.com/", None),
            ("", None),
        ],
    )
    def test_extracts(self, url: str, expected: str | None) -> None:
        assert seller_from_url(url) == expected

    def test_search_url_filters_by_seller(self) -> None:
        assert "_ssn=pragm_14" in seller_search_url("pragm_14")

    def test_payload_converts_profile_url_to_search_url(self) -> None:
        """The actor's `seller` field is a filter, not a source — it needs a
        startUrl or it fails with 'No input'."""
        payload = store_actor_payload("https://www.ebay.com/usr/pragm_14", 12)
        assert payload["startUrls"][0]["url"].startswith("https://www.ebay.com/sch/")
        assert "_ssn=pragm_14" in payload["startUrls"][0]["url"]
        assert payload["mode"] == "active"
        assert payload["maxItems"] == 12

    def test_payload_passes_unknown_urls_through(self) -> None:
        payload = store_actor_payload("https://www.ebay.com/b/some-category", 5)
        assert payload["startUrls"][0]["url"] == "https://www.ebay.com/b/some-category"


class TestUpstreamError:
    def test_detects_a_blocked_row(self) -> None:
        """Blocked actors return an error row and still exit SUCCEEDED."""
        rows = [
            {
                "type": "ebay_blocked",
                "reason": "upstream_error",
                "message": "eBay returned blocked or empty responses.",
            }
        ]
        assert upstream_error(rows) == "eBay returned blocked or empty responses."

    def test_clean_rows_report_nothing(self) -> None:
        assert upstream_error(load("ebay_store.json")) is None

    def test_empty_reports_nothing(self) -> None:
        assert upstream_error([]) is None


class TestSoldParsing:
    def test_parses_the_real_shape(self) -> None:
        comps = {c.external_id: c for c in parse_sold_comps(load("ebay_sold.json"), job_id="j1")}
        assert comps["366309179118"].price == 39.99
        assert comps["366309179118"].photo_url.endswith("s-l225.jpg")
        assert comps["366309179119"].price == 11.87

    def test_parses_the_sold_date_prefix(self) -> None:
        """memo23 emits 'Sold  4 Jun 2026' — note the literal prefix."""
        comps = {c.external_id: c for c in parse_sold_comps(load("ebay_sold.json"))}
        sold_at = comps["366309179118"].sold_at
        assert sold_at is not None
        assert (sold_at.year, sold_at.month, sold_at.day) == (2026, 6, 4)

    def test_skips_unusable_rows(self) -> None:
        comps = parse_sold_comps(load("ebay_sold.json"))
        assert all(c.price is not None and c.title for c in comps)
        assert len(comps) == 8

    def test_marks_venue_and_sold(self) -> None:
        for comp in parse_sold_comps(load("ebay_sold.json")):
            assert comp.venue is Venue.EBAY_SOLD
            assert comp.is_sold

    def test_maps_condition_vocabulary(self) -> None:
        comps = {c.external_id: c for c in parse_sold_comps(load("ebay_sold.json"))}
        assert comps["366309179119"].condition is Condition.ROUGH
        assert comps["366309179121"].condition is Condition.CLEAN
        assert comps["366309179118"].condition is Condition.USABLE

    def test_propagates_job_id(self) -> None:
        comps = parse_sold_comps(load("ebay_sold.json"), job_id="j42")
        assert all(c.job_id == "j42" for c in comps)


class TestConditionFromText:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("Brand new", Condition.CLEAN),
            ("New", Condition.CLEAN),
            ("Pre-owned", Condition.USABLE),
            ("For parts or not working", Condition.ROUGH),
            ("Used - good condition", Condition.USABLE),
            ("", Condition.UNKNOWN),
            ("indeterminate blob", Condition.UNKNOWN),
        ],
    )
    def test_mapping(self, text: str, expected: Condition) -> None:
        assert condition_from_text(text) is expected


class TestLocalParsing:
    def test_parses_the_known_shape(self) -> None:
        """`primary_listing_photo.photo_image_url` is the real, live-captured
        field — an earlier `image.uri` guess never matched a real actor run
        and silently produced blank thumbnails for every local comp."""
        comps = parse_local_comps(load("fb_marketplace.json"), job_id="j1")
        by_id = {c.external_id: c for c in comps}
        assert by_id["1189200412268517"].price == 240.0
        assert by_id["1189200412268517"].city == "Austin"
        assert by_id["1189200412268517"].state == "TX"
        assert by_id["1189200412268517"].photo_url.startswith("https://scontent")
        assert by_id["1189200412268517"].delivery == ["IN_PERSON"]

    def test_falls_back_to_the_older_nested_image_shape(self) -> None:
        comps = {c.external_id: c for c in parse_local_comps(load("fb_marketplace.json"))}
        assert comps["890679662817803"].photo_url.startswith("https://scontent")

    def test_handles_comma_formatted_price(self) -> None:
        comps = {c.external_id: c for c in parse_local_comps(load("fb_marketplace.json"))}
        assert comps["111"].price == 1250.0

    def test_skips_priceless_listings(self) -> None:
        comps = parse_local_comps(load("fb_marketplace.json"))
        assert "222" not in {c.external_id for c in comps}
        assert len(comps) == 3

    def test_venue_is_local(self) -> None:
        for comp in parse_local_comps(load("fb_marketplace.json")):
            assert comp.venue is Venue.FB_LOCAL

    def test_informal_titles_survive(self) -> None:
        """'wood dresser thing' is exactly why semantic matching is needed."""
        titles = {c.title for c in parse_local_comps(load("fb_marketplace.json"))}
        assert "wood dresser thing" in titles


class TestPayloads:
    def test_search_url_encodes(self) -> None:
        assert search_url("austin", "teak sideboard").endswith("query=teak+sideboard")

    def test_local_payload_is_one_query_per_run(self) -> None:
        """Not batched: maxItems is a global cap, so a batch lets the first
        query eat the whole budget and starve the rest."""
        payload = local_actor_payload("austin", "teak sideboard", per_query=10)
        assert len(payload["startUrls"]) == 1
        assert "austin" in payload["startUrls"][0]["url"]
        assert payload["resultsLimit"] == 10

    def test_sold_search_url_filters_to_completed_sales(self) -> None:
        url = sold_search_url("thermaltake riing 12")
        assert "LH_Sold=1" in url
        assert "LH_Complete=1" in url
        assert "thermaltake+riing+12" in url

    def test_sold_payload_is_one_query_per_run(self) -> None:
        payload = sold_actor_payload("thermaltake riing 12", 40, days_back=90)
        assert len(payload["startUrls"]) == 1
        assert payload["mode"] == "sold"
        assert payload["maxItems"] == 40
        assert payload["maxDaysBack"] == 90
