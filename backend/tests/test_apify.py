"""ApifyClient over mocked HTTP (respx) -- no real Apify calls in tests.

Covers the stale-cache fallback specifically: a platform-level outage or
account limit (a monthly quota, a disabled feature) isn't fixed by retrying,
but a cached result from an earlier successful run is still real data and
better than failing the whole job.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest
import respx

from sellowl.cache import cache_key, cache_set
from sellowl.sources.apify import ApifyClient, ApifyError

RUN_ID = "run123"
DATASET_ID = "ds456"
QUOTA_ERROR_BODY = {
    "error": {
        "type": "platform-feature-disabled",
        "message": "Monthly usage hard limit exceeded",
    }
}


def mock_success(router: respx.MockRouter, actor_slug: str, rows: list[object]) -> None:
    router.post(f"https://api.apify.com/v2/acts/{actor_slug}/runs").mock(
        return_value=httpx.Response(
            201, json={"data": {"id": RUN_ID, "defaultDatasetId": DATASET_ID}}
        )
    )
    router.get(f"https://api.apify.com/v2/actor-runs/{RUN_ID}").mock(
        return_value=httpx.Response(200, json={"data": {"status": "SUCCEEDED"}})
    )
    router.get(f"https://api.apify.com/v2/datasets/{DATASET_ID}/items").mock(
        return_value=httpx.Response(200, json=rows)
    )


def mock_quota_exceeded(router: respx.MockRouter, actor_slug: str) -> None:
    router.post(f"https://api.apify.com/v2/acts/{actor_slug}/runs").mock(
        return_value=httpx.Response(403, json=QUOTA_ERROR_BODY)
    )


class TestRunActorLive:
    @respx.mock
    async def test_returns_dataset_rows(self) -> None:
        mock_success(respx.mock, "some~actor", [{"title": "a"}, {"title": "b"}])
        client = ApifyClient("token")
        rows = await client.run_actor("some/actor", {"q": "x"})
        assert rows == [{"title": "a"}, {"title": "b"}]

    @respx.mock
    async def test_non_dict_rows_are_dropped(self) -> None:
        mock_success(respx.mock, "some~actor", [{"title": "a"}, "garbage", 123])
        client = ApifyClient("token")
        rows = await client.run_actor("some/actor", {})
        assert rows == [{"title": "a"}]

    @respx.mock
    async def test_start_failure_raises(self) -> None:
        mock_quota_exceeded(respx.mock, "some~actor")
        client = ApifyClient("token")
        with pytest.raises(ApifyError, match="start failed 403"):
            await client.run_actor("some/actor", {})

    @respx.mock
    async def test_run_ends_failed_raises(self) -> None:
        respx.mock.post("https://api.apify.com/v2/acts/some~actor/runs").mock(
            return_value=httpx.Response(
                201, json={"data": {"id": RUN_ID, "defaultDatasetId": DATASET_ID}}
            )
        )
        respx.mock.get(f"https://api.apify.com/v2/actor-runs/{RUN_ID}").mock(
            return_value=httpx.Response(200, json={"data": {"status": "FAILED"}})
        )
        client = ApifyClient("token")
        with pytest.raises(ApifyError, match="run ended FAILED"):
            await client.run_actor("some/actor", {})


class TestCaching:
    @respx.mock
    async def test_cache_hit_skips_the_network_entirely(self, tmp_path: Path) -> None:
        actor, payload = "some/actor", {"q": "x"}
        cache_set(
            cache_key("apify_run_actor", actor, payload, None),
            [{"title": "cached"}],
            cache_dir=tmp_path,
        )
        # No HTTP mock registered for this actor -- a live call would error
        # against respx's default "no route matched" behavior.
        client = ApifyClient("token", cache_ttl_s=3600, cache_dir=tmp_path)
        rows = await client.run_actor(actor, payload)
        assert rows == [{"title": "cached"}]

    @respx.mock
    async def test_successful_live_call_populates_the_cache(self, tmp_path: Path) -> None:
        mock_success(respx.mock, "some~actor", [{"title": "fresh"}])
        client = ApifyClient("token", cache_ttl_s=3600, cache_dir=tmp_path)
        await client.run_actor("some/actor", {"q": "x"})
        assert any(tmp_path.glob("*.json"))


class TestStaleCacheFallback:
    @respx.mock
    async def test_falls_back_to_stale_cache_when_live_call_fails(self, tmp_path: Path) -> None:
        """The exact scenario this exists for: Apify returns a 403 monthly-
        quota error, but an earlier successful run for the same query is
        still sitting in the cache, past its normal TTL."""
        actor, payload = "some/actor", {"q": "x"}
        cache_set(
            cache_key("apify_run_actor", actor, payload, None),
            [{"title": "stale but real"}],
            cache_dir=tmp_path,
        )
        mock_quota_exceeded(respx.mock, "some~actor")

        # A short-lived TTL, and let real time lapse past it: the fresh
        # cache_get lookup at the top of run_actor now genuinely misses,
        # forcing the live path -- which fails and falls back to the same
        # entry, read ignoring its age this time.
        client = ApifyClient("token", cache_ttl_s=0.01, cache_dir=tmp_path)
        await asyncio.sleep(0.05)
        rows = await client.run_actor(actor, payload)
        assert rows == [{"title": "stale but real"}]

    @respx.mock
    async def test_raises_when_no_cache_exists_at_all(self, tmp_path: Path) -> None:
        mock_quota_exceeded(respx.mock, "some~actor")
        client = ApifyClient("token", cache_ttl_s=3600, cache_dir=tmp_path)
        with pytest.raises(ApifyError, match="start failed 403"):
            await client.run_actor("some/actor", {})
