"""The self-hosted eBay scraper's pure parts.

The browser itself isn't exercised here (that needs a live site); these cover
the two transforms that broke real runs.
"""

from __future__ import annotations

from sellowl.sources.browser import _usable, as_jpeg, parse_fb_card


class TestAsJpeg:
    def test_rewrites_ebay_webp_to_jpeg(self) -> None:
        """The vision model rejects WebP ("Failed to load image or audio
        file"). eBay serves WebP to a modern browser and JPEG to the old
        Apify actor, so this only appeared after the migration -- every
        photo silently failed to grade."""
        assert (
            as_jpeg("https://i.ebayimg.com/images/g/gwIAAeSwWJxqTcdg/s-l500.webp")
            == "https://i.ebayimg.com/images/g/gwIAAeSwWJxqTcdg/s-l500.jpg"
        )

    def test_leaves_jpeg_alone(self) -> None:
        url = "https://i.ebayimg.com/images/g/abc/s-l225.jpg"
        assert as_jpeg(url) == url

    def test_leaves_other_hosts_alone(self) -> None:
        url = "https://scontent.xx.fbcdn.net/v/whatever.webp"
        assert as_jpeg(url) == url

    def test_empty_is_safe(self) -> None:
        assert as_jpeg("") == ""


class TestUsable:
    def test_rejects_ebays_placeholder_promo_cards(self) -> None:
        """Every result page carries a "Shop on eBay" card with a fake
        /itm/123456 link; priced and parsed, it would be a fake comp."""
        assert not _usable({"itemId": "123456", "title": "Shop on eBay"})

    def test_rejects_rows_without_an_item_id(self) -> None:
        assert not _usable({"itemId": "", "title": "A real listing"})

    def test_accepts_a_real_listing(self) -> None:
        assert _usable({"itemId": "800288494964", "title": "Makeblock Inventor Kit"})


class TestParseFbCard:
    """Facebook gives no stable class names, so fields are classified by
    shape. These pin the shapes seen on real result pages."""

    def test_typical_card(self) -> None:
        row = parse_fb_card(
            {
                "id": "1554075522903934",
                "lines": ["$325", "Unifi Dream Machine Pro", "Austin, TX"],
                "url": "https://www.facebook.com/marketplace/item/1554075522903934",
                "image": "https://scontent.example/x.jpg",
            }
        )
        assert row is not None
        assert row["marketplace_listing_title"] == "Unifi Dream Machine Pro"
        assert row["listing_price"]["amount"] == "325"
        assert row["location"]["reverse_geocode"] == {"city": "Austin", "state": "TX"}

    def test_markdown_uses_the_current_price_not_the_struck_out_one(self) -> None:
        """A discounted listing shows both; the first is what it sells for
        now, and taking the wrong one would inflate every local band."""
        row = parse_fb_card(
            {"id": "1", "lines": ["$50", "$85", "UniFi Switch 8", "Liberty Hill, TX"]}
        )
        assert row is not None
        assert row["listing_price"]["amount"] == "50"

    def test_drops_the_partner_listing_badge(self) -> None:
        row = parse_fb_card(
            {
                "id": "1",
                "lines": ["Partner listing", "$9.99", "Weather Cover", "Citrus Heights, CA"],
            }
        )
        assert row is not None
        assert row["marketplace_listing_title"] == "Weather Cover"

    def test_comma_prices(self) -> None:
        row = parse_fb_card({"id": "1", "lines": ["$1,250", "Server Rack", "Austin, TX"]})
        assert row is not None
        assert row["listing_price"]["amount"] == "1250"

    def test_no_price_is_dropped(self) -> None:
        assert parse_fb_card({"id": "1", "lines": ["Free", "Old Router", "Austin, TX"]}) is None

    def test_no_title_is_dropped(self) -> None:
        assert parse_fb_card({"id": "1", "lines": ["$20", "Austin, TX"]}) is None
