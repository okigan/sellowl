"""The self-hosted eBay scraper's pure parts.

The browser itself isn't exercised here (that needs a live site); these cover
the two transforms that broke real runs.
"""

from __future__ import annotations

from sellowl.sources.browser import _usable, as_jpeg


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
