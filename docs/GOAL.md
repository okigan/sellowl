# SellOwl — Goal

> 🦉 Paste your eBay store link. Find out what you're leaving on the table.

## The problem

People selling second-hand goods price by vibes. They copy whatever number the
last listing had, or they guess, and then the item sits for eight months at
$400 because nobody told them the real number is $180 — or it sells in a day
at $85 because the real number was $210.

Three facts they can't easily get:

1. **What the thing actually sells for.** Not what other people are *asking* —
   asking prices are fiction. Sold prices are the only real number.
2. **What it's worth in the condition it's actually in.** A "clean" example and
   a "rough" example of the same object are different products at different
   prices. No marketplace metadata captures this; it's only visible in photos.
3. **Where to sell it.** eBay takes ~13% plus shipping. Facebook Marketplace
   in-person takes nothing. For a heavy item, the venue decision is worth more
   than the price decision.

## What SellOwl does

Input: an eBay store URL. No account, no API key, no auth.

Output: a table of your listings, each with a verdict.

> **Teak sideboard** — you're asking **$85**
> Sold comps (clean, n=14): median **$210**, p25 $180, p75 $265
> Yours grades **usable** — visible veneer chip, original hardware
> Condition-matched target: **$150–$170**
> 3 local comps asking $190–$240, in-person
> **→ Relist locally at $180.** Net after eBay fees + shipping would be $122.

## Why this needs both tools

**Apify** — Facebook Marketplace has no API and is hostile to scraping; eBay
sold-price data is gated behind a limited-access API. Ready-made Actors get
all three of our sources in minutes instead of days, and we never run a
crawler ourselves.

**Elastic** — the hard problem is not fetching, it's *joining*. Three sources
with incompatible schemas whose only common key is meaning:

- `"Pyrex 444 Spring Blossom 4qt"` (your careful eBay title)
- `"green pyrex bowl big"` (a Facebook seller at 11pm)

Semantic search matches those. BM25 alone scores them as nearly unrelated. But
BM25 is what catches `Pyrex 444` when the model number *is* present — pure
vector search cheerfully matches 444 to 441. Neither half works alone, so we
fuse both with RRF. Then `percentiles` aggregations turn the matched set into
a price *distribution*, which is the actual shape of the question: "is $340
good" is a percentile question, and stuffing fifty listings into a context
window is the wrong tool for it.

## Tiers

| Tier | Needs | Delivers |
|---|---|---|
| **1** | a store URL | inventory table, comps found, sold price bands |
| **2** | nothing more | condition grading from photos, condition-matched target price, sell-here-not-there recommendation |
| **3** | eBay seller API key | renders the exact revise-price API call — **dry-run only** |

Tier 3 is deliberately not wired to execute. Mutating live listings from a
laptop is a bad idea, and the payload preview demonstrates the capability
without the risk. See DESIGN.md § Tier 3.

## Non-goals

- Not an aggregator or a search engine. We don't help you *find* things to buy.
- Not a chat interface. The output is a table with numbers in it.
- Not a general price oracle. Scoped to second-hand physical goods where
  condition drives price.
- No automated relisting, no bulk actions, no writes to any marketplace.

## Success criteria

**Hack night (must):**
- Paste a real store URL, get a populated verdict table, no crashes.
- At least one item where the recommendation is a genuine surprise with a
  dollar figure attached — that's the demo.
- Deployed and reachable at a URL.
- Honest empty states: fewer than `MIN_COMPS` sold comps says "not enough
  data", never invents a median.

**Judging rubric fit:**
- *Creativity* — photo-as-query and condition-matched pricing; the venue
  arbitrage output. Not another RAG chatbot.
- *Completeness* — works end to end on a real store, deployed.
- *Understanding* — see "Why this needs both tools" above. The RRF argument
  and the retrieve-then-rerank staging are the two things to be able to
  explain cold.

**Post-hackathon (nice):**
- Price history over time; alerting when a comp band moves.
- More venues (OfferUp, Craigslist, Mercari).
- Real write-back behind an explicit per-item confirmation.
