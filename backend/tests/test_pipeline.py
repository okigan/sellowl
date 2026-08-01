"""End-to-end pipeline over recorded fixtures.

No network, no cluster, no API keys: fake Apify, disabled vision, in-memory
comp store. Proves the stages wire together and that a store URL becomes a
verdict.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from sellowl.config import Settings
from sellowl.jobs import JobRegistry, Pipeline, revise_payload
from sellowl.models import Condition, Item, JobStatus, VerdictKind

FIXTURES = Path(__file__).parent / "fixtures"


def load(name: str) -> list[dict[str, Any]]:
    return list(json.loads((FIXTURES / name).read_text()))


class FakeApify:
    """Serves the right fixture for each run.

    The store and sold legs now share one actor slug (memo23 does both), so
    dispatch is on the payload's `mode`, not the slug.
    """

    def __init__(self, *, ebay: str, local: str, fail: set[str] | None = None) -> None:
        self._ebay = ebay
        self._local = local
        self._fail = fail or set()
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def run_actor(
        self, actor: str, payload: dict[str, Any], *, max_items: int | None = None
    ) -> list[dict[str, Any]]:
        self.calls.append((actor, payload))
        mode = str(payload.get("mode", ""))
        key = f"{actor}:{mode}" if actor == self._ebay else actor
        if key in self._fail or actor in self._fail:
            raise RuntimeError(f"{key} exploded")
        if actor == self._local:
            return load("fb_marketplace.json")
        if mode == "sold":
            return load("ebay_sold.json")
        return load("ebay_store.json")


@pytest.fixture
def settings() -> Settings:
    return Settings(
        apify_token="fake",
        anthropic_api_key="",  # vision disabled -> title fallback
        elasticsearch_endpoint="",  # -> MemoryCompStore
        min_comps=3,
        max_items=10,
        metro="austin",
    )


@pytest.fixture
def patched(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> FakeApify:
    fake = FakeApify(ebay=settings.actor_store, local=settings.actor_local)
    monkeypatch.setattr("sellowl.jobs.ApifyClient", lambda *a, **k: fake)

    async def no_photos(url: str, **kwargs: Any) -> None:
        return None

    monkeypatch.setattr("sellowl.jobs.fetch_bytes", no_photos)
    return fake


async def run_job(settings: Settings) -> Any:
    registry = JobRegistry()
    job = registry.create("https://www.ebay.com/usr/pragm_14", settings.metro)
    await Pipeline(settings, registry).run(job)
    return job


class TestPipeline:
    async def test_completes(self, settings: Settings, patched: FakeApify) -> None:
        job = await run_job(settings)
        assert job.status is JobStatus.DONE, job.error
        assert job.error == ""

    async def test_produces_items_with_verdicts(
        self, settings: Settings, patched: FakeApify
    ) -> None:
        job = await run_job(settings)
        assert len(job.items) == 3  # fixture has 4 rows, one is unusable
        assert all(item.verdict is not None for item in job.items)

    async def test_the_underpriced_item_is_caught(
        self, settings: Settings, patched: FakeApify
    ) -> None:
        """The demo: a fan listed at $12 against a market clearing near $35."""
        job = await run_job(settings)
        fan = next(i for i in job.items if i.external_id == "306499332211")
        assert fan.verdict is not None
        assert fan.verdict.kind is VerdictKind.UNDERPRICED
        assert fan.verdict.target is not None
        assert fan.ask_price is not None
        assert fan.verdict.target > fan.ask_price
        assert fan.verdict.opportunity_usd is not None
        assert fan.verdict.opportunity_usd > 0

    async def test_condition_falls_back_to_the_sellers_own_listing(
        self, settings: Settings, patched: FakeApify
    ) -> None:
        """Vision is disabled in this fixture, but the store scrape's own
        "Pre-owned" condition string should still reach the verdict instead of
        every item defaulting to unknown."""
        job = await run_job(settings)
        fan = next(i for i in job.items if i.external_id == "306499332211")
        assert fan.condition is Condition.USABLE
        assert fan.vision is not None and fan.vision.condition is Condition.USABLE

    async def test_comps_are_attached_for_audit(
        self, settings: Settings, patched: FakeApify
    ) -> None:
        """Every verdict must be inspectable — a number nobody can audit is
        worse than no number."""
        job = await run_job(settings)
        fan = next(i for i in job.items if i.external_id == "306499332211")
        assert fan.comps
        assert all(c.price for c in fan.comps)

    async def test_one_comp_run_per_query_per_source(self) -> None:
        """Fan-out, not batching: a shared maxItems cap would let the first
        query consume the entire budget."""
        settings = Settings(
            apify_token="fake", anthropic_api_key="", elasticsearch_endpoint="", min_comps=3
        )
        registry = JobRegistry()
        job = registry.create("https://www.ebay.com/usr/pragm_14", "austin")
        fake = FakeApify(ebay=settings.actor_store, local=settings.actor_local)
        import sellowl.jobs as jobs_mod

        original_client, original_fetch = jobs_mod.ApifyClient, jobs_mod.fetch_bytes

        async def no_photos(url: str, **kwargs: Any) -> None:
            return None

        jobs_mod.ApifyClient = lambda *a, **k: fake  # type: ignore[assignment]
        jobs_mod.fetch_bytes = no_photos  # type: ignore[assignment]
        try:
            await Pipeline(settings, registry).run(job)
        finally:
            jobs_mod.ApifyClient = original_client  # type: ignore[assignment]
            jobs_mod.fetch_bytes = original_fetch  # type: ignore[assignment]

        n_items = len(job.items)
        sold_runs = [p for a, p in fake.calls if p.get("mode") == "sold"]
        local_runs = [p for a, p in fake.calls if a == settings.actor_local]
        assert len(sold_runs) == n_items
        assert len(local_runs) == n_items
        assert all(len(p["startUrls"]) == 1 for p in sold_runs + local_runs)

    async def test_local_runs_target_the_requested_metro(
        self, settings: Settings, patched: FakeApify
    ) -> None:
        await run_job(settings)
        payload = next(p for a, p in patched.calls if a == settings.actor_local)
        assert "austin" in payload["startUrls"][0]["url"]

    async def test_survives_one_dead_comp_source(
        self, settings: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Local scraper down should degrade the venue advice, not the job."""
        fake = FakeApify(
            ebay=settings.actor_store,
            local=settings.actor_local,
            fail={settings.actor_local},
        )
        monkeypatch.setattr("sellowl.jobs.ApifyClient", lambda *a, **k: fake)

        async def no_photos(url: str, **kwargs: Any) -> None:
            return None

        monkeypatch.setattr("sellowl.jobs.fetch_bytes", no_photos)
        job = await run_job(settings)
        assert job.status is JobStatus.DONE, job.error

    async def test_fails_loudly_when_store_returns_nothing(
        self, settings: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class Empty:
            async def run_actor(self, *a: Any, **k: Any) -> list[dict[str, Any]]:
                return []

        monkeypatch.setattr("sellowl.jobs.ApifyClient", lambda *a, **k: Empty())
        job = await run_job(settings)
        assert job.status is JobStatus.FAILED
        assert "ACTOR_STORE" in job.error or "no parseable" in job.error

    async def test_min_comps_guard_bites(self, settings: Settings, patched: FakeApify) -> None:
        """With an unreachable threshold, nothing gets a fabricated band."""
        strict = settings.model_copy(update={"min_comps": 99})
        registry = JobRegistry()
        job = registry.create("https://www.ebay.com/usr/pragm_14", strict.metro)
        await Pipeline(strict, registry).run(job)
        assert job.status is JobStatus.DONE, job.error
        assert all(
            i.verdict is not None and i.verdict.kind is VerdictKind.INSUFFICIENT_DATA
            for i in job.items
        )
        assert all(i.verdict is not None and i.verdict.target is None for i in job.items)


class TestRevisePayload:
    def test_is_marked_dry_run(self) -> None:
        item = Item(external_id="1", title="thing", ask_price=85.0)
        payload = revise_payload(item, 210.0)
        assert payload["_dry_run"] is True
        assert payload["proposed"] == 210.0
        assert payload["current_ask"] == 85.0

    def test_renders_a_price_string(self) -> None:
        item = Item(external_id="1", title="thing", ask_price=85.0)
        body = revise_payload(item, 210.456)["body"]
        assert isinstance(body, dict)
        assert body["pricingSummary"]["price"]["value"] == "210.46"
