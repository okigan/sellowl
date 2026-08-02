# SellOwl — TODO

Ordered so that there is something demoable at every point. If the clock runs
out, whatever is checked off still runs.

**Status: phases 0–5 and 7 shipped; the hack-night submission is in.** What
follows is kept as the build log — the checked boxes are what actually got
built, and the notes under them record where the plan and the code diverged.
Open work is collected under *Now* at the bottom.

## Phase 0 — De-risk (do this first, ~15 min, timeboxed hard)

The three unknowns that can sink the build. Find out now, not at 8pm.

- [x] Run `crawlerbros/ebay-item-store-scraper` once against a real store URL.
      *Blocked/never returned — dropped.*
- [x] If it fails, run `delicious_zebu/ebay-product-listing-scraper` (4.79★).
      *Also dropped. Winner is `memo23/ebay-search-scraper-ppe`, used for both
      store and sold via its `mode` param.*
- [x] Run `caffein.dev/ebay-sold-listings` on one keyword. *Dropped — see
      DEVELOP.md. Sold comps come from `memo23` too.*
- [x] Run `apify/facebook-marketplace-scraper` on one metro search URL.
- [x] Confirm the Elastic cluster accepts `retriever` + `rrf` syntax.
      *It does; the Python-side `rrf_fuse` fallback exists anyway.*
- [x] Save one real response from each actor into `tests/fixtures/`.

Record actual field names in DESIGN.md if they differ from what's written
there — the actor READMEs are not reliable. *(Done — and they did differ: FB's
photo field is `primary_listing_photo.photo_image_url`, not what the README
implies.)*

## Phase 1 — Skeleton

- [x] `compose.yaml`, backend + frontend containers, hot reload both
- [x] FastAPI app with `/health`; React page that renders it
- [x] `config.py` with pydantic-settings, `.env.example` committed
- [x] `make check` green (ruff + mypy + pytest)
- [x] Elasticsearch client, create index mappings on startup if absent
      — **only `sellowl-comps` exists.** `sellowl-items`/`sellowl-jobs` were
      planned and deliberately not built: item and job state is in-process
      (see DESIGN.md § Jobs), so an index for them would have been a second
      source of truth for no gain.
- [x] Verify `semantic_text` actually embeds

## Phase 2 — Tier 1 vertical slice

Goal: paste a store URL → see a table of your own listings. No comps yet.

- [x] `sources/` — run actor, poll, parse into `Item` models
      *(`sources/store.py`, not `apify_store.py`)*
- [x] `POST /api/analyze` → job_id; background task
      *(state in the in-process `JobRegistry`, not `sellowl-jobs`)*
- [x] `GET /api/jobs/{id}` polling endpoint with stage + progress
- [x] Frontend: URL input → running state with stage narration → item table
- [x] Skeleton rows, no layout shift on arrival

**Demoable here.** Screenshot it before moving on.

## Phase 3 — Vision

- [x] `vision.py` — photo bytes → structured JSON (description, attributes,
      condition, evidence, broad + narrow queries)
- [x] Fetch photo bytes at ingest — **FB URLs expire**, never store-and-defer
- [x] Bounded concurrency (`asyncio.Semaphore`, default 8)
- [x] Condition chip in the UI with evidence on hover
- [x] Fixture-based test: known photo → expected condition bucket
- [x] *Beyond plan:* dual provider (Anthropic **or** any OpenAI-compatible
      server), condition-synonym normalisation, and prompt-hash-keyed disk
      caching so editing the prompt invalidates stale entries automatically.

## Phase 4 — Comps

- [x] `sources/sold.py` and `sources/local.py`, run in parallel
- [x] ~~Batch all items' broad queries into one run per actor~~ — **not done
      on purpose.** `maxItems` is a global per-run cap, so batching lets the
      first query eat the whole budget and starve the rest. One run per
      query, bounded by `COMP_CONCURRENCY`.
- [x] Bulk upsert into `sellowl-comps`, `_id` = external listing id
- [x] `match.py` — RRF retrieval, top-K per item
- [x] Drift guards: score floor + attribute agreement
- [x] Expandable comp list in the UI, with photos and scores

## Phase 5 — The verdict

- [x] Rerank pass: vision over top-K comps only (not all comps)
- [x] Percentiles over the condition-matched sold set — **computed in Python
      (`pricing.percentiles`), not as an ES agg.** Nearest-rank, so every
      number shown is a price something actually sold for.
- [x] `MIN_COMPS` guard → "insufficient data", never a fabricated median
- [x] `pricing.py` — net-proceeds math, target price, venue recommendation
- [x] Unit tests for pricing, then `make mutate`
- [x] Sort table by money-left-on-the-table, descending
- [ ] Delta count-up animation on verdict arrival *(never built; cosmetic)*

**This is the demo.** Everything after is optional.

## Phase 6 — Ship

- [ ] Deploy (Vercel frontend + Fly/Render backend, or single container)
      *Dockerfiles exist for both; no hosting configured. The only phase-6
      item still genuinely open.*
- [x] README with the why-Apify / why-Elastic paragraphs from GOAL.md
- [x] Screenshot of the best verdict in the README
- [x] Rehearse the 5-minute demo out loud, once, with a timer
- [x] Submission form: repo URL, description, why/how, teammate names

## Phase 7 — Tier 3

- [x] `POST /api/items/{id}/revise-payload` — render the eBay revise call
- [x] UI shows the payload with a copy button, labeled **DRY RUN**
- [x] Key used within the request, never persisted, scrubbed from logs

## Now — open work

Post-hackathon, in rough priority order:

- [ ] **Calibrate the numeric-spec exponents.** `NUMERIC_SPEC_POLICIES` ships
      0.6 capacity / 0.9 pack count / 0.35 length. The *ordering* is sound;
      the numbers are guesses. Needs paired same-product-different-spec sold
      data to fit against.
- [ ] **Tell radiators from fans** so `form_factor` can price the former (a
      360mm radiator really is worth more than a 120mm) while continuing to
      leave the latter alone (a 140mm fan is not "more fan").
- [ ] **Calibrate `FB_ASK_DISCOUNT`** (0.85) against real asking-vs-closed
      local sales instead of an eyeballed constant.
- [ ] Evict from `sellowl-comps`, or scope retrieval by job — the index grows
      unbounded across every job ever run.
- [ ] Deploy (see phase 6).
- [ ] Migrate off Apify/Elastic — see MIGRATION.md.

## Later

- [ ] Playwright e2e over the full flow
- [ ] Price history — re-run over time, track band movement
- [ ] More venues: OfferUp, Craigslist, Mercari
- [ ] Percolate-based alerting when a comp band moves past a threshold
- [ ] Real write-back, behind explicit per-item confirmation

## Cut list

When behind, cut in this order. Each cut leaves a working app:

1. Tier 3 payload preview (phase 7)
2. FB Marketplace source → sold comps only, drop the venue recommendation
3. Rerank pass → trust text matching alone, note it as a limitation
4. Attribute-agreement guard → score floor only
5. Condition matching → single price band for all conditions

**Never cut:** the `MIN_COMPS` guard, or showing the comps behind a verdict.
A number nobody can audit is worse than no number.

## Risks

| Risk | Mitigation |
|---|---|
| Store scraper unrated, may not work | Backup actor identified; phase 0 decides |
| RRF syntax unsupported | Python-side fusion fallback; decided in phase 0 |
| Vision too slow across all comps | Retrieve-then-rerank; only top-K get vision |
| FB photo URLs expire mid-run | Fetch bytes at ingest |
| Semantic drift → nonsense comps | Score floor + attributes + always show comps |
| Too few sold comps for a band | `MIN_COMPS` guard, honest empty state |
| Actor costs run away | $50 coupon ≈ 10k listings; cap `maxItems` per run |
