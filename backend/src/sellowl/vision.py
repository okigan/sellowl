"""Vision: a listing photo becomes the query.

Your own listing titles are bad — everyone's are ("vintage bowl green"). The
photo isn't. So the photo generates the canonical description we match on, the
condition grade nothing else captures, and the search query we hand the
scrapers. One call, three jobs.

Two interchangeable backends, same prompt and JSON contract:
- "anthropic": Claude, via the anthropic SDK.
- "openai": any OpenAI-compatible chat-completions server (vLLM, llama.cpp
  server, etc.) — e.g. a local Qwen3-VL/Qwen3.6 vision model. Selected by
  `Settings.vision_provider`; see config.py.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
from typing import Any, Literal

from anthropic import AsyncAnthropic
from anthropic.types import ImageBlockParam, TextBlockParam
from openai import AsyncOpenAI
from openai.types.chat import (
    ChatCompletionContentPartImageParam,
    ChatCompletionContentPartTextParam,
    ChatCompletionUserMessageParam,
)

from .cache import VISION_CACHE_DIR, cache_get, cache_key, cache_set
from .config import Settings
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
  attributes             Object with any of: category, material, brand, era,
                         style, size_class (one of: small, medium, large,
                         xlarge), color, capacity. Omit keys you cannot
                         determine from the photo. Never guess a brand.
                         category  What KIND of object this is, 1-3 plain
                         words (e.g. "USB flash drive", "padlock", "case
                         fan", "radiator"). Keep it short and generic, not a
                         precise model description.
                         capacity  A storage/volume/quantity spec printed on
                         the item or its packaging (e.g. "4GB", "64GB",
                         "2-pack", "500ml"). Only from text you can actually
                         read — never estimate or guess this one. Two
                         otherwise-identical items with different capacity
                         are a different product, not a variant.
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

# Cached results are keyed in part on this: editing the prompt (e.g. adding an
# attribute) must invalidate old cache entries automatically rather than
# silently serving stale shapes from before the change for up to the TTL.
_PROMPT_VERSION = hashlib.sha256(PROMPT.encode()).hexdigest()[:12]


def _media_type(data: bytes) -> MediaType:
    for magic, mime in _MEDIA_TYPES.items():
        if data.startswith(magic):
            return mime
    return "image/jpeg"


# Not every model sticks to the three-bucket vocabulary the prompt asks for
# (e.g. a general-purpose vision model answering "new" instead of "clean").
# Map common synonyms rather than silently discarding the grade as unknown.
_CONDITION_SYNONYMS: dict[str, str] = {
    "new": "clean",
    "brand new": "clean",
    "like new": "clean",
    "mint": "clean",
    "excellent": "clean",
    "good": "usable",
    "fair": "usable",
    "used": "usable",
    "pre-owned": "usable",
    "preowned": "usable",
    "worn": "usable",
    "fair condition": "usable",
    "poor": "rough",
    "damaged": "rough",
    "broken": "rough",
    "for parts": "rough",
    "not working": "rough",
}


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
    condition_text = str(raw.get("condition", "")).strip().lower()
    condition_text = _CONDITION_SYNONYMS.get(condition_text, condition_text)
    try:
        condition = Condition(condition_text)
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
    def __init__(self, settings: Settings) -> None:
        self._provider = settings.vision_provider
        self._anthropic: AsyncAnthropic | None = None
        self._openai: AsyncOpenAI | None = None
        self._model = ""

        if self._provider == "openai":
            if settings.vision_base_url and settings.vision_api_key:
                self._openai = AsyncOpenAI(
                    base_url=settings.vision_base_url, api_key=settings.vision_api_key
                )
                self._model = settings.vision_model
        elif settings.anthropic_api_key:
            self._anthropic = AsyncAnthropic(api_key=settings.anthropic_api_key)
            self._model = settings.anthropic_model

        self._sem = asyncio.Semaphore(settings.vision_concurrency)
        self._cache_ttl_s = settings.vision_cache_ttl_hours * 3600

    @property
    def enabled(self) -> bool:
        return self._anthropic is not None or self._openai is not None

    async def grade(self, photo: bytes | None, title: str) -> VisionResult:
        """Grade one photo. Never raises: a failed grade degrades one row.

        Comp photos repeat heavily across re-analyzes of the same store (the
        same eBay sold / Facebook comps get retrieved every time), and each
        call costs a couple of seconds — cached on disk like Apify runs.
        """
        if not self.enabled or not photo:
            return _fallback(title)

        digest = hashlib.sha256(photo).hexdigest()
        key = cache_key("vision_grade", _PROMPT_VERSION, self._provider, self._model, digest, title)
        if self._cache_ttl_s > 0:
            cached = cache_get(key, ttl_seconds=self._cache_ttl_s, cache_dir=VISION_CACHE_DIR)
            if cached is not None:
                return VisionResult.model_validate(cached)

        async with self._sem:
            try:
                if self._openai is not None:
                    text = await self._grade_openai(self._openai, photo, title)
                else:
                    assert self._anthropic is not None
                    text = await self._grade_anthropic(self._anthropic, photo, title)
            except Exception as exc:  # noqa: BLE001 - one bad photo must not kill a job
                log.warning("vision_call_failed", title=title[:60], error=str(exc))
                return _fallback(title)

        result = parse_vision_json(text)
        if not result.canonical_description:
            result.canonical_description = title
        if not result.search_query_broad:
            result.search_query_broad = _broad_from_title(title)
        if self._cache_ttl_s > 0:
            cache_set(key, result.model_dump(mode="json"), cache_dir=VISION_CACHE_DIR)
        return result

    async def _grade_anthropic(self, client: AsyncAnthropic, photo: bytes, title: str) -> str:
        image_block: ImageBlockParam = {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": _media_type(photo),
                "data": base64.standard_b64encode(photo).decode(),
            },
        }
        text_block: TextBlockParam = {"type": "text", "text": PROMPT.format(title=title)}
        message = await client.messages.create(
            model=self._model,
            max_tokens=600,
            messages=[{"role": "user", "content": [image_block, text_block]}],
        )
        return "".join(
            str(getattr(block, "text", ""))
            for block in message.content
            if getattr(block, "type", "") == "text"
        )

    async def _grade_openai(self, client: AsyncOpenAI, photo: bytes, title: str) -> str:
        data_url = f"data:{_media_type(photo)};base64,{base64.standard_b64encode(photo).decode()}"
        image_part: ChatCompletionContentPartImageParam = {
            "type": "image_url",
            "image_url": {"url": data_url},
        }
        text_part: ChatCompletionContentPartTextParam = {
            "type": "text",
            "text": PROMPT.format(title=title),
        }
        message: ChatCompletionUserMessageParam = {
            "role": "user",
            "content": [image_part, text_part],
        }
        response = await client.chat.completions.create(
            model=self._model,
            max_tokens=600,
            messages=[message],
        )
        return response.choices[0].message.content or ""

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
