"""Your eBay storefront -> Item models.

Tier 1 needs no eBay credentials: the storefront is scraped, not API-read.

Actor: `memo23/ebay-search-scraper-ppe`. Its `seller` field is a *filter*, not
a source — passing it alone fails with "No input". The working shape is a
seller-search `startUrls` entry, which is what `store_actor_payload` builds.
Verified against a live store; see docs/DEVELOP.md § Actor notes.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import parse_qs, urlparse

from ..models import Item
from .parse import as_price, as_text, as_url, first

# memo23 emits both a display string ("$14.99") and a numeric `priceValue`.
# Prefer the number; fall back to parsing the string.
_TITLE = ("title", "product_title", "name", "itemTitle", "basic_info.title")
_PRICE = ("priceValue", "price", "basic_info.priceValue", "basic_info.price", "currentPrice")
_URL = ("url", "product_url", "itemUrl", "link", "basic_info.url")
_PHOTO = (
    "image",
    "image_url",
    "imageUrl",
    "thumbnail",
    "basic_info.image",
    "images.0.url",
    "images.0",
)
_ID = ("itemId", "listingId", "id", "basic_info.itemId", "legacyItemId")

_SELLER_PATTERNS = (
    re.compile(r"/usr/([^/?#]+)", re.I),
    re.compile(r"/str/([^/?#]+)", re.I),
    re.compile(r"/sch/([^/?#]+)/m\.html", re.I),
)


def seller_from_url(store_url: str) -> str | None:
    """Pull the seller username out of whatever eBay URL the user pasted.

    Handles /usr/<name>, /str/<name>, and ?_ssn=<name>.
    """
    if not store_url:
        return None
    text = store_url.strip()
    query = parse_qs(urlparse(text).query)
    for key in ("_ssn", "_sasl", "_saslop"):
        values = query.get(key)
        if values and values[0].strip():
            return values[0].strip()
    for pattern in _SELLER_PATTERNS:
        match = pattern.search(text)
        if match:
            return match.group(1).strip()
    return None


def seller_search_url(seller: str) -> str:
    """eBay's canonical 'everything this seller has listed' search."""
    return f"https://www.ebay.com/sch/i.html?_ssn={seller}&_sop=12&_ipg=60"


def store_actor_payload(store_url: str, limit: int) -> dict[str, Any]:
    seller = seller_from_url(store_url)
    url = seller_search_url(seller) if seller else store_url
    return {
        "startUrls": [{"url": url}],
        "mode": "active",
        "maxItems": limit,
        "detailedItems": False,
    }


def parse_store_items(rows: list[dict[str, Any]], store_url: str, limit: int) -> list[Item]:
    items: list[Item] = []
    for row in rows:
        title = as_text(first(row, *_TITLE))
        if not title:
            continue
        url = as_url(first(row, *_URL))
        items.append(
            Item(
                external_id=as_text(first(row, *_ID)) or url or title,
                title=title,
                ask_price=as_price(first(row, *_PRICE)),
                url=url,
                photo_url=as_url(first(row, *_PHOTO)),
                store_url=store_url,
            )
        )
        if len(items) >= limit:
            break
    return items


def upstream_error(rows: list[dict[str, Any]]) -> str | None:
    """Detect an actor that reported failure as a data row.

    Blocked-scraper actors often return `{"type": "ebay_blocked", ...}` and exit
    SUCCEEDED, which would otherwise surface as the useless "no parseable
    listings". Surfacing their own message saves ten minutes of confusion.
    """
    for row in rows:
        kind = str(row.get("type") or row.get("error") or "").lower()
        if "block" in kind or "error" in kind:
            message = as_text(first(row, "message", "reason", "error"))
            return message or f"actor reported {kind}"
    return None
