"""Claude vision: a listing photo becomes the query.

Your own listing titles are bad — everyone's are ("vintage bowl green"). The
photo isn't. So the photo generates the canonical description we match on, the
condition grade nothing else captures, and the search query we hand the
scrapers. One call, three jobs.
"""

from __future__ import annotations

import asyncio
import base64
import json
from typing import Any, Literal

from anthropic import AsyncAnthropic
from anthropic.types import ImageBlockParam, TextBlockParam

from .logging import get_logger
from .models import Condition, VisionResult

log = get_logger(__name__)

MediaType = Literal["image/jpeg", "image/png", "image/gif", "image/webp"]

_MEDIA_TYPES: dict[bytes, MediaType] = {
    b"\xff\xd8\xff": "image/jpeg",
    b"\x89PNG": "image/png",
    b"GIF8": "image/gif",
    b"RIFF": "image/webp",
}

PROMPT = """\
You are appraising a second-hand item from its listing photo, for resale pricing.

Return ONLY a JSON object, no prose, with exactly these keys:
  canonical_description  A dense factual description someone would search for:
                         object type, material, style, era, distinguishing
                         features. No marketing language. Max 30 words.
  attributes             Object with any of: material, brand, era, style,
                         size_class (one of: small, medium, large, xlarge),
                         color. Omit keys you cannot determine from the photo.
                         Never guess a brand.
  condition              Exactly one of: "rough", "usable", "clean".
                         rough  = damaged, missing parts, heavy wear, parts-only
                         usable = works, honest wear, cosmetic flaws visible
                         clean  = no visible flaws, looks near-new
  condition_evidence     What in the photo justifies that grade. Cite visible
                         detail. One sentence. If the photo is too poor to
                         judge, say so here and grade "usable".
  search_query_broad     2-4 plain words a casual seller would title this with.
                         No model numbers, no jargon. This goes to a scraper.
  search_query_narrow    A precise query including model/brand if visible.

The listing title is: {title}
Treat the title as a hint only; trust the photo where they disagree.\
"""


def _media_type(data: bytes) -> MediaType:
    for magic, mime in _MEDIA_TYPES.items():
        if data.startswith(magic):
            return mime
    return "image/jpeg"


def parse_vision_json(text: str) -> VisionResult:
    """Parse the model's reply, tolerating fences and surrounding prose."""
    body = text.strip()
    if "```" in body:
        chunks = body.split("```")
        for chunk in chunks:
            candidate = chunk.removeprefix("json").strip()
            if candidate.startswith("{"):
                body = candidate
                break
    start, end = body.find("{"), body.rfind("}")
    if start == -1 or end == -1 or end < start:
        return VisionResult()
    try:
        raw = json.loads(body[start : end + 1])
    except json.JSONDecodeError:
        return VisionResult()
    if not isinstance(raw, dict):
        return VisionResult()

    attrs_raw = raw.get("attributes")
    attributes = (
        {str(k): str(v) for k, v in attrs_raw.items() if v not in (None, "")}
        if isinstance(attrs_raw, dict)
        else {}
    )
    try:
        condition = Condition(str(raw.get("condition", "")).strip().lower())
    except ValueError:
        condition = Condition.UNKNOWN

    return VisionResult(
        canonical_description=str(raw.get("canonical_description", "")).strip(),
        attributes=attributes,
        condition=condition,
        condition_evidence=str(raw.get("condition_evidence", "")).strip(),
        search_query_broad=str(raw.get("search_query_broad", "")).strip(),
        search_query_narrow=str(raw.get("search_query_narrow", "")).strip(),
    )


class VisionGrader:
    def __init__(self, api_key: str, model: str, concurrency: int = 8) -> None:
        self._client = AsyncAnthropic(api_key=api_key) if api_key else None
        self._model = model
        self._sem = asyncio.Semaphore(concurrency)

    @property
    def enabled(self) -> bool:
        return self._client is not None

    async def grade(self, photo: bytes | None, title: str) -> VisionResult:
        """Grade one photo. Never raises: a failed grade degrades one row."""
        if self._client is None or not photo:
            return _fallback(title)

        image_block: ImageBlockParam = {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": _media_type(photo),
                "data": base64.standard_b64encode(photo).decode(),
            },
        }
        text_block: TextBlockParam = {"type": "text", "text": PROMPT.format(title=title)}

        async with self._sem:
            try:
                message = await self._client.messages.create(
                    model=self._model,
                    max_tokens=600,
                    messages=[{"role": "user", "content": [image_block, text_block]}],
                )
            except Exception as exc:  # noqa: BLE001 - one bad photo must not kill a job
                log.warning("vision_call_failed", title=title[:60], error=str(exc))
                return _fallback(title)

        text = "".join(
            str(getattr(block, "text", ""))
            for block in message.content
            if getattr(block, "type", "") == "text"
        )
        result = parse_vision_json(text)
        if not result.canonical_description:
            result.canonical_description = title
        if not result.search_query_broad:
            result.search_query_broad = _broad_from_title(title)
        return result

    async def grade_many(self, photos: list[tuple[bytes | None, str]]) -> list[VisionResult]:
        return list(await asyncio.gather(*(self.grade(p, t) for p, t in photos)))


def _fallback(title: str) -> VisionResult:
    """No key, no photo, or a failed call: degrade to the title."""
    return VisionResult(
        canonical_description=title,
        condition=Condition.UNKNOWN,
        condition_evidence="No photo available to grade.",
        search_query_broad=_broad_from_title(title),
        search_query_narrow=title,
    )


def _broad_from_title(title: str) -> str:
    """Strip model numbers and noise down to a query a casual seller would use."""
    words = [w for w in title.split() if not any(ch.isdigit() for ch in w)]
    return " ".join(words[:4]) if words else title[:40]


class Attributes:
    """Attribute keys the pipeline treats as meaningful."""

    SIZE_CLASS = "size_class"
    MATERIAL = "material"
    BRAND = "brand"
    ERA = "era"

    @staticmethod
    def merge(*sources: dict[str, str]) -> dict[str, Any]:
        merged: dict[str, Any] = {}
        for src in sources:
            merged.update({k: v for k, v in src.items() if v})
        return merged
