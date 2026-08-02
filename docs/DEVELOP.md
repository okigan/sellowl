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
| E2E | Playwright — intended, never built (see below) |
| Search | Elastic Serverless (cloud), local ES via optional compose profile |

## Where code goes

`backend/src/sellowl/` is flat, one module per pipeline responsibility, with
`sources/` the only subpackage (one module per upstream, plus the shared
client). `frontend/src/` is `App.tsx` plus `components/` and a single `api.ts`
holding every fetch and every response type. Read the directory — it's small,
and it's the only description that can't go stale.

The rules that actually constrain where something belongs:

**Money math stays pure.** The pricing module takes aggregation numbers and
config in and returns a verdict — no I/O, no LLM, no Elasticsearch. That is
what makes it mutation-testable, and it's where a silent wrong answer would
hurt most. Anything needing a network call belongs on the other side of that
boundary, with the numbers passed in.

**Parse third-party JSON at the edge.** Scraper output is `dict[str, Any]`
until a source module turns it into a pydantic model; nothing downstream
should ever see a raw actor row. When an actor's real shape contradicts its
README — which has happened for every actor tried — the fixture and the
parser are the record of what's true.

**Retrieval and pricing are separate layers.** Retrieval decides *which*
listings are comparable; pricing decides what they imply. Guards that answer
"is this the same product?" belong with matching. Guards that answer "is this
number plausible?" belong with pricing.

**Match the storage guarantee to the data's lifetime.** The disk cache is
disposable by design — TTL'd, and `DELETE /api/cache` wipes it. Anything
whose loss would silently change an answer rather than just slow things down
needs its own durable store, even when the mechanics look identical. (Listing
age is the live example: it's the only record a listing existed before today,
so it deliberately does not live in the cache.)

**Job state is in-process and that's a choice, not an oversight.** One
process, one event loop, no Redis. A restart mid-job orphans it. See
DESIGN.md § Jobs before adding a second source of truth.

Playwright/e2e appears in the stack table as an intention. **It was never
built.** Coverage is backend pytest — including Hypothesis property tests —
plus `tsc --noEmit` on the frontend.

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
make mutate       # mutmut run, scoped — see [tool.mutmut] in pyproject.toml
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
surviving mutant tells you nothing. Point it only at code where a wrong answer
would be *silent and plausible*: money math, percentile selection, venue
choice, score thresholds, the minimum-comps guard. A flipped comparison in
any of those quietly recommends the wrong price while every test still
passes. The exact scope lives in `[tool.mutmut]` in `pyproject.toml`; extend
it when a new module starts deciding a number the user acts on, and target no
survivors there.

**Never hit live actors from tests** — they cost money and they're flaky.
Record one real response per upstream into a fixture and replay it. Those
fixtures double as documentation of the actual output shapes, which the actor
READMEs get wrong. Test HTTP-level behaviour (retries, cache fallbacks)
against a mocked transport rather than a live host.

**Write property tests for the "is this answer sane at all" class of bug.**
Golden cases only catch scenarios someone thought to write down. Fuzz the
verdict builder over its whole input space instead and assert what no correct
recommendation may *ever* do: claim an opportunity larger than every price in
play, estimate shipping at several times the item's own value, or recommend a
price outside the range real sales support. This is not redundant with the
golden cases — a $7 cable with a hallucinated $140 shipping estimate passed
every unit test in the suite and shipped a fabricated "+$138 opportunity" to
the UI, because each formula was individually correct and nothing checked
whether the *output* was plausible.

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
