"""On-disk cache for slow or repeated idempotent calls.

Apify actor runs take minutes; vision grading calls a model per photo. Both
repeat identical inputs across dev iterations and re-analyzes of the same
store, so caching them is the single biggest latency win available. One
JSON file per key, TTL checked on read, one subdirectory per namespace so
`DELETE /api/cache` can clear everything at once. Not for correctness-
sensitive data: callers decide what's cacheable.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import time
from pathlib import Path
from typing import Any

from .logging import get_logger

log = get_logger(__name__)

CACHE_ROOT = Path(".cache")
DEFAULT_CACHE_DIR = CACHE_ROOT / "apify"
VISION_CACHE_DIR = CACHE_ROOT / "vision"


def _key_path(cache_dir: Path, key: str) -> Path:
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
    return cache_dir / f"{digest}.json"


def cache_key(*parts: Any) -> str:
    return json.dumps(parts, sort_keys=True, default=str)


def cache_get(key: str, *, ttl_seconds: float, cache_dir: Path = DEFAULT_CACHE_DIR) -> Any | None:
    path = _key_path(cache_dir, key)
    if not path.exists():
        return None
    try:
        envelope = json.loads(path.read_text())
    except json.JSONDecodeError, OSError:
        return None
    age = time.time() - envelope["cached_at"]
    if age > ttl_seconds:
        return None
    log.info("cache_hit", key_digest=path.stem, age_s=round(age))
    return envelope["value"]


def cache_set(key: str, value: Any, *, cache_dir: Path = DEFAULT_CACHE_DIR) -> None:
    cache_dir.mkdir(parents=True, exist_ok=True)
    path = _key_path(cache_dir, key)
    path.write_text(json.dumps({"cached_at": time.time(), "value": value}))


def cache_clear(cache_dir: Path = CACHE_ROOT) -> int:
    """Clears every namespace under `cache_dir` (the whole cache by default)."""
    if not cache_dir.exists():
        return 0
    count = sum(1 for _ in cache_dir.rglob("*.json"))
    shutil.rmtree(cache_dir)
    log.info("cache_cleared", entries=count, cache_dir=str(cache_dir))
    return count
