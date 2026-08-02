"""What the pipeline needs from a data source, independent of who provides it.

`jobs.py` used to import `ApifyClient` and Apify's payload builders directly,
which made "stop using Apify" a change to the pipeline rather than a change of
implementation. It now depends on these three methods, so a replacement is a
new class and a config value.

The three legs are deliberately separate because they are *not* equally
replaceable, and pretending otherwise would hide the real constraint:

- `store_listings` -- a seller's own active listings. eBay's official Browse
  API can do this (`filter=sellers:{username}`), free within a generous daily
  quota. Genuinely replaceable today.
- `sold_comps` -- **the load-bearing one, and the blocker.** The Browse API
  does not expose sold/completed listings at all; that lives behind the
  Marketplace Insights API, which is limited-release. Everything this app
  claims rests on sold prices being real transactions rather than asking
  prices, so substituting active listings here would quietly convert the
  trustworthy half of the data into the untrustworthy kind. Better to keep
  scraping, or to have no sold data and say so, than to swap in asks and not
  say so.
- `local_comps` -- Facebook Marketplace. No official API exists. Any
  replacement is a scraper we then own and maintain.

A source returns raw rows; parsing into models stays with the source module
that knows the shape (see DEVELOP.md, "parse third-party JSON at the edge").
"""

from __future__ import annotations

from typing import Any, Protocol


class CompSource(Protocol):
    """Rows in, nothing clever. Implementations own their own auth and retries."""

    async def store_listings(self, store_url: str, limit: int) -> list[dict[str, Any]]:
        """The seller's own active listings."""
        ...

    async def sold_comps(self, query: str, limit: int, days_back: int) -> list[dict[str, Any]]:
        """Completed/sold listings matching a query."""
        ...

    async def local_comps(self, metro: str, query: str, limit: int) -> list[dict[str, Any]]:
        """Local marketplace listings (asking prices) for a metro."""
        ...
