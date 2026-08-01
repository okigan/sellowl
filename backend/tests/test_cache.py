"""On-disk cache: get/set/TTL/clear. Pure filesystem I/O, no network."""

from __future__ import annotations

import time
from pathlib import Path

from sellowl.cache import cache_clear, cache_get, cache_key, cache_set


class TestCacheKey:
    def test_same_args_same_key(self) -> None:
        assert cache_key("a", 1, {"x": 1}) == cache_key("a", 1, {"x": 1})

    def test_different_args_different_key(self) -> None:
        assert cache_key("a", 1) != cache_key("a", 2)


class TestCacheGetSet:
    def test_miss_is_none(self, tmp_path: Path) -> None:
        assert cache_get("nope", ttl_seconds=60, cache_dir=tmp_path) is None

    def test_set_then_get_round_trips(self, tmp_path: Path) -> None:
        cache_set("k", {"hello": "world"}, cache_dir=tmp_path)
        assert cache_get("k", ttl_seconds=60, cache_dir=tmp_path) == {"hello": "world"}

    def test_expired_entry_is_a_miss(self, tmp_path: Path) -> None:
        cache_set("k", "v", cache_dir=tmp_path)
        assert cache_get("k", ttl_seconds=0, cache_dir=tmp_path) is None

    def test_corrupt_file_is_a_miss_not_a_crash(self, tmp_path: Path) -> None:
        cache_set("k", "v", cache_dir=tmp_path)
        for f in tmp_path.glob("*.json"):
            f.write_text("not json")
        assert cache_get("k", ttl_seconds=60, cache_dir=tmp_path) is None

    def test_distinct_keys_do_not_collide(self, tmp_path: Path) -> None:
        cache_set("a", "value-a", cache_dir=tmp_path)
        cache_set("b", "value-b", cache_dir=tmp_path)
        assert cache_get("a", ttl_seconds=60, cache_dir=tmp_path) == "value-a"
        assert cache_get("b", ttl_seconds=60, cache_dir=tmp_path) == "value-b"

    def test_age_just_under_ttl_still_hits(self, tmp_path: Path) -> None:
        cache_set("k", "v", cache_dir=tmp_path)
        time.sleep(0.05)
        assert cache_get("k", ttl_seconds=60, cache_dir=tmp_path) == "v"


class TestCacheClear:
    def test_clears_nested_namespaces(self, tmp_path: Path) -> None:
        cache_set("k1", "v", cache_dir=tmp_path / "apify")
        cache_set("k2", "v", cache_dir=tmp_path / "vision")
        cleared = cache_clear(tmp_path)
        assert cleared == 2
        assert not tmp_path.exists()

    def test_missing_dir_clears_nothing(self, tmp_path: Path) -> None:
        assert cache_clear(tmp_path / "does-not-exist") == 0
