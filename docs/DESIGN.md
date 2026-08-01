# SellOwl — Design

## Pipeline

```
  eBay store URL
        │
        ▼
  ┌─────────────┐   Apify: crawlerbros/ebay-item-store-scraper
  │ 1. INVENTORY│   → your listings: title, ask price, photo
  └─────────────┘
        │
        ▼
  ┌─────────────┐   Claude vision, 1 call per item
  │ 2. VISION   │   → canonical description, attributes,
  │    (yours)  │     condition grade, and TWO search queries
  └─────────────┘
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

| Purpose | Actor | Users / rating | Fallback |
|---|---|---|---|
| Your inventory | `crawlerbros/ebay-item-store-scraper` | 40 / unrated | `delicious_zebu/ebay-product-listing-scraper` (694, 4.79★) |
| Sold comps | `caffein.dev/ebay-sold-listings` | 2430 / 4.0★ | `astronomical_reception/ebay-sold-lite` (5.0★), `memo23/ebay-search-scraper-ppe` |
| Local comps | `apify/facebook-marketplace-scraper` | 9.1K / 3.7★ | — |

**Verify every actor with a single throwaway run before building on it.** The
store scraper in particular is unrated. This is the top risk in the project.

**Do not use** `xtracto/ebay-sold-comps-scraper`. It returns a pre-computed
min/median/p90 band — convenient, and it steals the job that makes Elastic
interesting. Pull raw sold listings; compute percentiles ourselves.

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

### `sellowl-items` — your inventory

Same shape plus `ask_price`, `store_url`, `verdict` (the computed
recommendation, written back after stage 8 so the UI reads one document).

### `sellowl-jobs` — job state

`{ job_id, status, stage, progress, error, created_at }`. Avoids needing Redis;
the frontend polls this. See § Jobs.

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

> ⚠️ The `retriever` + `rrf` syntax requires a recent Elasticsearch. Serverless
> is fine. If RRF is unavailable, fall back to two separate queries fused in
> Python — the fusion is twenty lines and the argument for hybrid search
> survives either way. **Decide this in the first fifteen minutes.**

### Guarding against semantic drift

Semantic search will happily match a photo of a guitar to *every* guitar.
Three guards:

1. **Score floor** — drop comps below a configurable RRF score threshold.
2. **Attribute agreement** — the rerank stage (6) compares vision-extracted
   attributes (material, era, brand, size class); disagreement on a hard
   attribute drops the comp.
3. **Always show the comps.** Every verdict expands to the listings behind it,
   with photos. A human spots a bad match instantly, and a verdict you can't
   audit is a verdict nobody trusts.

## Vision (stages 2 and 6)

One Claude call per photo, returning structured JSON:

```jsonc
{
  "canonical_description": "mid-century teak sideboard, tapered legs, brass pulls, Danish style, 1960s",
  "attributes": { "material": "teak", "era": "1960s", "style": "danish modern", "size_class": "large" },
  "condition": "usable",
  "condition_evidence": "visible veneer chip on left door, original hardware intact, no structural damage",
  "search_query_broad": "teak sideboard",
  "search_query_narrow": "danish modern teak credenza"
}
```

Two design notes that matter:

**Condition is three buckets — `rough` / `usable` / `clean`.** Not 1–10. A
ten-point vision score is noise and collapses the moment someone asks how you
got a 7. Three buckets with cited evidence survive the question.

**Vision generates the scraper's search query too.** Your eBay title says
"Pyrex 444 Spring Blossom"; no Facebook seller types that. The broad query
goes to the scrapers, the canonical description goes to the matcher. One call,
two jobs.

## Aggregation (stage 7)

Over the condition-matched sold set only:

```jsonc
{
  "size": 0,
  "query": { "bool": { "filter": [
      { "terms": { "_id": ["<matched comp ids>"] } },
      { "term": { "venue": "ebay_sold" } },
      { "term": { "condition": "<your item's grade>" } } ] } },
  "aggs": { "band": { "percentiles": {
      "field": "price", "percents": [25, 50, 75, 90] } } }
}
```

**If the matched set is smaller than `MIN_COMPS` (default 5), return
"insufficient data" and show the comps anyway.** Never invent a median from
two data points. This is a real quality bar and a good thing to be asked about.

## Recommendation (stage 8)

Pure functions over the aggregation output — no LLM in this step. It's
arithmetic, it must be reproducible, and it's the part under mutation test.

```python
EBAY_FVF_RATE   = 0.1325   # varies by category — configurable, not gospel
EBAY_FIXED_FEE  = 0.40
FB_LOCAL_RATE   = 0.0      # in-person is free

ebay_net  = sold_median * (1 - EBAY_FVF_RATE) - EBAY_FIXED_FEE - shipping_est
local_net = local_median * (1 - FB_LOCAL_RATE)
```

Recommend the higher net, and report the delta in dollars — *"sell locally,
net $60 more"* is the sentence the whole app exists to produce.

`shipping_est` is a coarse lookup by `size_class` from the vision attributes.
It's an estimate and the UI says so.

Fee rates live in config. They vary by category and change; hardcoding them as
truth would be wrong.

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
POST /api/analyze          { store_url, metro } -> { job_id }
GET  /api/jobs/{job_id}                         -> { status, stage, progress }
GET  /api/jobs/{job_id}/items                   -> [ item + verdict + comps ]
POST /api/items/{id}/revise-payload  { api_key } -> { payload }   # dry run
```

### Jobs

Scraping takes minutes, so `/analyze` returns immediately and work runs in a
FastAPI background task. State lives in `sellowl-jobs` in Elasticsearch — no
Redis, no Celery, one less container. The frontend polls
`GET /api/jobs/{id}` every 2s.

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
ANTHROPIC_API_KEY=
ANTHROPIC_MODEL=claude-sonnet-5
APIFY_TOKEN=
ELASTICSEARCH_ENDPOINT=          # the .es. URL, not .kb.
ELASTICSEARCH_API_KEY=
ACTOR_STORE=crawlerbros/ebay-item-store-scraper
ACTOR_SOLD=caffein.dev/ebay-sold-listings
ACTOR_LOCAL=apify/facebook-marketplace-scraper
MIN_COMPS=5
RERANK_TOP_K=8
EBAY_FVF_RATE=0.1325
```

Actor slugs are config, not constants — swapping a failed actor at 7pm must be
an env change, not a code change.

## Known limitations

Say these out loud rather than being caught by them:

- FB comps are *asking* prices; only eBay comps are *sold*. The recommendation
  weights them differently and the UI labels which is which.
- No listing dates from FB, so no staleness signal on local comps.
- City-level location only; "local" means the metro, not a radius.
- Condition grading from a single photo misses interior/back/underside damage.
- Fee rates are category-dependent approximations.
- Sold-comp actors are third-party; their coverage and freshness are unaudited.
