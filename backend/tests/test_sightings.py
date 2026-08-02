"""Observed listing age.

eBay's search output has no "listed on" date, so age is measured from the
first time this app saw the listing. That makes the ledger's durability the
whole feature: lose it and every item silently looks brand new again.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from sellowl.sightings import record_sightings

STORE = "https://www.ebay.com/usr/someone"
NOW = datetime(2026, 8, 1, tzinfo=UTC)


class TestRecordSightings:
    def test_first_sighting_is_zero_days(self, tmp_path: Path) -> None:
        ages = record_sightings(STORE, ["a", "b"], now=NOW, path=tmp_path / "s.json")
        assert ages == {"a": 0, "b": 0}

    def test_second_run_measures_from_the_first(self, tmp_path: Path) -> None:
        path = tmp_path / "s.json"
        record_sightings(STORE, ["a"], now=NOW, path=path)
        ages = record_sightings(STORE, ["a"], now=NOW + timedelta(days=45), path=path)
        assert ages == {"a": 45}

    def test_first_seen_is_not_overwritten_by_later_runs(self, tmp_path: Path) -> None:
        """The bug that would quietly reset every age to zero."""
        path = tmp_path / "s.json"
        record_sightings(STORE, ["a"], now=NOW, path=path)
        record_sightings(STORE, ["a"], now=NOW + timedelta(days=10), path=path)
        ages = record_sightings(STORE, ["a"], now=NOW + timedelta(days=20), path=path)
        assert ages == {"a": 20}

    def test_a_new_listing_starts_its_own_clock(self, tmp_path: Path) -> None:
        path = tmp_path / "s.json"
        record_sightings(STORE, ["old"], now=NOW, path=path)
        ages = record_sightings(STORE, ["old", "new"], now=NOW + timedelta(days=30), path=path)
        assert ages == {"old": 30, "new": 0}

    def test_same_id_in_two_stores_is_tracked_separately(self, tmp_path: Path) -> None:
        path = tmp_path / "s.json"
        record_sightings(STORE, ["a"], now=NOW, path=path)
        ages = record_sightings("other-store", ["a"], now=NOW + timedelta(days=60), path=path)
        assert ages == {"a": 0}

    def test_survives_a_corrupt_ledger(self, tmp_path: Path) -> None:
        path = tmp_path / "s.json"
        path.write_text("{not json")
        assert record_sightings(STORE, ["a"], now=NOW, path=path) == {"a": 0}

    def test_clock_skew_never_reports_negative_age(self, tmp_path: Path) -> None:
        path = tmp_path / "s.json"
        record_sightings(STORE, ["a"], now=NOW, path=path)
        ages = record_sightings(STORE, ["a"], now=NOW - timedelta(days=5), path=path)
        assert ages == {"a": 0}

    def test_empty_input_is_harmless(self, tmp_path: Path) -> None:
        assert record_sightings(STORE, [], now=NOW, path=tmp_path / "s.json") == {}
