# SellOwl — Design

## Pipeline

```
  eBay store URL
        │
        ▼
  ┌─────────────┐   Apify: memo23/ebay-search-scraper-ppe (mode: active)
  │ 1. INVENTORY│   → your listings: title, ask price, photo, listed condition
  └─────────────┘
        │
        ▼
  ┌─────────────┐   Vision (Anthropic or any OpenAI-compatible server),
  │ 2. VISION   │   1 call per item → canonical description, attributes
  │    (yours)  │   (incl. category/model/capacity), condition grade, and
  └─────────────┘   TWO search queries
        │
        ├──────────────────────┐
        ▼                      ▼
  ┌─────────────┐        ┌─────────────┐
  │ 3a. SOLD    │        │ 3b. LOCAL   │  Apify, in parallel
  │  eBay sold  │        │  FB Mktplc  │
  └─────────────┘        └─────────────┘
        │                      │
        └──────────┬───────────┘
                   ▼
            ┌─────────────┐
            │ 4. INDEX    │  Elasticsearch, semantic_text + keyword
            └─────────────┘
                   ▼
            ┌─────────────┐  RRF: BM25 ⊕ semantic, top-K per item
            │ 5. RETRIEVE │  ← cheap, over everything
            └─────────────┘
                   ▼
            ┌─────────────┐  Claude vision on top-K comp photos only
            │ 6. RERANK   │  ← expensive, over survivors
            └─────────────┘
                   ▼
            ┌─────────────┐  percentiles over condition-matched sold set
            │ 7. AGGREGATE│
            └─────────────┘
                   ▼
            ┌─────────────┐  target price + venue, net-proceeds math
            │ 8. ADVISE   │
            └─────────────┘
```

### Why stages 5 and 6 are separate

Naively running vision over every comp is 10 items × 50 comps = **500 vision
calls**. That's slow and expensive and most of them are obvious non-matches.

So: cheap text retrieval narrows 50 → 8, then vision reranks the 8. Total
becomes ~80 calls, parallelizable. This is classic retrieve-then-rerank and it
is worth saying out loud when explaining the design — it's the difference
between a pipeline that finishes and one that doesn't.

## Data sources

All three are Apify Actors. **No marketplace API keys in tiers 1–2.**

| Purpose | Actor | Notes |
|---|---|---|
| Your inventory | `memo23/ebay-search-scraper-ppe` (`mode: "active"`) | Its `seller` field is a *filter*, not a source — passing it alone fails with "No input". Works by constructing a seller-search `startUrls` entry: `https://www.ebay.com/sch/i.html?_ssn={seller}&_sop=12&_ipg=60`. |
| Sold comps | `memo23/ebay-search-scraper-ppe` (`mode: "sold"`) | Same actor as above, dispatched by `mode` in the payload — one fewer unknown actor to verify. Search URL built with `LH_Sold=1&LH_Complete=1`. |
| Local comps | `apify/facebook-marketplace-scraper` | See § Facebook Marketplace specifics below. |

**Verify every actor with a live throwaway run before building on it — actor
READMEs get the output shape wrong.** Three actors were tried and dropped for
the store/sold legs before landing on memo23:
`crawlerbros/ebay-item-store-scraper` (returns a `{"type": "ebay_blocked", ...}`
row and exits SUCCEEDED — a blocked run disguised as a normal one),
`delicious_zebu/ebay-product-listing-scraper` (silently ignores seller-filter
input and always returns results for an unrelated hardcoded query), and
`caffein.dev/ebay-sold-listings` (every run failed outright). None of the
three surfaced their failure mode from documentation alone — only a live run
did. **Do not point `ACTOR_STORE`/`ACTOR_SOLD` at an untested actor without
re-verifying live first.**

Two lessons from that process that generalize:

- **`maxItems` is a global cap across a batched `startUrls` array**, not a
  per-URL limit. Batching every item's query into one actor call lets the
  first query's results consume the whole budget and starve the rest to
  zero. Fixed by fanning out **one actor run per query**, concurrently,
  bounded by `COMP_CONCURRENCY` — see § Apify run fan-out and caching.
- **Do not use** `xtracto/ebay-sold-comps-scraper`. It returns a pre-computed
  min/median/p90 band — convenient, and it steals the job that makes Elastic
  interesting. Pull raw sold listings; compute percentiles ourselves.

### Apify run fan-out and caching

Comp-fetching is one actor run per query per source (sold + local), fanned
out with `asyncio.gather`, bounded by a `Semaphore(COMP_CONCURRENCY)` — not
one batched run, for the `maxItems`-starvation reason above. Apify accounts
on a free/basic plan also cap *total concurrent Actor memory* across all
your running actors (16GB observed in testing); `facebook-marketplace-scraper`
alone requests ~4GB per run, so `COMP_CONCURRENCY` also has to stay low
enough that the fan-out itself doesn't 402.

Actor runs take minutes and repeat identical `(actor, payload)` calls across
dev iterations and re-analyzes of the same store, so results are cached on
disk (`backend/.cache/apify/`, TTL `APIFY_CACHE_TTL_HOURS`, default 20h).
Vision grades are cached the same way (`backend/.cache/vision/`,
`VISION_CACHE_TTL_HOURS`) — the vision cache key includes a hash of the
prompt text itself, so editing the prompt (adding an attribute, say)
automatically invalidates old entries instead of silently serving results
shaped by the old prompt for the rest of the TTL. `DELETE /api/cache` clears
every namespace under `.cache/` at once.

### Facebook Marketplace specifics

Input is a URL, three forms; we use the search form:

```
https://www.facebook.com/marketplace/austin/search/?query=teak+sideboard
```

Batch all items' queries into one run's `startUrls`. ~$2.60/1000 listings
(the actor README's $5 figure is stale — trust the pricing tab).

Output quirks that shape the design:

- **No description field.** Only `marketplace_listing_title` + a photo. This is
  why vision is core rather than decorative — the photo is the richest signal
  in the payload.
- **No post date.** Can't compute listing age or staleness. Don't promise it.
- **No lat/lon.** Only `location.reverse_geocode.city` / `.state`. So: no
  `geo_point`, no radius filter. Scope the scrape to one metro and treat all
  results as local.
- **Photo URLs expire.** `scontent-*.fbcdn.net` links carry an `oe=` expiry.
  Fetch bytes during ingest and pass them to the vision call; never store the
  URL and process it later.
- Useful: `is_sold` / `is_live` / `is_pending`, `delivery_types` (an
  `IN_PERSON`-only heavy item genuinely can't be compared to a national eBay
  sold price), `listing_price.amount` (string, cast to float), and `id`
  (use as the ES `_id` so re-runs upsert instead of duplicating).

## Elasticsearch

One index for comps, one for your inventory, one for job state.

### `sellowl-comps`

```jsonc
{
  "mappings": {
    "properties": {
      "venue":        { "type": "keyword" },        // ebay_sold | fb_local
      "external_id":  { "type": "keyword" },
      "url":          { "type": "keyword", "index": false },
      "title":        { "type": "text" },           // BM25 half of RRF
      "title_kw":     { "type": "keyword" },
      "description":  { "type": "text" },           // vision-generated, if reranked
      "content_semantic": { "type": "semantic_text" },  // semantic half of RRF
      "price":        { "type": "double" },
      "sold_at":      { "type": "date" },           // ebay_sold only
      "condition":    { "type": "keyword" },        // rough | usable | clean | unknown
      "condition_evidence": { "type": "text" },
      "attributes":   { "type": "flattened" },
      "city":         { "type": "keyword" },
      "state":        { "type": "keyword" },
      "delivery":     { "type": "keyword" },
      "photo_url":    { "type": "keyword", "index": false },
      "scraped_at":   { "type": "date" },
      "job_id":       { "type": "keyword" }
    }
  }
}
```

`content_semantic` is populated by copying title (+ description once the
rerank stage has generated one). On Elastic Serverless `semantic_text` uses
the default inference endpoint; no model deployment needed.

There is only this one index. Your inventory and job state are **not** in
Elasticsearch — see § Jobs for why, and don't be surprised there's no
`sellowl-items`/`sellowl-jobs` index in the cluster; an earlier draft of this
doc planned one and the implementation diverged on purpose.

**Retrieval is not scoped to one job's data.** `sellowl-comps` accumulates
comps across every job that has ever run against this cluster, and a query
has no `job_id` filter — a top-scoring hit is routinely a document a
*different* job indexed. `Comp` objects are reconstructed straight from each
hit's `_source` for exactly this reason: an earlier version resolved hits
through a local, per-process cache keyed by `doc_id` that only ever held
*this job's own* upserted comps, which meant any hit belonging to another
job's data was silently dropped. That bug got worse the longer a cluster
had been used for testing (more accumulated cross-job data → more hits that
missed the local cache), and it was diagnosed by comparing app-level
retrieval (0 results) against the identical raw query run directly against
Elasticsearch (hundreds of hits) — see git history on `index.py` if this
class of bug resurfaces.

## Matching (stage 5)

RRF over two retrievers — this is the load-bearing Elastic query and the one
worth having correct before hack night:

```jsonc
{
  "retriever": {
    "rrf": {
      "retrievers": [
        { "standard": { "query": {
            "match": { "title": { "query": "<vision query>" } } } } },
        { "standard": { "query": {
            "semantic": { "field": "content_semantic",
                          "query": "<vision canonical description>" } } } }
      ],
      "rank_window_size": 50,
      "rank_constant": 20
    }
  },
  "size": 8
}
```

> ⚠️ The `retriever` + `rrf` syntax requires a recent Elasticsearch. If RRF is
> unavailable, `index.py` detects the failure at runtime and falls back to
> two separate queries fused in Python (`match.rrf_fuse` — the same
> reciprocal-rank-fusion formula either way, so the code under test is the
> code that ships). This isn't hypothetical: it's the actual fallback path
> that runs on a cluster predating `retriever`/`rrf` support.

### Guarding against semantic drift

Semantic search will happily match a photo of a guitar to *every* guitar, and
title-only matching (no vision configured) is worse: a full water-cooling
loop and an industrial coolant system both matched a $15 water-cooling *tube*
purely on shared vocabulary, once inflating a single item's "opportunity"
number to +$155 off a 36x price spread among the "comps" — see git history on
`pricing.py`'s `local_band_is_trustworthy` for the incident that guard exists
for. The guards, in the order a comp actually passes through them
(`match.apply_guards`):

1. **Score floor** — drop comps below a configurable RRF score.
2. **Hard-attribute agreement** (`material`, `brand`, `era`) — a comp whose
   vision-extracted attribute *conflicts* with the item's is dropped. Values
   agree if either is a substring of the other ("Plastic, Acrylic" agrees
   with "acrylic") — two independent vision calls describing the same real
   material rarely use identical words, and exact-string comparison rejected
   almost every genuine match once vision started populating attributes on
   both sides for real.
   - **`size_class` and `category` are deliberately *not* hard-gated.** Both
     are coarse, subjective LLM judgments that can vary between two separate
     calls on the exact same real product ("small" vs. "medium"; "cooling
     fan" vs. "PC case fan") — gating on either reintroduces the
     mass-rejection failure mode above. `size_class` still feeds shipping-
     cost estimation; `category` is captured and shown for audit (see
     Vision, below) but never excludes a comp on its own. Known, accepted
     tradeoff: a same-brand, different-product mismatch (a padlock scored
     against a USB flash drive because both are "Apricorn Aegis") can still
     slip through on brand alone if nothing else disagrees.
3. **Numeric-spec price scaling** (`SCALABLE_NUMERIC_ATTRIBUTES`, currently
   just `capacity`) — a *different* mechanism from attribute agreement,
   because a capacity difference doesn't mean "different product", it means
   "same product line, price should be scaled". Generic on purpose: any
   attribute whose value is a free-text "amount + unit" spec (a package's
   "4GB", "500ml", "2-pack") can reuse the same parse-and-scale mechanism
   just by being added to `SCALABLE_NUMERIC_ATTRIBUTES` and the vision
   prompt — no new parsing or pricing code per attribute. `match.
   parse_quantity` extracts `(amount, unit)`; `match.quantity_scale_factor`
   returns a combined ratio (raised to `QUANTITY_SCALE_EXPONENT = 0.6`,
   **an uncalibrated heuristic** — bigger specs are assumed to cost less per
   unit, which held up in one real product line's data but hasn't been
   checked against real paired same-product-different-capacity sales) when
   specs differ but share a unit, or `None` (reject the comp, don't guess) if
   they can't be reconciled — different units, or either side unparsable. A
   scaled comp's `Comp.price_note` records the adjustment
   (`"scaled from $X (x0.44)"`) and the frontend surfaces it as a hover-
   titled asterisk on the price, plus a caption on the comp table when any
   row was scaled — a silently-adjusted number is exactly the kind of thing
   this app exists to make visible, not hide.
   - **Vision has real run-to-run variance on whether it surfaces a numeric
     attribute at all**, even when the title states it plainly. `match.
     capacity_from_text` is a title-regex fallback applied (both to the item
     and to each comp) only when the vision attribute is missing, never
     overriding a real grade.
4. **Always show the comps.** Every verdict expands to the listings behind
   it, with photos, condition, matched attributes, RRF score, and the price
   scaling note if any. A human spots a bad match instantly, and a verdict
   you can't audit is a verdict nobody trusts.

### What isn't attribute-gated yet

**`model`** — the product's specific line or generation within a brand (an
"Aegis Secure Key" vs. an "Aegis Secure Key 3NX" — plausibly different base
prices even at the same capacity) — is now explicitly prompted for, the same
way `category` is, and captured on `Comp.attributes`/vision results for
audit. It is **deliberately not** in `HARD_ATTRIBUTES` yet, for the same
reason `category` isn't: model-name text is free-form and read off
packaging inconsistently between two separate vision calls ("Aegis Secure
Key 3" vs. "Aegis Secure Key 3z" for the same physical line), and gating on
exact or even substring agreement risks the same mass-rejection failure
mode `size_class` caused. Worth revisiting once there's a corpus of real
`model` values to see how consistent the vocabulary actually is in
practice — until then, an unhelpfully-precise "reject on any model
mismatch" would trade the known problem (a padlock scoring against a USB
key on brand alone) for a worse one (rejecting genuine matches over
packaging-text noise).

## Vision (stages 2 and 6)

One vision call per photo, returning structured JSON:

```jsonc
{
  "canonical_description": "mid-century teak sideboard, tapered legs, brass pulls, Danish style, 1960s",
  "attributes": {
    "category": "sideboard", "material": "teak", "era": "1960s",
    "style": "danish modern", "size_class": "large", "color": "brown",
    "capacity": "4GB"
  },
  "condition": "usable",
  "condition_evidence": "visible veneer chip on left door, original hardware intact, no structural damage",
  "search_query_broad": "teak sideboard",
  "search_query_narrow": "danish modern teak credenza"
}
```

Design notes that matter:

**Condition is three buckets — `rough` / `usable` / `clean`.** Not 1–10. A
ten-point vision score is noise and collapses the moment someone asks how you
got a 7. Three buckets with cited evidence survive the question. Not every
model sticks to that exact vocabulary (a general-purpose model may answer
"new" instead of "clean"), so `vision.parse_vision_json` normalizes common
synonyms (new/mint/excellent → clean, good/used/worn → usable,
damaged/broken/for-parts → rough) before falling back to `unknown`.

**Condition has a non-vision fallback too.** The eBay store scrape already
returns the seller's own condition string ("Pre-owned", "Brand new") —
`Item.listed_condition`, parsed the same way sold-comp conditions are. When a
photo grade comes back `unknown` (no vision configured, no photo, or a
failed call), `jobs._stage_vision` uses the seller's own listed condition
instead, so condition-bucketed comp matching degrades gracefully rather than
falling back to "every comp regardless of condition" whenever a photo grade
isn't available.

**Vision generates the scraper's search query too.** Your eBay title says
"Pyrex 444 Spring Blossom"; no Facebook seller types that. The broad query
goes to the scrapers, the canonical description goes to the matcher. One call,
several jobs.

**Two interchangeable providers, same prompt and JSON contract.**
`Settings.vision_provider` selects between Anthropic (`AsyncAnthropic`) and
any OpenAI-compatible chat-completions server (`AsyncOpenAI` pointed at a
`vision_base_url` — a local vLLM/llama.cpp server running a vision model,
for instance). The message format differs per SDK (Anthropic's
`{"type": "image", "source": {...}}` vs. OpenAI's `{"type": "image_url",
"image_url": {"url": "data:...;base64,..."}}`) but the prompt text and
expected JSON shape are identical either way, so `vision.VisionGrader`
branches only at the two `_grade_*` call sites, not throughout the module.
Verified live against a local Qwen3.6-35B-A3B-MTP vision model.

**Grades are cached on disk**, keyed by `(prompt hash, provider, model,
sha256(photo bytes), title)` — see § Apify run fan-out and caching. The
prompt hash means editing the prompt (adding `capacity`, say) can't silently
serve results shaped by the old prompt for the rest of the TTL.

## Aggregation (stage 7)

Percentiles are computed in **Python** (`pricing.percentiles`), over the
prices already retrieved and guard-passed in stage 6 — not a separate
Elasticsearch aggs query. (An earlier draft of this doc planned the
percentiles as an ES `percentiles` agg; the implementation ended up simpler:
by stage 7 the matched comps are already in hand as `Comp` objects, so
there's no reason to round-trip back to the cluster to reduce them.)
Nearest-rank, not interpolation, so every reported number is a price
something actually sold for, not an invented point between two real ones.

**Condition-bucketed, with a size-aware fallback.**
`match.condition_matched_prices` prefers prices from comps sharing the
item's own condition grade (photo grade, or `listed_condition` — see
Vision), but only when that bucket has **at least `min_comps`** comps in
it — not merely "at least one". Before real per-comp condition grading
existed (vision off, or comps ungraded), this distinction never mattered:
every comp's condition was `unknown`, so the bucket was always either
"everything" or empty. Once vision started grading comps for real, an
exact-condition bucket could legitimately have 2 comps against 8
total — narrowing to just those 2 (below `MIN_COMPS`) reported
"insufficient data" in cases where a *blended* band across all conditions
would have been perfectly quotable. Falls back to every priced comp,
regardless of condition, when the narrow bucket is too small or the grade
is unknown.

**If the resulting set is still smaller than `MIN_COMPS` (default 5), return
"insufficient data" and show the comps anyway.** Never invent a median from
two data points. This is a real quality bar and a good thing to be asked
about. Applied independently to the sold set (gates the whole verdict — see
below) and, informationally, to the local set.

**The local band gets an additional trust check before it's allowed to
influence anything.** `pricing.local_band_is_trustworthy` compares the local
band's own spread (`p90 / p25`) against the sold band's (which already
passed its `MIN_COMPS` gate, making it the more reliable anchor): a local
spread wildly wider than the sold band's own is a strong signal that at
least one comp is simply the wrong item, not a real price signal — Facebook
Marketplace text-matching is noisier than eBay sold comps, and a full
water-cooling loop or an industrial coolant system can both match a $15
tube on shared vocabulary alone under weak (title-only) matching. When
distrusted, `local_net` is set to `None` (eBay wins by default) and the
reason text says so plainly ("Local asks looked scattered/mismatched —
ignored for pricing"), rather than silently using a contaminated number.

## Recommendation (stage 8)

Pure functions over the aggregation output — no LLM in this step. It's
arithmetic, it must be reproducible, and it's the part under mutation test.

```python
EBAY_FVF_RATE   = 0.1325   # varies by category — configurable, not gospel
EBAY_FIXED_FEE  = 0.40
FB_LOCAL_RATE   = 0.0      # in-person is free

ebay_net    = sold_band.p50  * (1 - EBAY_FVF_RATE) - EBAY_FIXED_FEE - shipping_est
local_net   = local_band.p50 * (1 - FB_LOCAL_RATE)          # None if untrustworthy (see above)
current_net = ask_price      * (1 - EBAY_FVF_RATE) - EBAY_FIXED_FEE - shipping_est

opportunity = (local_net if local_net > ebay_net else ebay_net) - current_net
```

eBay wins ties (national reach beats a marginal local premium). Recommend
the higher net, and report the delta in dollars — *"sell locally, net $60
more"* is the sentence the whole app exists to produce. `shipping_est` is a
coarse lookup by `size_class` from the vision attributes; it's an estimate
and the UI says so. Fee rates live in config — they vary by category and
change; hardcoding them as truth would be wrong.

**`target` tracks whichever venue is actually recommended, not always the
eBay sold band.** Showing "Target $17" next to "sell local" (where asks run
$50) contradicted the recommendation on its face — a real user-reported
confusion. `target`/`target_low`/`target_high` are drawn from `local_band`
when `recommended_venue` is `fb_local`, from `sold_band` otherwise; the
frontend tags the number "local" when it isn't the eBay-sold figure shown
in the adjacent column.

**`current_net` is exposed on the `Verdict`, not just used internally.**
`opportunity_usd` is computed from `current_net` (net proceeds *at the
current ask price*) vs. the winning venue's net — but `current_net` wasn't
originally a field on `Verdict`, only `ebay_net` was (net proceeds *at the
eBay sold median*, a different price than the current ask, computed for a
different purpose). Both numbers can look superficially similar, which made
`opportunity_usd` read as unreconcilable from what was shown — a user (or a
reviewer) trying to verify "$50 target minus what number equals +$53
opportunity" had no way to find that second number anywhere in the API
response. `current_net` is now a first-class field, and the frontend states
both nets side by side ("You'd net -$3 today ... vs. $50 ... — the gap is
the opportunity number") so the arithmetic is checkable by eye. See
`test_pricing.py::TestVerdictReconciliation` for the golden-case regression
suite this bug produced.

**The verdict `kind` (under/over/fair) and the recommended venue are
independent judgments, and the reason text must never let that read as a
contradiction.** `kind` is judged only against the eBay sold band; the
venue is whichever nets more, independently. "Fair" (vs. eBay) next to a
large "opportunity" and "sell locally" is not a bug — it means the ask is
reasonable *for eBay* specifically, while local buyers are paying more for
the same thing — but reporting the two facts without connecting them reads
as one. `pricing._reason` states both in one sentence for every
kind × venue combination, and gets the *why* right when local wins: local
can win either because local asks are genuinely higher, or purely by
avoiding eBay's fee and shipping even at an equal or lower local price (an
AmazonBasics cable: local median $5 vs. eBay's $7, yet local still nets
more once eBay's cut and shipping are subtracted) — the reason text
compares `local_band.p50` to `sold_band.p50` and states whichever is
actually true; claiming "local asks run higher" in the fee-driven case
would be false and checkable-as-wrong against the local band shown right
next to it.

## Tier 3 — dry run only

Given an eBay seller API key, render the exact revise-price call that *would*
be made, with a copy button. **No execution path exists in the code.**

Rationale: the OAuth user-token flow plus the Inventory/Trading revise API is
a large chunk of work, and mutating live listings from a demo is a bad idea.
The payload preview demonstrates the capability at zero risk. Say so plainly
rather than shipping a button that does nothing.

The key is accepted per-request, used to render, and never persisted.

## Backend surface (FastAPI)

```
GET  /health                                    -> { apify/elastic/vision configured, defaults }
POST /api/analyze          { store_url, metro } -> { job_id }
GET  /api/jobs/{job_id}                         -> { status, stage, progress, error }
GET  /api/jobs/{job_id}/items                   -> [ item + verdict + comps ]
POST /api/items/{id}/revise-payload  { api_key } -> { payload }   # dry run, never executed
DELETE /api/cache                               -> { cleared: <count> }
```

### Jobs

Scraping takes minutes, so `/analyze` returns immediately and work runs in a
FastAPI background task. Job state is an **in-process dict**
(`jobs.JobRegistry`), not Elasticsearch — no Redis, no Celery, one less
container to fail on demo night. The real trade-off: state dies with the
process, and restarting the backend mid-analysis silently orphans any job
in flight (its result is simply never retrievable again — this has actually
happened during dev when a code change required a restart while a job was
running). That's the right trade for this app's current lifetime, not a
decision to forget was made. The frontend polls `GET /api/jobs/{id}` every 2s.

Stages report progress so the UI can narrate (`scraping store`, `reading
photos`, `finding comps`, …) instead of showing a dead spinner for four
minutes. That narration is most of the perceived speed.

## Frontend (React)

Single page, three states: empty → running → results.

- **Results are a dense table**, not a chat. One row per listing: thumbnail,
  title, your ask, the band, condition chip, target price, venue call, and a
  delta in dollars colored by direction.
- **Sort by "money left on the table" descending** by default. The most
  valuable row is the first thing on screen, which is also the demo.
- **Every row expands** to its comps with photos, prices, and the RRF score —
  the audit trail from § Guarding against semantic drift.
- **Skeleton rows stream in** as items complete, rather than blocking on the
  whole job.

See DEVELOP.md § Design language.

## Configuration

```
APIFY_TOKEN=
ELASTICSEARCH_ENDPOINT=          # the .es. URL, not .kb.
ELASTICSEARCH_API_KEY=

# Vision: Anthropic by default, or any OpenAI-compatible server.
ANTHROPIC_API_KEY=
ANTHROPIC_MODEL=claude-sonnet-5
VISION_PROVIDER=anthropic        # or "openai"
VISION_BASE_URL=                 # openai provider only, e.g. http://host:port/v1
VISION_API_KEY=
VISION_MODEL=
VISION_CACHE_TTL_HOURS=20

ACTOR_STORE=memo23/ebay-search-scraper-ppe
ACTOR_SOLD=memo23/ebay-search-scraper-ppe
ACTOR_LOCAL=apify/facebook-marketplace-scraper
APIFY_CACHE_TTL_HOURS=20
COMP_CONCURRENCY=3                # keep low: Apify account-wide memory cap

MIN_COMPS=5
RERANK_TOP_K=8
EBAY_FVF_RATE=0.1325
```

Actor slugs are config, not constants — swapping a failed actor at 7pm must be
an env change, not a code change. **Verify any actor change live before
trusting it** — see § Data sources for why (three actors were tried and
silently misbehaved before landing on the current ones).

## Known limitations

Say these out loud rather than being caught by them:

- FB comps are *asking* prices; only eBay comps are *sold*. The recommendation
  weights them differently and the UI labels which is which.
- No listing dates from FB, so no staleness signal on local comps.
- City-level location only; "local" means the metro, not a radius.
- Condition grading from a single photo misses interior/back/underside damage.
- Fee rates are category-dependent approximations.
- Sold-comp actor is third-party; its coverage and freshness are unaudited.
- `QUANTITY_SCALE_EXPONENT` (capacity/spec price scaling) is an uncalibrated
  heuristic — validated against one real product line's rough shape, not
  fit against real paired same-product-different-spec sales data.
- Product model/generation within a brand isn't a recognized attribute —
  see § What isn't attribute-gated yet.
- Retrieval is not scoped to one job's data; `sellowl-comps` accumulates
  across every job that has ever run. Harmless for correctness (comps
  upsert by `doc_id`, not duplicate) but means the index grows unbounded
  with no eviction — fine for a demo, not for a long-lived deployment.
- Job state is an in-process dict; a backend restart mid-job orphans it
  (see § Jobs).
