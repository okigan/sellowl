"""Apify as one implementation of `CompSource`, not as the pipeline's assumption.

All the Apify-specific knowledge -- actor slugs, payload shapes, the fact that
`maxItems` is a global per-run cap -- stays here. The pipeline sees three
methods returning rows.
"""

from __future__ import annotations

from typing import Any

from ..config import Settings
from ..logging import get_logger
from .apify import ApifyClient
from .local import local_actor_payload
from .sold import sold_actor_payload
from .store import store_actor_payload

log = get_logger(__name__)


class ApifyCompSource:
    def __init__(self, settings: Settings, client: ApifyClient | None = None) -> None:
        self._s = settings
        self._client = client or ApifyClient(
            settings.apify_token,
            timeout_s=settings.apify_timeout_s,
            cache_ttl_s=settings.apify_cache_ttl_hours * 3600,
        )

    async def store_listings(self, store_url: str, limit: int) -> list[dict[str, Any]]:
        return await self._client.run_actor(
            self._s.actor_store,
            store_actor_payload(store_url, limit),
            max_items=limit,
        )

    async def sold_comps(self, query: str, limit: int, days_back: int) -> list[dict[str, Any]]:
        return await self._client.run_actor(
            self._s.actor_sold,
            sold_actor_payload(query, limit, days_back),
            max_items=limit,
        )

    async def local_comps(self, metro: str, query: str, limit: int) -> list[dict[str, Any]]:
        return await self._client.run_actor(
            self._s.actor_local,
            local_actor_payload(metro, query, limit),
            max_items=limit,
        )
