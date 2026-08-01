# Migration off Apify and Elastic — planning doc

**Status: planning only. No code changes yet.** SellOwl was built for the
Apify × Elastic Hack Night and the submitted entry genuinely uses both end
to end — this document is a forward-looking option, not a decision to rip
either one out. If/when this app needs to run indefinitely (cost, rate
limits, or just wanting fewer hosted dependencies), here's what that looks
like.

## Why this might matter later

- **Cost at scale.** Apify actor runs and Elasticsearch (even a small
  Serverless project) both bill continuously. Fine for a demo store of 12
  items; a real multi-user product doing this on a schedule adds up.
- **Rate limits / blocking.** Apify's own scrapers get blocked by eBay and
  Facebook periodically (we hit this directly during dev — see
  `docs/DEVELOP.md` § Actor notes). That risk doesn't go away by
  self-hosting, but it becomes something we control the retry/backoff
  policy for instead of routing through a third party's actor.
- **Vendor lock-in on the search side.** The matching logic depends on
  Elastic-specific query syntax (`retriever`/`rrf`, `semantic_text`
  managed inference). That's genuinely convenient today, but it's also the
  single most Elastic-specific piece of the codebase.

None of this is urgent. This doc exists so that *if* it becomes worth doing,
there's already a plan instead of a scramble.

## What each dependency actually does today

| Dependency | Used for | Where in the code |
|---|---|---|
| Apify (`memo23/ebay-search-scraper-ppe`) | Scrape the seller's own store listings, and eBay sold/completed listings | `sources/store.py`, `sources/sold.py`, `sources/apify.py` |
| Apify (`apify/facebook-marketplace-scraper`) | Scrape local Facebook Marketplace asking prices | `sources/local.py`, `sources/apify.py` |
| Elasticsearch | Index all comps; hybrid BM25 + `semantic_text` retrieval; RRF fusion | `index.py` (`ElasticCompStore`) |

Two things already work in our favor:

- **`CompStore` is already a `Protocol`** (`index.py`), with a second real
  implementation (`MemoryCompStore`) used in tests today. Swapping the
  search backend means writing a third implementation of that same
  interface — no call-site changes anywhere else.
- **Apify access is centralized in one class** (`ApifyClient` in
  `sources/apify.py`), and the actor slug is already config, not code
  (`ACTOR_STORE`/`ACTOR_SOLD`/`ACTOR_LOCAL` in `.env`). Swapping the scraper
  backend means a new client behind the same `run_actor()` shape.

That's the leverage this plan uses: neither migration requires touching
`jobs.py`'s pipeline logic, `match.py`, or `pricing.py`.

## Migrating off Elastic

**Difficulty: low-to-medium. Do this one first if only doing one.**

The data volume here is small (hundreds of comps per job, not millions), so
a full distributed search engine is arguably overkill for this app's actual
scale — which makes this the easier and lower-risk migration.

Candidates, roughly in order of how little else has to change:

1. **Self-hosted OpenSearch.** Nearly API-compatible with the current
   `retriever`/`rrf` query shape used in `match.build_rrf_query`. Would
   still need a separate embedding step (OpenSearch's neural search needs
   its own configured pipeline, or embeddings computed application-side and
   stored as a `knn_vector`) since `semantic_text`'s managed inference is
   an Elastic Cloud–specific convenience. Lowest rewrite of `match.py`'s
   query-building logic; biggest remaining hosted dependency (still a
   cluster to run).
2. **Postgres + pgvector.** Compute embeddings ourselves (a small local
   model, or the same Anthropic/OpenAI-compatible endpoint already wired
   up for vision could also serve text embeddings). BM25-equivalent via
   Postgres full-text search (`tsvector`/`ts_rank`) or `pg_trgm`. RRF fusion
   is already implemented in pure Python (`match.rrf_fuse`) specifically so
   it isn't tied to Elastic's query-time fusion — this is exactly the
   fallback path that already runs today against older Elastic clusters,
   so **the fallback IS most of the pgvector implementation already**:
   two separate ranked-list queries, fused in Python.
3. **SQLite + sqlite-vec (or just a flat in-memory scan).** Given the
   actual scale (a handful of items × tens of comps each, per job), a
   from-scratch semantic layer might not need a real vector index at all —
   brute-force cosine similarity over a few hundred embeddings is
   milliseconds. This is close to what `MemoryCompStore` already does for
   the keyword side; extending it with real embeddings (rather than its
   current token-overlap heuristic) instead of introducing a new dependency
   at all is the lowest-dependency option, at the cost of not scaling past
   single-process, single-job memory.

**Recommended path:** implement a `PgVectorCompStore` (or extend
`MemoryCompStore` with real embeddings, if avoiding a database entirely is
preferred) behind the existing `CompStore` protocol, run it side-by-side
with `ElasticCompStore` behind a config flag, compare match quality on the
same store for a few runs, then flip the default.

## Migrating off Apify

**Difficulty: medium-to-high, and uneven across the three actors.** This is
the harder migration, and eBay vs. Facebook Marketplace are not
comparable difficulty.

- **eBay (store + sold listings): easier.** eBay has an official [Browse
  API](https://developer.ebay.com/api-docs/buy/browse/overview.html) and
  [Finding/Marketplace Insights APIs](https://developer.ebay.com/docs) for
  active and (with restricted access) sold listings. Since the user already
  has eBay Seller API access (per `docs/GOAL.md`'s Tier 3 design), the same
  credentials likely cover read access here too — this could plausibly
  *improve* reliability over scraping, not just replace it. This is the
  actor to migrate first.
- **Facebook Marketplace: hard.** There is no official API for Marketplace
  listings. Any self-hosted replacement is still a scraper (e.g. Playwright
  driving a real browser session), inherits the same blocking/ToS exposure
  Apify's actor already has, and additionally becomes our own
  infrastructure to keep working as Facebook's markup changes. This is
  realistically the long pole — worth explicitly deciding whether local
  Marketplace comps stay an Apify dependency indefinitely, or whether the
  "sell locally" venue comparison gets cut entirely if this can't be
  self-hosted sustainably. That's a product decision, not just an
  engineering one, and belongs in a follow-up conversation rather than
  buried in this doc.

**Recommended path:**
1. Introduce a `Scraper` protocol (`run(payload) -> list[dict]`, mirroring
   `ApifyClient.run_actor`'s shape) so `jobs.py` depends on the interface,
   not the Apify SDK/HTTP calls directly.
2. Build an `EbayApiScraper` against the official Browse API for the store
   + sold-listings legs — no scraping at all for this part once implemented.
3. Leave Facebook Marketplace on Apify's actor until/unless the product
   decision above says otherwise; it's already isolated to `sources/local.py`
   behind the same interface, so it doesn't block steps 1–2.

## What this doc is not

Not a timeline, not a commitment, and not a reason to touch anything before
the hack night judging concludes. The actual next step, if this becomes a
priority, is a spike: stand up `PgVectorCompStore` (or the embedded
`MemoryCompStore` extension) behind the existing protocol and compare
result quality against `ElasticCompStore` on the same store data — that
spike is small, reversible, and doesn't require deciding the Apify question
at all.
