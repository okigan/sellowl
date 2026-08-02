"""All configuration in one place.

Defaults are empty rather than required so the app boots, `make check` runs,
and the test suite passes without any credentials. Missing credentials fail at
the point of use with a clear message, not at import time.
"""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(".env", "../.env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- credentials -----------------------------------------------------
    anthropic_api_key: str = ""
    anthropic_model: str = "claude-sonnet-5"
    apify_token: str = ""
    elasticsearch_endpoint: str = ""
    elasticsearch_api_key: str = ""

    # --- vision ------------------------------------------------------------
    # "anthropic" uses anthropic_api_key/anthropic_model above. "openai" talks
    # to any OpenAI-compatible chat-completions endpoint (vLLM, llama.cpp
    # server, etc.) via vision_base_url/vision_api_key/vision_model — same
    # prompt and JSON contract either way, see vision.py.
    vision_provider: str = "anthropic"
    vision_base_url: str = ""
    vision_api_key: str = ""
    vision_model: str = ""
    # Comp photos repeat heavily across re-analyzes of the same store; cache
    # grades on disk like Apify runs. 0 disables. DELETE /api/cache clears it.
    #
    # 30 days, not hours: a grade is a function of (image, model, prompt), and
    # none of those drift on their own -- a used drive's photo doesn't change
    # because a day passed. The prompt is hashed into the key, so editing it
    # invalidates entries immediately regardless of TTL, which is the only
    # staleness that actually matters here. A short TTL just bought re-running
    # hundreds of identical model calls for nothing.
    vision_cache_ttl_hours: float = 24.0 * 30

    # --- actor slugs -----------------------------------------------------
    # Config, not constants: swapping a failed actor mid-event must be an env
    # change and a restart, never a code change.
    actor_store: str = "memo23/ebay-search-scraper-ppe"
    actor_sold: str = "memo23/ebay-search-scraper-ppe"
    actor_local: str = "apify/facebook-marketplace-scraper"

    # --- pipeline tuning -------------------------------------------------
    metro: str = "austin"
    # Convenience prefill for the UI. The app takes any store URL; this is
    # just what the input box starts with.
    default_store_url: str = "https://www.ebay.com/usr/pragm_14"
    min_comps: int = 5
    rerank_top_k: int = 8
    max_items: int = 12
    max_comps_per_query: int = 40
    vision_concurrency: int = 8
    # Concurrent Apify runs. One run per comp query, so this bounds fan-out.
    comp_concurrency: int = 5
    sold_days_back: int = 90
    match_score_floor: float = 0.0
    apify_timeout_s: float = 480.0
    # Apify runs are the slow part (minutes) and idempotent for a given
    # (actor, payload). Cache their results on disk; 0 disables caching.
    #
    # 7 days. Unlike vision grades this genuinely does go stale -- new comps
    # get listed and sold -- but a week-old comp set still prices an item far
    # better than a failed run does, and quota is finite (see the stale-cache
    # fallback in sources/apify.py, which serves even expired entries rather
    # than failing a job).
    apify_cache_ttl_hours: float = 24.0 * 7

    # --- fees ------------------------------------------------------------
    # Category-dependent approximations, not gospel. See docs/DESIGN.md.
    ebay_fvf_rate: float = 0.1325
    ebay_fixed_fee: float = 0.40
    fb_local_rate: float = 0.0
    # A Facebook comp is an asking price, not a sold one; this haircuts it
    # before it's treated as achievable proceeds. Uncalibrated, like the
    # rates above.
    fb_ask_discount: float = 0.85

    # --- search backend --------------------------------------------------
    # "elastic" (default, what the hack-night entry ships) or "sqlite" (the
    # self-hosted path in docs/MIGRATION.md: FTS5 BM25 + brute-force vectors,
    # no cluster). Both implement the same CompStore protocol, so this is the
    # only switch needed to compare them on the same store.
    search_backend: str = "elastic"
    sqlite_db_path: str = ".cache/comps.db"
    # Embeddings for the sqlite backend. Blank base_url uses the built-in
    # dependency-free hashing embedder -- see embeddings.py for why that is a
    # deliberately lexical stand-in and not a claim of semantics.
    embedding_base_url: str = ""
    embedding_api_key: str = ""
    embedding_model: str = ""

    # --- elastic ---------------------------------------------------------
    index_prefix: str = "sellowl"
    # Flipped to False by the phase-0 check if the cluster predates the
    # `retriever`/`rrf` syntax; match.py then fuses in Python instead.
    rrf_enabled: bool = True

    @property
    def index_comps(self) -> str:
        return f"{self.index_prefix}-comps"

    @property
    def index_items(self) -> str:
        return f"{self.index_prefix}-items"

    @property
    def elastic_configured(self) -> bool:
        return bool(self.elasticsearch_endpoint and self.elasticsearch_api_key)

    @property
    def vision_configured(self) -> bool:
        if self.vision_provider == "openai":
            return bool(self.vision_base_url and self.vision_api_key and self.vision_model)
        return bool(self.anthropic_api_key)


@lru_cache
def get_settings() -> Settings:
    return Settings()
