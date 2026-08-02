"""Compare search backends on the answer they produce, not on overlap.

Run from backend/:  uv run python scripts/compare_backends.py

Does the backend change the *answer*? Retrieval overlap is not the metric.

58% top-8 overlap between SQLite and Elastic says they disagree, not that
either is wrong -- neither is ground truth. The decision-relevant question is
whether a seller would be told something different, so this runs the real
downstream path (guards -> condition/model bucketing -> verdict) on each
backend's comps and diffs the recommendations.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, "src")

# Uses Pipeline's private _stage_* methods on purpose: this must exercise the
# real corpus-building path, not a reimplementation that could drift from it.

from sellowl.config import Settings
from sellowl.index import ElasticCompStore
from sellowl.jobs import JobRegistry, Pipeline, make_embedder
from sellowl.match import apply_guards, matched_prices
from sellowl.models import Venue
from sellowl.pricing import FeeConfig, build_verdict
from sellowl.sources import ApifyClient
from sellowl.sqlite_store import SqliteCompStore
from sellowl.vision import VisionGrader


async def verdict_for(store, item, settings, fees, job_id):
    vision = item.vision
    bm25 = (vision.search_query_narrow if vision else "") or item.title
    semantic = (vision.canonical_description if vision else "") or item.title
    found = []
    for venue in (Venue.EBAY_SOLD, Venue.FB_LOCAL):
        found += await store.find_comps(
            bm25_query=bm25,
            semantic_query=semantic,
            venue=venue,
            size=settings.rerank_top_k,
            job_id=job_id,
        )
    found = apply_guards(
        found,
        item_attrs=vision.attributes if vision else {},
        score_floor=settings.match_score_floor,
    )
    sold = [c for c in found if c.venue is Venue.EBAY_SOLD]
    local = [c for c in found if c.venue is Venue.FB_LOCAL]
    model_value = vision.attributes.get("model", "") if vision else ""
    cond = vision.condition.value if vision else "unknown"
    s = matched_prices(
        sold, condition_value=cond, model_value=model_value, min_comps=settings.min_comps
    )
    lo = matched_prices(
        local, condition_value=cond, model_value=model_value, min_comps=settings.min_comps
    )
    return build_verdict(
        ask_price=item.ask_price,
        sold_prices=s.prices,
        local_prices=lo.prices,
        attributes=vision.attributes if vision else {},
        condition=vision.condition if vision else None,
        fees=fees,
        min_comps=settings.min_comps,
        sold_tier=s.tier,
        local_tier=lo.tier,
        days_listed=item.days_listed,
    )


async def main() -> None:
    settings = Settings()
    registry = JobRegistry()
    pipeline = Pipeline(settings, registry)
    job = registry.create(settings.default_store_url, settings.metro)
    fees = FeeConfig(
        ebay_fvf_rate=settings.ebay_fvf_rate,
        ebay_fixed_fee=settings.ebay_fixed_fee,
        fb_local_rate=settings.fb_local_rate,
        fb_ask_discount=settings.fb_ask_discount,
    )
    apify = ApifyClient(
        settings.apify_token,
        timeout_s=settings.apify_timeout_s,
        cache_ttl_s=settings.apify_cache_ttl_hours * 3600,
    )
    grader = VisionGrader(settings)

    items = await pipeline._stage_inventory(job, apify)
    await pipeline._stage_vision(job, grader, items)
    comps = await pipeline._stage_comps(job, apify, items)

    p = Path("/tmp/compare_comps.db")
    p.unlink(missing_ok=True)
    sqlite = SqliteCompStore(p, embedder=make_embedder(settings))
    elastic = ElasticCompStore(
        settings.elasticsearch_endpoint,
        settings.elasticsearch_api_key,
        "sellowl-compare",
        rrf_enabled=settings.rrf_enabled,
    )
    for store in (sqlite, elastic):
        await store.ensure_indices()
        await store.upsert_comps(comps)
    await asyncio.sleep(6)

    print(f"\n{'item':40} {'ask':>6} | {'sqlite verdict':>22} | {'elastic verdict':>22} | same?")
    print("-" * 108)
    same_kind = usable_s = usable_e = 0
    for item in items:
        vs = await verdict_for(sqlite, item, settings, fees, job.job_id)
        ve = await verdict_for(elastic, item, settings, fees, job.job_id)

        def fmt(v):
            if v.kind.value == "insufficient_data":
                return "no data"
            b = v.sold_band
            return f"{v.kind.value} ${b.p50:.0f} (n={b.n})" if b else v.kind.value

        usable_s += vs.kind.value != "insufficient_data"
        usable_e += ve.kind.value != "insufficient_data"
        agree = vs.kind == ve.kind
        same_kind += agree
        print(
            f"{item.title[:40]:40} {item.ask_price or 0:>6.0f} | {fmt(vs):>22} | "
            f"{fmt(ve):>22} | {'yes' if agree else 'NO'}"
        )

    n = len(items)
    print(f"\nusable verdicts: sqlite {usable_s}/{n}, elastic {usable_e}/{n}")
    print(f"same verdict kind: {same_kind}/{n} ({100 * same_kind / n:.0f}%)")
    await sqlite.close()
    await elastic.close()


asyncio.run(main())
