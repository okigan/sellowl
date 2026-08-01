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
    apify_cache_ttl_hours: float = 20.0

    # --- fees ------------------------------------------------------------
    # Category-dependent approximations, not gospel. See docs/DESIGN.md.
    ebay_fvf_rate: float = 0.1325
    ebay_fixed_fee: float = 0.40
    fb_local_rate: float = 0.0

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


@lru_cache
def get_settings() -> Settings:
    return Settings()
