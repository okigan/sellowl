"""Thin async Apify client: run an actor, wait, return dataset items."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import httpx

from ..cache import DEFAULT_CACHE_DIR, cache_get, cache_get_stale, cache_key, cache_set
from ..logging import get_logger

log = get_logger(__name__)

BASE = "https://api.apify.com/v2"
_TERMINAL_OK = {"SUCCEEDED"}
_TERMINAL_BAD = {"FAILED", "ABORTED", "TIMED-OUT"}


class ApifyError(RuntimeError):
    pass


class ApifyClient:
    def __init__(
        self,
        token: str,
        timeout_s: float = 300.0,
        *,
        cache_ttl_s: float = 0.0,
        cache_dir: Path = DEFAULT_CACHE_DIR,
    ) -> None:
        if not token:
            raise ApifyError("APIFY_TOKEN is not set — fill it in sellowl/.env")
        self._token = token
        self._timeout_s = timeout_s
        self._cache_ttl_s = cache_ttl_s
        self._cache_dir = cache_dir

    async def run_actor(
        self,
        actor: str,
        payload: dict[str, Any],
        *,
        max_items: int | None = None,
    ) -> list[dict[str, Any]]:
        """Run an actor to completion and return its dataset items.

        `actor` is a slug like "apify/facebook-marketplace-scraper"; the API
        wants the tilde form. Runs take minutes and are idempotent for a given
        (actor, payload, max_items), so identical calls are served from an
        on-disk cache when `cache_ttl_s` is set — see `..cache`.
        """
        key = cache_key("apify_run_actor", actor, payload, max_items)
        if self._cache_ttl_s > 0:
            cached = cache_get(key, ttl_seconds=self._cache_ttl_s, cache_dir=self._cache_dir)
            if cached is not None:
                return list(cached)

        try:
            rows = await self._run_live(actor, payload, max_items)
        except Exception:
            # A platform-level outage or account limit (a monthly quota, a
            # disabled feature) won't be fixed by retrying, but a cached
            # result from an earlier successful run — even stale — is still
            # real data and better than failing the whole job. Only used as
            # a last resort: the fresh live attempt above always runs first.
            stale = (
                cache_get_stale(key, cache_dir=self._cache_dir) if self._cache_ttl_s > 0 else None
            )
            if stale is None:
                raise
            log.warning("apify_call_failed_serving_stale_cache", actor=actor)
            return list(stale)

        if self._cache_ttl_s > 0:
            cache_set(key, rows, cache_dir=self._cache_dir)
        return rows

    async def _run_live(
        self, actor: str, payload: dict[str, Any], max_items: int | None
    ) -> list[dict[str, Any]]:
        slug = actor.replace("/", "~")
        params: dict[str, str] = {"token": self._token}
        async with httpx.AsyncClient(timeout=60.0) as client:
            start = await client.post(f"{BASE}/acts/{slug}/runs", params=params, json=payload)
            if start.status_code >= 400:
                raise ApifyError(f"{actor}: start failed {start.status_code} {start.text[:300]}")
            run = start.json()["data"]
            run_id = run["id"]
            dataset_id = run["defaultDatasetId"]

            status = await self._wait(client, run_id, params, actor)
            if status not in _TERMINAL_OK:
                raise ApifyError(f"{actor}: run ended {status}")

            ds_params: dict[str, str] = {**params, "clean": "true", "format": "json"}
            if max_items is not None:
                ds_params["limit"] = str(max_items)
            items = await client.get(f"{BASE}/datasets/{dataset_id}/items", params=ds_params)
            items.raise_for_status()
            data = items.json()
        if not isinstance(data, list):
            raise ApifyError(f"{actor}: dataset was not a list")
        log.info("actor_run_complete", actor=actor, items=len(data))
        return [row for row in data if isinstance(row, dict)]

    async def _wait(
        self,
        client: httpx.AsyncClient,
        run_id: str,
        params: dict[str, str],
        actor: str,
    ) -> str:
        """Poll until terminal.

        'Still running' and 'failed' are different outcomes and the caller
        needs to be able to tell them apart.
        """
        deadline = asyncio.get_running_loop().time() + self._timeout_s
        delay = 2.0
        while True:
            resp = await client.get(f"{BASE}/actor-runs/{run_id}", params=params)
            resp.raise_for_status()
            status = str(resp.json()["data"]["status"])
            if status in _TERMINAL_OK or status in _TERMINAL_BAD:
                return status
            if asyncio.get_running_loop().time() > deadline:
                raise ApifyError(f"{actor}: still {status} after {self._timeout_s:.0f}s")
            await asyncio.sleep(delay)
            delay = min(delay * 1.4, 10.0)


async def fetch_bytes(url: str, *, timeout_s: float = 20.0) -> bytes | None:
    """Fetch an image.

    Facebook CDN URLs carry an `oe=` expiry, so photos must be fetched during
    ingest and handed straight to the vision call. Returns None rather than
    raising: a missing photo degrades one comp, it does not fail a job.
    """
    if not url:
        return None
    try:
        async with httpx.AsyncClient(timeout=timeout_s, follow_redirects=True) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            return resp.content
    except (httpx.HTTPError, ValueError) as exc:
        log.warning("photo_fetch_failed", url=url[:80], error=str(exc))
        return None
