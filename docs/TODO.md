# SellOwl — TODO

Ordered so that there is something demoable at every point. If the clock runs
out, whatever is checked off still runs.

## Phase 0 — De-risk (do this first, ~15 min, timeboxed hard)

The three unknowns that can sink the build. Find out now, not at 8pm.

- [ ] Run `crawlerbros/ebay-item-store-scraper` once against a real store URL.
      Does it return listings with photo URLs? *(unrated actor — top risk)*
- [ ] If it fails, run `delicious_zebu/ebay-product-listing-scraper` (4.79★)
      against the same URL. Pick a winner, put the slug in `.env`.
- [ ] Run `caffein.dev/ebay-sold-listings` on one keyword. Confirm it returns
      sold prices **and sold dates**.
- [ ] Run `apify/facebook-marketplace-scraper` on one metro search URL.
      Confirm it returns for your metro, not just the sample cities.
- [ ] Confirm the Elastic cluster accepts `retriever` + `rrf` syntax.
      If not → Python-side fusion, decide now and move on.
- [ ] Save one real response from each actor into `tests/fixtures/`.
      These are both the test fixtures and the real schema documentation.

Record actual field names in DESIGN.md if they differ from what's written
there — the actor READMEs are not reliable.

## Phase 1 — Skeleton

- [ ] `compose.yaml`, backend + frontend containers, hot reload both
- [ ] FastAPI app with `/health`; React page that renders it
- [ ] `config.py` with pydantic-settings, `.env.example` committed
- [ ] `make check` green on an empty project (ruff + mypy + pytest)
- [ ] Elasticsearch client, create `sellowl-comps` / `sellowl-items` /
      `sellowl-jobs` mappings on startup if absent
- [ ] Verify `semantic_text` actually embeds: index one doc, run a `semantic`
      query, get it back

## Phase 2 — Tier 1 vertical slice

Goal: paste a store URL → see a table of your own listings. No comps yet.

- [ ] `sources/apify_store.py` — run actor, poll, parse into `Item` models
- [ ] `POST /api/analyze` → job_id; background task; state in `sellowl-jobs`
- [ ] `GET /api/jobs/{id}` polling endpoint with stage + progress
- [ ] Frontend: URL input → running state with stage narration → item table
- [ ] Skeleton rows, no layout shift on arrival

**Demoable here.** Screenshot it before moving on.

## Phase 3 — Vision

- [ ] `vision.py` — photo bytes → structured JSON (description, attributes,
      condition, evidence, broad + narrow queries)
- [ ] Fetch photo bytes at ingest — **FB URLs expire**, never store-and-defer
- [ ] Bounded concurrency (`asyncio.Semaphore`, default 8)
- [ ] Condition chip in the UI with evidence on hover
- [ ] Fixture-based test: known photo → expected condition bucket

## Phase 4 — Comps

- [ ] `sources/apify_sold.py` and `sources/apify_local.py`, run in parallel
- [ ] Batch all items' broad queries into one run per actor
- [ ] Bulk upsert into `sellowl-comps`, `_id` = external listing id
- [ ] `match.py` — RRF retrieval, top-K per item
- [ ] Drift guards: score floor + attribute agreement
- [ ] Expandable comp list in the UI, with photos and scores

## Phase 5 — The verdict

- [ ] Rerank pass: vision over top-K comps only (not all comps)
- [ ] `percentiles` agg over the condition-matched sold set
- [ ] `MIN_COMPS` guard → "insufficient data", never a fabricated median
- [ ] `pricing.py` — net-proceeds math, target price, venue recommendation
- [ ] Unit tests for pricing, then `make mutate` → no survivors in `pricing.py`
- [ ] Sort table by money-left-on-the-table, descending
- [ ] Delta count-up animation on verdict arrival

**This is the demo.** Everything after is optional.

## Phase 6 — Ship

- [ ] Deploy (Vercel frontend + Fly/Render backend, or single container)
- [ ] README with the why-Apify / why-Elastic paragraphs from GOAL.md
- [ ] Screenshot of the best verdict in the README
- [ ] Rehearse the 5-minute demo out loud, once, with a timer
- [ ] Submission form: repo URL, description, why/how, teammate names

## Phase 7 — Tier 3 (only if comfortably ahead)

- [ ] `POST /api/items/{id}/revise-payload` — render the eBay revise call
- [ ] UI shows the payload with a copy button, labeled **DRY RUN**
- [ ] Key used within the request, never persisted, scrubbed from logs

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
