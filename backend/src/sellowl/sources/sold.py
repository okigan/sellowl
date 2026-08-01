"""eBay sold listings -> Comp models.

Sold prices are the only real number in this app: asking prices are what
sellers hope for, sold prices are what the thing fetches.

Actor: `memo23/ebay-search-scraper-ppe` in `mode: "sold"` — the same actor that
reads the store, which keeps one fewer third-party shape to reverse-engineer.

One run per query, not one run for all of them: `maxItems` is a *global* cap,
so batching several searches into one run lets the first query eat the entire
budget and starve the rest. Verified — a two-query batch returned 16 rows for
one query and 0 for the other.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote_plus

from ..models import Comp, Condition, Venue
from .parse import as_date, as_price, as_text, as_url, first

_TITLE = ("title", "product_title", "name", "basic_info.title")
_PRICE = ("priceValue", "price", "basic_info.priceValue", "soldPrice", "salePrice")
_URL = ("url", "itemUrl", "link", "basic_info.url")
_PHOTO = ("image", "imageUrl", "thumbnail", "basic_info.image", "images.0.url", "images.0")
_ID = ("itemId", "listingId", "id", "basic_info.itemId")
_DATE = ("soldDate", "basic_info.soldDate", "dateSold", "endTime", "soldAt", "endDate")
_COND = ("condition", "basic_info.condition", "itemCondition", "conditionDisplayName")

# eBay's condition vocabulary folded into our three buckets. Vision overrides
# this during rerank; it is only a prior.
_CONDITION_HINTS: dict[str, Condition] = {
    "new with tags": Condition.CLEAN,
    "new without tags": Condition.CLEAN,
    "new (other)": Condition.CLEAN,
    "brand new": Condition.CLEAN,
    "open box": Condition.CLEAN,
    "excellent": Condition.CLEAN,
    "very good": Condition.CLEAN,
    "new": Condition.CLEAN,
    "good": Condition.USABLE,
    "pre-owned": Condition.USABLE,
    "preowned": Condition.USABLE,
    "used": Condition.USABLE,
    "acceptable": Condition.USABLE,
    "refurbished": Condition.USABLE,
    "for parts or not working": Condition.ROUGH,
    "parts only": Condition.ROUGH,
    "not working": Condition.ROUGH,
    "salvage": Condition.ROUGH,
    "damaged": Condition.ROUGH,
    "broken": Condition.ROUGH,
}


def condition_from_text(value: str) -> Condition:
    text = value.strip().lower()
    if not text:
        return Condition.UNKNOWN
    if text in _CONDITION_HINTS:
        return _CONDITION_HINTS[text]
    # Longest key first so "new with tags" wins over bare "new".
    for key in sorted(_CONDITION_HINTS, key=len, reverse=True):
        if key in text:
            return _CONDITION_HINTS[key]
    return Condition.UNKNOWN


def sold_search_url(query: str) -> str:
    """eBay sold+completed search, most recent first."""
    return (
        f"https://www.ebay.com/sch/i.html?_nkw={quote_plus(query)}"
        "&LH_Sold=1&LH_Complete=1&_sop=13&_ipg=60"
    )


def sold_actor_payload(query: str, limit: int, days_back: int = 90) -> dict[str, Any]:
    """Payload for ONE query. Callers fan out; see the module docstring."""
    return {
        "startUrls": [{"url": sold_search_url(query)}],
        "mode": "sold",
        "maxItems": limit,
        "detailedItems": False,
        "maxDaysBack": days_back,
    }


def parse_sold_comps(rows: list[dict[str, Any]], job_id: str = "") -> list[Comp]:
    comps: list[Comp] = []
    for row in rows:
        title = as_text(first(row, *_TITLE))
        price = as_price(first(row, *_PRICE))
        if not title or price is None:
            continue
        url = as_url(first(row, *_URL))
        comps.append(
            Comp(
                external_id=as_text(first(row, *_ID)) or url or title,
                venue=Venue.EBAY_SOLD,
                title=title,
                price=price,
                url=url,
                photo_url=as_url(first(row, *_PHOTO)),
                sold_at=as_date(first(row, *_DATE)),
                is_sold=True,
                condition=condition_from_text(as_text(first(row, *_COND))),
                job_id=job_id,
            )
        )
    return comps
