# SellOwl — Development

## Stack

| Layer | Choice |
|---|---|
| Runtime | Docker Compose |
| Backend | FastAPI, Python 3.14 (`python:3.14-slim`) |
| Package manager | `uv` |
| Types | mypy, strict |
| Lint + format | ruff |
| Tests | pytest, pytest-asyncio |
| Mutation tests | mutmut — scoped, see below |
| Frontend | React 19 + TypeScript + Vite |
| Styling | Tailwind |
| E2E | Playwright — added after tier 1 works, not before |
| Search | Elastic Serverless (cloud), local ES via optional compose profile |

## Layout

```
sellowl/
  docs/            GOAL.md DESIGN.md TODO.md DEVELOP.md MIGRATION.md
  backend/
    pyproject.toml
    src/sellowl/
      main.py          FastAPI app, routes
      config.py        pydantic-settings, all env
      jobs.py          background job orchestration + in-process job state
      sources/         apify.py (client) + store.py sold.py local.py parse.py
      vision.py        photo -> structured JSON (Anthropic or OpenAI-compatible)
      index.py         ES mappings, bulk upsert, hit -> Comp
      match.py         RRF retrieval + drift guards + numeric-spec scaling
      pricing.py       ← pure functions. percentiles -> verdict. mutation-tested.
      sightings.py     durable first-seen ledger -> days a listing has sat
      cache.py         TTL disk cache (apify + vision namespaces)
      logging.py       structlog setup
      models.py        pydantic models shared across the pipeline
    tests/
  frontend/
    src/
      App.tsx
      components/
      api.ts
  compose.yaml
  Makefile
```

Two things that were planned differently and are worth knowing:

- **Job and item state is an in-process dict** (`jobs.JobRegistry`), not an
  Elasticsearch index. Only `sellowl-comps` exists in the cluster. A restart
  mid-job orphans it; that's the accepted trade (see DESIGN.md § Jobs).
- **`sightings.py` is not part of `cache.py`** despite the similar shape. The
  cache is disposable and TTL'd (`DELETE /api/cache` wipes it); the sightings
  ledger is the only record that a listing existed before today, and losing
  it silently resets every item's age to zero.

Playwright/e2e is in the stack table as an intention. **It was never built** —
there is no `frontend/e2e/`. Coverage is backend pytest (including
Hypothesis property tests in `test_invariants.py`) plus `tsc --noEmit`.

`pricing.py` is deliberately pure and dependency-free: aggregation numbers and
config in, verdict out. No I/O, no LLM, no Elasticsearch. That's what makes it
testable, and it's where all the money math lives.

## Compose

```yaml
services:
  backend:
    build: ./backend
    ports: ["8000:8000"]
    env_file: .env
    volumes: ["./backend/src:/app/src"]      # hot reload
  frontend:
    build: ./frontend
    ports: ["5173:5173"]
    environment: [VITE_API_BASE=http://localhost:8000]
    volumes: ["./frontend/src:/app/src"]

  # optional: docker compose --profile local-es up
  elasticsearch:
    profiles: ["local-es"]
    image: docker.elastic.co/elasticsearch/elasticsearch:9.0.0
    environment:
      - discovery.type=single-node
      - xpack.security.enabled=false
```

Default path is **Elastic Serverless in the cloud** — that's what the hackathon
provisions and what `semantic_text` works on without deploying a model. The
local profile exists for offline work; note that `semantic_text` needs an
inference endpoint configured, so local is not a drop-in substitute.

## Commands

```bash
make dev          # compose up, backend + frontend, hot reload
make check        # ruff format --check && ruff check && mypy && pytest
make fix          # ruff format && ruff check --fix
make mutate       # mutmut run, scoped to pricing.py + match.py
make e2e          # placeholder — no Playwright suite exists yet, this fails
```

Run `make check` before every commit. It's fast enough that there's no excuse.

## Quality bar

**mypy strict.** `strict = true`, plus `disallow_any_explicit` where it
doesn't fight pydantic. Scraper output is the exception — third-party JSON of
unverified shape gets parsed into pydantic models at the boundary and is
`dict[str, Any]` before that. Parse at the edge, typed everywhere inside.

**ruff** for both lint and format. Line length 100. Enable `E`, `F`, `I`, `UP`,
`B`, `SIM`, `RUF`. No `# noqa` without a reason on the same line.

**Mutation testing is scoped, on purpose.** Running mutmut over the whole app
is theatre — most of it is I/O glue against third-party scrapers, where a
surviving mutant tells you nothing. Point it at the two modules where a silent
wrong answer is the actual product failure:

- `pricing.py` — fee math, percentile selection, venue choice, the
  `MIN_COMPS` guard. A flipped comparison here quietly recommends the wrong
  price, and every test would still pass.
- `match.py` — score thresholds and attribute-agreement logic. An off-by-one
  on the threshold silently widens or empties the comp set.

Target: no surviving mutants in `pricing.py`. `match.py` best-effort.

**Testing approach.** Record one real response from each Apify actor into
`tests/fixtures/` and replay it. Never hit live actors from tests — they cost
money and they're flaky. The fixtures double as documentation of the actual
output shapes, which the actor READMEs get wrong. HTTP-level Apify behaviour
(retries, the stale-cache fallback) is mocked with `respx`.

**Property tests for the "is this answer sane at all" class of bug.**
`tests/test_invariants.py` fuzzes `build_verdict` with Hypothesis over its
whole input space, asserting things no correct recommendation may ever do:
an opportunity larger than every price in play, a shipping estimate several
times the item's own value, a stale-trimmed target below the observed band.
Golden-case tests only catch scenarios someone thought to write down — a
$7 cable with a hallucinated $140 shipping estimate wasn't one of them, and
it shipped a fabricated "+$138 opportunity" to the UI as a result.

## Design language

"Nice and fast" means it *feels* instant even though scraping takes minutes.

**Fast:**
- Never block on the whole job. Stream rows in as items finish.
- Narrate the stage (`reading photos · 6 of 12`), never a bare spinner. Most
  of perceived speed is knowing something is happening.
- Skeleton rows with the right dimensions so nothing reflows on arrival.
- Optimistic sort — re-sort as verdicts land, with a brief highlight on the
  row that moved, so motion explains itself.

**Nice:**
- Dense over airy. This is a data tool; a table that shows twelve rows beats
  cards that show three.
- Tabular numerals for every price. Prices in a column must align.
- One accent color for money-left-on-the-table. Condition chips get three
  muted tones (rough / usable / clean). Nothing else is colored.
- Dark mode by default — it demos better in a dim room.
- The one animation that earns its place: the delta number counting up when a
  verdict lands.
- No modals. Rows expand in place.

## Conventions

- **Async all the way.** Scrapers, vision calls, and ES are all I/O; the
  pipeline fans out with `asyncio.gather` and bounded semaphores. Vision
  concurrency is capped (default 8) to stay under rate limits.
- **Every external call gets a timeout and one retry with backoff.** Apify runs
  are long-polled; treat "still running" and "failed" as different outcomes.
- **Secrets never leave the backend.** The eBay key in tier 3 is used within
  the request and never persisted or logged. Scrub tokens from all log lines.
- **Actor slugs come from config**, so swapping a broken actor mid-event is an
  env change and a restart.
- Commit messages: imperative, one line, scoped (`pricing: guard MIN_COMPS`).

## Order of work

Vertical slices, always shippable. Tier 1 end-to-end on one item beats tier 2
half-built across the pipeline — at any moment there should be something
demoable. See TODO.md for the time-boxed version and the cut list.

## Setup

```bash
cp .env.example .env      # fill in Anthropic, Apify, Elastic
make dev
```

Elastic Serverless: use the `.es.` endpoint, **not** the `.kb.` Kibana URL —
this is the single most common setup mistake and it fails with a confusing
error.
