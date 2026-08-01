"""Tolerant field extraction from third-party scraper output.

The actor READMEs are not reliable about field names, and two of our three
actors are barely reviewed. So every field is looked up through a list of
candidate paths rather than a single hardcoded key, and anything unparseable
becomes None instead of an exception. A comp with a missing photo is still a
usable comp; a crashed pipeline is not.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

_MONEY_RE = re.compile(r"-?\d[\d,]*\.?\d*")


def dig(data: Any, path: str) -> Any:
    """Walk a dotted path, tolerating missing keys and list indices.

    `dig(d, "location.reverse_geocode.city")` or `dig(d, "images.0.url")`.
    """
    current = data
    for part in path.split("."):
        if current is None:
            return None
        if isinstance(current, list):
            if not part.isdigit():
                return None
            idx = int(part)
            current = current[idx] if idx < len(current) else None
        elif isinstance(current, dict):
            current = current.get(part)
        else:
            return None
    return current


def first(data: Any, *paths: str) -> Any:
    """First non-empty value among candidate paths."""
    for path in paths:
        value = dig(data, path)
        if value not in (None, "", [], {}):
            return value
    return None


def as_price(value: Any) -> float | None:
    """Parse a price from the several shapes scrapers use.

    Handles 65, 65.0, "65.00", "$1,234.56", "US $20.00", {"value": ...},
    {"amount": ...}, {"formatted_amount": "$65"}.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value) if value > 0 else None
    if isinstance(value, dict):
        nested = first(
            value,
            "amount",
            "value",
            "raw",
            "formatted_amount",
            "convertedFromValue",
            "__value__",
        )
        return as_price(nested) if nested is not None else None
    if isinstance(value, str):
        match = _MONEY_RE.search(value.replace(",", ""))
        if not match:
            return None
        try:
            parsed = float(match.group())
        except ValueError:
            return None
        return parsed if parsed > 0 else None
    return None


def as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list) and value:
        return as_text(value[0])
    return str(value).strip()


def as_url(value: Any) -> str:
    """Extract a URL from a string, or from the several wrapper shapes scrapers
    use (`{"url": ...}`, `{"uri": ...}`, `[{"url": ...}]`).

    Actors disagree about whether an image field is a string or an object, and
    a stringified dict in a photo field silently breaks every downstream fetch.
    """
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        for entry in value:
            found = as_url(entry)
            if found:
                return found
        return ""
    if isinstance(value, dict):
        nested = first(value, "url", "uri", "href", "src", "imageUrl", "image.uri", "image.url")
        return as_url(nested) if nested is not None else ""
    return ""


def as_date(value: Any) -> datetime | None:
    """Parse a date from ISO strings, epoch seconds/millis, or common formats."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        # Heuristic: anything past ~2001 in seconds is below 1e12 in millis.
        seconds = value / 1000.0 if value > 1e11 else float(value)
        try:
            return datetime.fromtimestamp(seconds, tz=UTC)
        except OverflowError, OSError, ValueError:
            return None
    if not isinstance(value, str):
        return None
    # memo23 emits "Sold  4 Jun 2026" (note the double space).
    text = value.strip().removeprefix("Sold").removeprefix("sold").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        for fmt in ("%b %d, %Y", "%d %b %Y", "%Y/%m/%d", "%m/%d/%Y", "%d %B %Y", "%B %d, %Y"):
            try:
                parsed = datetime.strptime(text, fmt)
                break
            except ValueError:
                continue
        else:
            return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def as_str_map(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    return {str(k): as_text(v) for k, v in value.items() if v not in (None, "", [], {})}
