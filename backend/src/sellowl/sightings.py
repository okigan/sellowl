"""How long a listing has been sitting unsold.

A listing that has not moved in two months is telling you something the comps
cannot: the ask is above what this market will actually pay. eBay's search
output has no "listed on" date (see docs/DESIGN.md § Data sources), so the age
is *observed* rather than scraped -- the first time a listing is seen it is
written down, and every later run measures from there.

Deliberately NOT in `cache.py`, despite the similar shape. That module is a
TTL cache whose entries are meant to expire and whose whole point is being
disposable (`DELETE /api/cache` wipes it). This ledger is the opposite: it is
the only record that a listing existed before today, and losing it silently
resets every item's age to zero. Different lifetime, different guarantees,
different directory.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from .logging import get_logger

log = get_logger(__name__)

SIGHTINGS_PATH = Path(".cache") / "sightings.json"


def _load(path: Path) -> dict[str, str]:
    try:
        raw = json.loads(path.read_text())
    except json.JSONDecodeError, OSError:
        return {}
    return raw if isinstance(raw, dict) else {}


def _key(store_url: str, external_id: str) -> str:
    return f"{store_url}|{external_id}"


def record_sightings(
    store_url: str,
    external_ids: list[str],
    *,
    now: datetime | None = None,
    path: Path = SIGHTINGS_PATH,
) -> dict[str, int]:
    """Record that these listings were seen now; return days-listed per id.

    A listing seen for the first time reports 0 days rather than None: it is
    genuinely "seen today", and the caller's threshold treats 0 as fresh. The
    distinction that matters downstream is not first-run-vs-not, it is
    whether enough days have passed to mean anything, and on a first run they
    haven't.
    """
    now = now or datetime.now(UTC)
    seen = _load(path)
    ages: dict[str, int] = {}
    changed = False

    for external_id in external_ids:
        key = _key(store_url, external_id)
        first_seen_raw = seen.get(key)
        first_seen: datetime | None = None
        if first_seen_raw:
            try:
                first_seen = datetime.fromisoformat(first_seen_raw)
            except ValueError:
                first_seen = None
        if first_seen is None:
            seen[key] = now.isoformat()
            changed = True
            ages[external_id] = 0
            continue
        ages[external_id] = max(0, (now - first_seen).days)

    if changed:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(seen, indent=0, sort_keys=True))
        except OSError as exc:  # a lost ledger must not fail the job
            log.warning("sightings_write_failed", error=str(exc))

    return ages
