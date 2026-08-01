"""Facebook Marketplace -> Comp models.

This is the one actor whose output shape we actually know, from the store
listing sample. Quirks that shape the code (docs/DESIGN.md § FB specifics):

- No description field. Title + photo is everything, which is why vision is
  core rather than decorative.
- No post date, so no staleness signal. Don't promise one.
- No lat/lon — city/state strings only. "Local" means the metro, not a radius.
- Photo URLs carry an `oe=` expiry; fetch bytes at ingest.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote_plus

from ..models import Comp, Venue
from .parse import as_price, as_text, as_url, first

MARKETPLACE_BASE = "https://www.facebook.com/marketplace"


def search_url(metro: str, query: str) -> str:
    return f"{MARKETPLACE_BASE}/{metro}/search/?query={quote_plus(query)}"


def parse_local_comps(rows: list[dict[str, Any]], job_id: str = "") -> list[Comp]:
    comps: list[Comp] = []
    for row in rows:
        title = as_text(first(row, "marketplace_listing_title", "custom_title", "title"))
        price = as_price(
            first(row, "listing_price.amount", "listing_price.formatted_amount", "price")
        )
        if not title or price is None:
            continue
        external_id = as_text(first(row, "id", "listingUrl", "listing_url"))
        delivery = row.get("delivery_types")
        comps.append(
            Comp(
                external_id=external_id or title,
                venue=Venue.FB_LOCAL,
                title=title,
                price=price,
                url=as_text(first(row, "listingUrl", "listing_url", "url")),
                photo_url=as_url(
                    first(row, "primary_listing_photo.image.uri", "image.uri", "photo")
                ),
                city=as_text(first(row, "location.reverse_geocode.city", "city")),
                state=as_text(first(row, "location.reverse_geocode.state", "state")),
                delivery=[as_text(d) for d in delivery] if isinstance(delivery, list) else [],
                is_sold=bool(row.get("is_sold", False)),
                job_id=job_id,
            )
        )
    return comps


def local_actor_payload(metro: str, query: str, per_query: int) -> dict[str, Any]:
    """Payload for ONE query.

    Like the sold source, callers fan out rather than batching: a shared
    results cap lets one search consume the whole budget.
    """
    return {
        "startUrls": [{"url": search_url(metro, query)}],
        "resultsLimit": per_query,
        "maxItems": per_query,
    }
