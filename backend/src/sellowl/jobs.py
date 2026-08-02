"""Pipeline orchestration.

Job state is in-process and authoritative. Single process, single event loop —
no Redis, no Celery, one less container to fail on demo night. Trade-off: state
dies with the process. That is the right trade for this app's lifetime.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Coroutine
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import structlog
from openai import AsyncOpenAI

from .config import Settings
from .embeddings import PHOTO_FLOOR_RATIO, Embedder, HashingEmbedder, OpenAIEmbedder, cosine
from .index import CompStore, ElasticCompStore, MemoryCompStore
from .logging import get_logger
from .match import apply_guards, matched_prices, specs_from_text
from .models import Comp, Condition, Item, Job, JobStage, JobStatus, Venue, VisionResult
from .pricing import FeeConfig, build_verdict
from .sightings import record_sightings
from .sources import (
    ApifyCompSource,
    BrowserScraper,
    CompSource,
    EbayBrowserSource,
    fetch_bytes,
    parse_local_comps,
    parse_sold_comps,
    parse_store_items,
    upstream_error,
)
from .sqlite_store import SqliteCompStore
from .vision import VisionGrader

log = get_logger(__name__)


def make_store(settings: Settings) -> CompStore:
    if settings.search_backend == "sqlite":
        log.info("search_backend", backend="sqlite", db=settings.sqlite_db_path)
        return SqliteCompStore(Path(settings.sqlite_db_path), embedder=make_embedder(settings))
    if settings.elastic_configured:
        return ElasticCompStore(
            settings.elasticsearch_endpoint,
            settings.elasticsearch_api_key,
            settings.index_comps,
            rrf_enabled=settings.rrf_enabled,
        )
    log.warning("elastic_not_configured", fallback="in-memory matching (demo will be weaker)")
    return MemoryCompStore()


# One browser for the process, not one per job. The scraping profile is
# single-writer, so a per-job browser made two jobs in a row fight over the
# lock; keeping it warm also avoids re-running the homepage warm-up (and the
# extra launches that come with it) on every analysis.
_shared_scraper: BrowserScraper | None = None


def make_source(settings: Settings) -> CompSource:
    """See sources/protocol.py for why the three legs differ in replaceability."""
    if settings.comp_source == "browser":
        global _shared_scraper
        if _shared_scraper is None:
            _shared_scraper = BrowserScraper(settings)
        return EbayBrowserSource(settings, scraper=_shared_scraper)
    return ApifyCompSource(settings)


async def close_shared_scraper() -> None:
    """Called on app shutdown; the browser deliberately outlives a job."""
    global _shared_scraper
    if _shared_scraper is not None:
        await _shared_scraper.close()
        _shared_scraper = None


def make_embedder(settings: Settings) -> Embedder:
    if settings.embedding_base_url and settings.embedding_model:
        return OpenAIEmbedder(
            AsyncOpenAI(
                base_url=settings.embedding_base_url,
                api_key=settings.embedding_api_key or "unused",
                # Fail fast and fall back. The SDK's default retry/timeout
                # budget assumes a call worth waiting for; here there is a
                # working local fallback, so a dead endpoint should cost a
                # moment, not stall the job. An unreachable server turned a
                # 24-query corpus check into minutes of retries.
                timeout=settings.embedding_timeout_s,
                max_retries=1,
            ),
            settings.embedding_model,
        )
    return HashingEmbedder()


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
        self._embedder = make_embedder(settings)
        self._fees = FeeConfig(
            ebay_fvf_rate=settings.ebay_fvf_rate,
            ebay_fixed_fee=settings.ebay_fixed_fee,
            fb_local_rate=settings.fb_local_rate,
            fb_ask_discount=settings.fb_ask_discount,
        )

    async def run(self, job: Job) -> None:
        """Full pipeline. Any failure marks the job failed with a real message."""
        structlog.contextvars.bind_contextvars(job_id=job.job_id)
        # Both are created before the try so the finally can always close
        # them -- binding `source` inside would make cleanup raise NameError
        # on the very failures it exists to clean up after.
        store = make_store(self._s)
        source = make_source(self._s)
        try:
            job.status = JobStatus.RUNNING
            grader = VisionGrader(self._s)
            await store.ensure_indices()

            items = await self._stage_inventory(job, source)
            await self._stage_vision(job, grader, items)
            comps = await self._stage_comps(job, source, items, store)
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
            # The browser source owns a real Chromium and a single-writer
            # profile lock. Leaving it open leaked a browser per job and made
            # the *next* job fail on the lock -- and the failure surfaced as a
            # profile-in-use error that reads like the user's fault.
            closer = getattr(source, "close", None)
            if closer is not None:
                try:
                    await closer()
                except Exception as exc:  # noqa: BLE001 - cleanup must not mask the real error
                    log.warning("source_close_failed", error=str(exc))
            structlog.contextvars.unbind_contextvars("job_id")

    # --- stages ----------------------------------------------------------

    async def _stage_inventory(self, job: Job, source: CompSource) -> list[Item]:
        job.stage = JobStage(name="scraping_store", detail=job.store_url)
        rows = await source.store_listings(job.store_url, self._s.max_items)
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
            [(photo, item.title) for photo, item in zip(photos, items, strict=True)],
            identities=[f"item:{item.external_id}" for item in items],
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

    async def _covered_by_corpus(self, store: CompStore, query: str, venue: Venue) -> bool:
        """True when we already hold enough fresh, relevant comps for this query.

        Scraping is the expensive half of a run (~$0.19 per query per venue,
        and the two dropped actors aside, ~22% of one month's spend went on
        re-buying queries already answered). The comp store persists across
        jobs, so re-analysing a store -- the normal case -- was paying again
        for data sitting on disk.

        "Enough" is measured after the relevance floor, so a corpus full of
        near-misses does not count as coverage. "Fresh" matters because sold
        prices drift; comps older than `corpus_max_age_days` are ignored for
        the purpose of skipping a scrape, even though they still get used in
        pricing if the scrape then fails.
        """
        if not self._s.corpus_first:
            return False
        try:
            existing = await store.find_comps(
                bm25_query=query,
                semantic_query=query,
                venue=venue,
                size=self._s.rerank_top_k,
                job_id="",
            )
        except Exception as exc:  # noqa: BLE001 - a store miss must not stop a scrape
            log.warning("corpus_check_failed", error=str(exc))
            return False
        cutoff = datetime.now(UTC) - timedelta(days=self._s.corpus_max_age_days)
        fresh = [c for c in existing if c.scraped_at >= cutoff]
        return len(fresh) >= self._s.rerank_top_k

    async def _stage_comps(
        self, job: Job, source: CompSource, items: list[Item], store: CompStore
    ) -> list[Comp]:
        """One actor run per query, bounded, and only for what we lack.

        Not one batched run: `maxItems` is a global cap, so a batch lets the
        first query eat the whole budget and starve the rest.
        """
        queries = _queries_for(items)
        wanted: list[tuple[str, Venue]] = [
            (q, v) for q in queries for v in (Venue.EBAY_SOLD, Venue.FB_LOCAL)
        ]
        covered = await asyncio.gather(*(self._covered_by_corpus(store, q, v) for q, v in wanted))
        skip = {pair for pair, hit in zip(wanted, covered, strict=True) if hit}
        if skip:
            log.info(
                "corpus_first_skipped_scrapes",
                skipped=len(skip),
                of=len(wanted),
                saved_usd=round(len(skip) * 0.19, 2),
            )
        job.stage = JobStage(name="finding_comps", total=len(wanted) - len(skip))
        gate = asyncio.Semaphore(self._s.comp_concurrency)

        async def one(
            kind: str, fetch: Coroutine[Any, Any, list[dict[str, Any]]]
        ) -> list[dict[str, Any]]:
            async with gate:
                try:
                    return await fetch
                except Exception as exc:  # noqa: BLE001 - one dead query must not kill the job
                    log.warning("comp_run_failed", kind=kind, error=str(exc))
                    return []
                finally:
                    job.stage.done += 1

        limit = self._s.max_comps_per_query
        sold_queries = [q for q in queries if (q, Venue.EBAY_SOLD) not in skip]
        local_queries = [q for q in queries if (q, Venue.FB_LOCAL) not in skip]
        sold_runs = [
            one("sold", source.sold_comps(q, limit, self._s.sold_days_back)) for q in sold_queries
        ]
        local_runs = [one("local", source.local_comps(job.metro, q, limit)) for q in local_queries]

        results = await asyncio.gather(*sold_runs, *local_runs)
        sold_rows = [row for batch in results[: len(sold_queries)] for row in batch]
        local_rows = [row for batch in results[len(sold_queries) :] for row in batch]

        comps = parse_sold_comps(sold_rows, job.job_id) + parse_local_comps(local_rows, job.job_id)
        # No comps is not a reason to throw away a successful store read.
        # Every item still renders, and the MIN_COMPS guard turns each one
        # into an honest "insufficient data" rather than a fabricated band --
        # strictly more useful than failing the job and showing nothing. This
        # is the real state of things whenever eBay's sold listings sit behind
        # their sign-in wall, which is most of the time without a session.
        if not comps and not skip:
            log.warning(
                "no_comps_available",
                source=self._s.comp_source,
                hint=(
                    "eBay requires a signed-in session for sold listings: run "
                    "`uv run python scripts/browser_login.py` once"
                    if self._s.comp_source == "browser"
                    else "check the scraper runs for blocking"
                ),
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
            found = await self._drop_photo_mismatches(vision, found)
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

    async def _drop_photo_mismatches(self, vision: VisionResult, comps: list[Comp]) -> list[Comp]:
        """Second relevance pass, this time using what the comp's photo showed.

        Retrieval can only match on what was indexed, and comps are indexed
        before their photos are graded -- so a listing titled "Electronics
        Kit" scores well against "Makeblock Inventor Electronic Kit" on text
        alone. The rerank stage then looks at its photo, correctly reports "a
        precision screwdriver set in a tool case", and until now that finding
        was used for condition and attributes but never allowed to reject the
        comp. We were paying for the photo and ignoring what it said.

        Applied only where a comp actually has a vision description: a comp
        that was never graded is missing information, not a mismatch, which
        is the same rule the attribute guards follow.
        """
        graded = [c for c in comps if c.description]
        mine = vision.canonical_description
        if not graded or not mine:
            return comps

        query = await self._embedder.embed_query(mine)
        vectors = await self._embedder.embed([c.description for c in graded])
        # A looser bar than retrieval's: see PHOTO_FLOOR_RATIO.
        floor = self._embedder.relevance_floor * PHOTO_FLOOR_RATIO
        rejected = {
            c.doc_id for c, v in zip(graded, vectors, strict=True) if cosine(query, v) < floor
        }
        if rejected:
            log.info("comps_rejected_on_photo", count=len(rejected), floor=floor)
        return [c for c in comps if c.doc_id not in rejected]

    async def _rerank(
        self, comps: list[Comp], grader: VisionGrader, cache: dict[str, VisionResult]
    ) -> list[Comp]:
        pending = [c for c in comps if c.doc_id not in cache and c.photo_url]
        if pending and grader.enabled:
            # Check the disk cache BEFORE downloading anything. A comp's photo
            # is keyed by its listing id, so a previously-graded comp costs no
            # network at all -- this used to re-fetch every comp photo on every
            # run purely to compute a byte-hash cache key it already had the
            # answer for, which is what made re-analyzing a store slow.
            todo: list[Comp] = []
            for comp in pending:
                hit = grader.cached_grade(grader.reference_key(comp.doc_id, comp.title))
                if hit is not None:
                    cache[comp.doc_id] = hit
                else:
                    todo.append(comp)
            if todo:
                log.info(
                    "rerank_vision",
                    to_grade=len(todo),
                    skipped_download=len(pending) - len(todo),
                )
                photos = await asyncio.gather(*(fetch_bytes(c.photo_url) for c in todo))
                results = await grader.grade_many(
                    [(photo, comp.title) for photo, comp in zip(photos, todo, strict=True)],
                    identities=[comp.doc_id for comp in todo],
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
