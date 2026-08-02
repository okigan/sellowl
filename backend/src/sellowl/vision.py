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
  attributes             Object with any of: category, model, material,
                         brand, era, style, size_class (one of: small,
                         medium, large, xlarge), color, capacity, pack_count,
                         length, form_factor. Omit keys you cannot determine
                         from the photo. Never guess a brand or model.
                         category  What KIND of object this is, 1-3 plain
                         words (e.g. "USB flash drive", "padlock", "case
                         fan", "radiator"). Keep it short and generic, not a
                         precise model description.
                         size_class  The size of the SHIPPING BOX this item
                         would travel in, not the item's own length or a
                         number printed on it. A coiled cable, cord, or strap
                         ships in a small padded envelope no matter how many
                         feet long it is -- an "8 ft cable" is size_class
                         "small", the same as a keychain or a USB drive.
                         small   = fits in a padded envelope (cables, small
                                   electronics, jewelry, keys, cards)
                         medium  = fits in a shoebox-sized box (shoes, small
                                   appliances, stacked cable multi-packs)
                         large   = a large box, hand-carryable (desktop fans,
                                   monitors, small furniture)
                         xlarge  = requires freight or two hands and a cart
                                   (furniture, large appliances, radiators)
                         model  The specific product line or generation
                         printed on the item or packaging, if legible (e.g.
                         "Aegis Secure Key 3NX", "Riing Trio", "Pacific
                         C-Pro"). Different from `category`: two items can
                         share a category and brand and still be different
                         models with different typical prices. Only from
                         text you can actually read — never guess or infer
                         one from context.
                         The next four are NUMERIC SPECS. Each is a bare
                         "<number><unit>" and nothing else — no ranges, no
                         "+", no "10-in-1", no prose. Omit the key entirely
                         rather than writing "unspecified" or "40+ parts".
                         Read them off the item or packaging; never estimate.
                         Keep them in separate keys even when the packaging
                         prints them together: they price differently.
                         capacity     How much the thing holds — storage or
                         volume only (e.g. "4GB", "64GB", "500ml", "1TB").
                         pack_count   How many identical units are in the
                         package (e.g. "3-pack", "2-pack"). A single item is
                         "1-pack"; omit if not stated.
                         length       Physical length of a cable, cord,
                         tube, or strap (e.g. "6ft", "1.8m", "15ft").
                         form_factor  A size that names a product variant
                         rather than an amount — fan/radiator size, tube
                         diameter (e.g. "120mm", "240mm", "360mm"). This one
                         is NOT more-of-the-same: a 140mm fan is a different
                         fan, not a bigger quantity of fan.
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

    def _key(self, *parts: str) -> str:
        return cache_key("vision_grade", _PROMPT_VERSION, self._provider, self._model, *parts)

    def reference_key(self, identity: str, title: str) -> str:
        """Key for "this listing / this URL", answerable without a download.

        A grade depends on (image, model, prompt) and the honest key for the
        image is its content hash -- but that can only be computed *after*
        fetching the image, which is most of the cost this cache exists to
        avoid. Every re-analyze was re-downloading several hundred comp photos
        purely to compute keys it already had answers for.

        A listing id (or a photo URL) is known for free and the photo behind
        it doesn't change, so it answers the common case at zero network cost.
        The title is in the key because it is interpolated into the prompt.
        """
        return self._key("ref", identity, title)

    def content_key(self, photo: bytes, title: str) -> str:
        """Key for "this exact image", regardless of where it came from.

        The second half of the pair: eBay and Facebook serve the same picture
        under different (and, for FB, expiring signed) URLs, and sellers
        relist the same photo under a new id. Those all miss the reference key
        and would be re-graded despite being byte-identical to something
        already known.
        """
        return self._key("content", hashlib.sha256(photo).hexdigest(), title)

    def cached_grade(self, key: str | None) -> VisionResult | None:
        """A previously-stored grade, or None."""
        if key is None or self._cache_ttl_s <= 0:
            return None
        cached = cache_get(key, ttl_seconds=self._cache_ttl_s, cache_dir=VISION_CACHE_DIR)
        return VisionResult.model_validate(cached) if cached is not None else None

    def _remember(self, result: VisionResult, *keys: str | None) -> None:
        if self._cache_ttl_s <= 0:
            return
        payload = result.model_dump(mode="json")
        for key in keys:
            if key is not None:
                cache_set(key, payload, cache_dir=VISION_CACHE_DIR)

    async def grade(
        self, photo: bytes | None, title: str, *, identity: str | None = None
    ) -> VisionResult:
        """Grade one photo. Never raises: a failed grade degrades one row.

        Two-level cache. The reference key (listing id / URL) is checked first
        because it costs no network; the content key catches the same image
        arriving from a different URL. A content hit writes the reference key
        back, so the next run for that listing skips the download too.
        """
        if not self.enabled or not photo:
            return _fallback(title)

        ref_key = self.reference_key(identity, title) if identity else None
        cached_result = self.cached_grade(ref_key)
        if cached_result is not None:
            return cached_result

        content_key = self.content_key(photo, title)
        cached_result = self.cached_grade(content_key)
        if cached_result is not None:
            # Same picture, new URL or new listing id. Alias it so this
            # listing is answerable without a download next time.
            self._remember(cached_result, ref_key)
            return cached_result

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
        self._remember(result, ref_key, content_key)
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

    async def grade_many(
        self,
        photos: list[tuple[bytes | None, str]],
        *,
        identities: list[str | None] | None = None,
    ) -> list[VisionResult]:
        ids: list[str | None] = identities or [None] * len(photos)
        return list(
            await asyncio.gather(
                *(
                    self.grade(photo, title, identity=identity)
                    for (photo, title), identity in zip(photos, ids, strict=True)
                )
            )
        )


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
