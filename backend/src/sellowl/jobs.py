"""Pipeline orchestration.

Job state is in-process and authoritative. Single process, single event loop —
no Redis, no Celery, one less container to fail on demo night. Trade-off: state
dies with the process. That is the right trade for this app's lifetime.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import structlog

from .config import Settings
from .index import CompStore, ElasticCompStore, MemoryCompStore
from .logging import get_logger
from .match import apply_guards, matched_prices, specs_from_text
from .models import Comp, Condition, Item, Job, JobStage, JobStatus, Venue, VisionResult
from .pricing import FeeConfig, build_verdict
from .sightings import record_sightings
from .sources import (
    ApifyClient,
    fetch_bytes,
    local_actor_payload,
    parse_local_comps,
    parse_sold_comps,
    parse_store_items,
    sold_actor_payload,
    store_actor_payload,
    upstream_error,
)
from .vision import VisionGrader

log = get_logger(__name__)


def make_store(settings: Settings) -> CompStore:
    if settings.elastic_configured:
        return ElasticCompStore(
            settings.elasticsearch_endpoint,
            settings.elasticsearch_api_key,
            settings.index_comps,
            rrf_enabled=settings.rrf_enabled,
        )
    log.warning("elastic_not_configured", fallback="in-memory matching (demo will be weaker)")
    return MemoryCompStore()


class JobRegistry:
    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}

    def create(self, store_url: str, metro: str) -> Job:
        job = Job(job_id=uuid.uuid4().hex[:12], store_url=store_url, metro=metro)
        self._jobs[job.job_id] = job
        return job

    def get(self, job_id: str) -> Job | None:
        return self._jobs.get(job_id)

    def all(self) -> list[Job]:
        return sorted(self._jobs.values(), key=lambda j: j.created_at, reverse=True)


class Pipeline:
    def __init__(self, settings: Settings, registry: JobRegistry) -> None:
        self._s = settings
        self._registry = registry
        self._fees = FeeConfig(
            ebay_fvf_rate=settings.ebay_fvf_rate,
            ebay_fixed_fee=settings.ebay_fixed_fee,
            fb_local_rate=settings.fb_local_rate,
            fb_ask_discount=settings.fb_ask_discount,
        )

    async def run(self, job: Job) -> None:
        """Full pipeline. Any failure marks the job failed with a real message."""
        structlog.contextvars.bind_contextvars(job_id=job.job_id)
        store = make_store(self._s)
        try:
            job.status = JobStatus.RUNNING
            apify = ApifyClient(
                self._s.apify_token,
                timeout_s=self._s.apify_timeout_s,
                cache_ttl_s=self._s.apify_cache_ttl_hours * 3600,
            )
            grader = VisionGrader(self._s)
            await store.ensure_indices()

            items = await self._stage_inventory(job, apify)
            await self._stage_vision(job, grader, items)
            comps = await self._stage_comps(job, apify, items)
            await self._stage_index(job, store, comps)
            await self._stage_match(job, store, grader, items)

            job.items = items
            job.stage = JobStage(name="done", detail=f"{len(items)} items", done=1, total=1)
            job.status = JobStatus.DONE
        except Exception as exc:  # surface the real reason to the UI
            log.exception("job_failed", error=str(exc))
            job.status = JobStatus.FAILED
            job.error = f"{type(exc).__name__}: {exc}"
        finally:
            await store.close()
            structlog.contextvars.unbind_contextvars("job_id")

    # --- stages ----------------------------------------------------------

    async def _stage_inventory(self, job: Job, apify: ApifyClient) -> list[Item]:
        job.stage = JobStage(name="scraping_store", detail=job.store_url)
        rows = await apify.run_actor(
            self._s.actor_store,
            store_actor_payload(job.store_url, self._s.max_items),
            max_items=self._s.max_items,
        )
        items = parse_store_items(rows, job.store_url, self._s.max_items)
        if not items:
            reported = upstream_error(rows)
            if reported:
                raise RuntimeError(f"{self._s.actor_store} could not read the store: {reported}")
            raise RuntimeError(
                f"{self._s.actor_store} returned {len(rows)} rows but no parseable listings. "
                "Check the actor output shape, or switch ACTOR_STORE in .env."
            )
        # How long each listing has been sitting. eBay's search output has no
        # "listed on" date, so age is measured from the first time this app
        # saw the listing -- worthless on a store's first analysis, real
        # signal on every one after it.
        ages = record_sightings(job.store_url, [i.external_id for i in items])
        for item in items:
            item.job_id = job.job_id
            item.days_listed = ages.get(item.external_id)
        job.items = items
        return items

    async def _stage_vision(self, job: Job, grader: VisionGrader, items: list[Item]) -> None:
        job.stage = JobStage(name="reading_photos", total=len(items))
        photos = await asyncio.gather(*(fetch_bytes(i.photo_url) for i in items))
        results = await grader.grade_many(
            [(photo, item.title) for photo, item in zip(photos, items, strict=True)]
        )
        for item, result in zip(items, results, strict=True):
            # A photo grade beats nothing; the seller's own listed condition
            # beats a photo grade that came back unknown (no vision configured,
            # no photo, or a failed call).
            if (
                result.condition is Condition.UNKNOWN
                and item.listed_condition is not Condition.UNKNOWN
            ):
                result = result.model_copy(
                    update={
                        "condition": item.listed_condition,
                        "condition_evidence": "From the seller's own listing (no photo grade).",
                    }
                )
            # Vision extraction has run-to-run variance on whether it surfaces
            # the numeric spec attributes at all; the title usually states
            # them plainly, so fall back to reading them straight from there.
            # Vision wins wherever it did produce a value.
            found = {
                k: v for k, v in specs_from_text(item.title).items() if k not in result.attributes
            }
            if found:
                result = result.model_copy(update={"attributes": {**result.attributes, **found}})
            item.vision = result
            job.stage.done += 1

    async def _stage_comps(self, job: Job, apify: ApifyClient, items: list[Item]) -> list[Comp]:
        """One actor run per query, bounded.

        Not one batched run: `maxItems` is a global cap, so a batch lets the
        first query eat the whole budget and starve the rest.
        """
        queries = _queries_for(items)
        job.stage = JobStage(name="finding_comps", total=len(queries) * 2)
        gate = asyncio.Semaphore(self._s.comp_concurrency)

        async def one(actor: str, payload: dict[str, Any], limit: int) -> list[dict[str, Any]]:
            async with gate:
                try:
                    return await apify.run_actor(actor, payload, max_items=limit)
                except Exception as exc:  # noqa: BLE001 - one dead query must not kill the job
                    log.warning("comp_run_failed", actor=actor, error=str(exc))
                    return []
                finally:
                    job.stage.done += 1

        limit = self._s.max_comps_per_query
        sold_runs = [
            one(
                self._s.actor_sold,
                sold_actor_payload(q, limit, self._s.sold_days_back),
                limit,
            )
            for q in queries
        ]
        local_runs = [
            one(self._s.actor_local, local_actor_payload(job.metro, q, limit), limit)
            for q in queries
        ]

        results = await asyncio.gather(*sold_runs, *local_runs)
        sold_rows = [row for batch in results[: len(queries)] for row in batch]
        local_rows = [row for batch in results[len(queries) :] for row in batch]

        comps = parse_sold_comps(sold_rows, job.job_id) + parse_local_comps(local_rows, job.job_id)
        if not comps:
            raise RuntimeError(
                "Both comp sources returned nothing usable — cannot price anything. "
                "Check the Apify runs for blocking."
            )
        log.info(
            "comps_fetched",
            sold_rows=len(sold_rows),
            local_rows=len(local_rows),
            parsed=len(comps),
        )
        return comps

    async def _stage_index(self, job: Job, store: CompStore, comps: list[Comp]) -> None:
        job.stage = JobStage(name="indexing", total=len(comps))
        job.stage.done = await store.upsert_comps(comps)

    async def _stage_match(
        self, job: Job, store: CompStore, grader: VisionGrader, items: list[Item]
    ) -> None:
        job.stage = JobStage(name="matching", total=len(items))
        graded: dict[str, VisionResult] = {}
        for item in items:
            vision = item.vision or VisionResult(canonical_description=item.title)
            bm25 = vision.search_query_narrow or item.title
            semantic = vision.canonical_description or item.title

            found: list[Comp] = []
            for venue in (Venue.EBAY_SOLD, Venue.FB_LOCAL):
                found += await store.find_comps(
                    bm25_query=bm25,
                    semantic_query=semantic,
                    venue=venue,
                    size=self._s.rerank_top_k,
                    job_id=job.job_id,
                )

            # Rerank: vision only over survivors. Grading every comp would be
            # ~500 calls; this is ~top_k per venue, and cached across items.
            found = await self._rerank(found, grader, graded)
            found = apply_guards(
                found,
                item_attrs=vision.attributes,
                score_floor=self._s.match_score_floor,
            )
            item.comps = sorted(found, key=lambda c: c.score, reverse=True)

            sold = [c for c in item.comps if c.venue is Venue.EBAY_SOLD]
            local = [c for c in item.comps if c.venue is Venue.FB_LOCAL]
            model_value = vision.attributes.get("model", "")
            sold_matched = matched_prices(
                sold,
                condition_value=vision.condition.value,
                model_value=model_value,
                min_comps=self._s.min_comps,
            )
            local_matched = matched_prices(
                local,
                condition_value=vision.condition.value,
                model_value=model_value,
                min_comps=self._s.min_comps,
            )
            item.verdict = build_verdict(
                ask_price=item.ask_price,
                sold_prices=sold_matched.prices,
                local_prices=local_matched.prices,
                attributes=vision.attributes,
                condition=vision.condition,
                fees=self._fees,
                min_comps=self._s.min_comps,
                sold_tier=sold_matched.tier,
                local_tier=local_matched.tier,
                days_listed=item.days_listed,
            )
            job.stage.done += 1

    async def _rerank(
        self, comps: list[Comp], grader: VisionGrader, cache: dict[str, VisionResult]
    ) -> list[Comp]:
        todo = [c for c in comps if c.doc_id not in cache and c.photo_url]
        if todo and grader.enabled:
            photos = await asyncio.gather(*(fetch_bytes(c.photo_url) for c in todo))
            results = await grader.grade_many(
                [(photo, comp.title) for photo, comp in zip(photos, todo, strict=True)]
            )
            for comp, result in zip(todo, results, strict=True):
                cache[comp.doc_id] = result

        out: list[Comp] = []
        for comp in comps:
            graded = cache.get(comp.doc_id)
            if graded is None:
                out.append(comp)
                continue
            attributes = graded.attributes
            from_title = {
                k: v for k, v in specs_from_text(comp.title).items() if k not in attributes
            }
            if from_title:
                attributes = {**attributes, **from_title}
            out.append(
                comp.model_copy(
                    update={
                        "condition": (
                            graded.condition
                            if graded.condition.value != "unknown"
                            else comp.condition
                        ),
                        "condition_evidence": graded.condition_evidence,
                        "attributes": attributes,
                        "description": graded.canonical_description,
                    }
                )
            )
        return out


def _queries_for(items: list[Item]) -> list[str]:
    """One broad query per item, deduplicated, order preserved."""
    seen: dict[str, None] = {}
    for item in items:
        query = (item.vision.search_query_broad if item.vision else "") or item.title
        seen.setdefault(query.strip(), None)
    return [q for q in seen if q]


def revise_payload(item: Item, new_price: float) -> dict[str, Any]:
    """Tier 3: render the eBay call that WOULD be made. Dry run only.

    There is deliberately no execution path in this codebase. See
    docs/DESIGN.md § Tier 3.
    """
    return {
        "_dry_run": True,
        "_note": "SellOwl never executes this. Copy it if you want to run it yourself.",
        "endpoint": "PUT https://api.ebay.com/sell/inventory/v1/offer/{offerId}/update_price",
        "path_params": {"offerId": f"<offer id for listing {item.external_id}>"},
        "body": {
            "pricingSummary": {
                "price": {"value": f"{new_price:.2f}", "currency": "USD"},
            }
        },
        "current_ask": item.ask_price,
        "proposed": round(new_price, 2),
    }
