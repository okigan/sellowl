"""Vision output parsing.

The model is prompted for bare JSON but will occasionally fence it or wrap it
in a sentence. A malformed grade must degrade one row, never fail a job.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from sellowl.config import Settings
from sellowl.models import Condition
from sellowl.vision import VisionGrader, _broad_from_title, _media_type, parse_vision_json

GOOD = """{
  "canonical_description": "mid-century teak sideboard, tapered legs, brass pulls",
  "attributes": {"material": "teak", "era": "1960s", "size_class": "large"},
  "condition": "usable",
  "condition_evidence": "visible veneer chip on left door, original hardware intact",
  "search_query_broad": "teak sideboard",
  "search_query_narrow": "danish modern teak credenza"
}"""


class TestParseVisionJson:
    def test_clean_json(self) -> None:
        result = parse_vision_json(GOOD)
        assert result.condition is Condition.USABLE
        assert result.attributes["material"] == "teak"
        assert result.search_query_broad == "teak sideboard"

    def test_fenced_json(self) -> None:
        assert parse_vision_json(f"```json\n{GOOD}\n```").condition is Condition.USABLE

    def test_json_with_surrounding_prose(self) -> None:
        wrapped = f"Here is the appraisal:\n{GOOD}\nHope that helps."
        assert parse_vision_json(wrapped).attributes["era"] == "1960s"

    def test_unknown_condition_word_degrades(self) -> None:
        result = parse_vision_json('{"condition": "pristine"}')
        assert result.condition is Condition.UNKNOWN

    def test_condition_is_case_insensitive(self) -> None:
        assert parse_vision_json('{"condition": "CLEAN"}').condition is Condition.CLEAN

    def test_condition_synonyms_are_normalized(self) -> None:
        """Not every vision model sticks to rough/usable/clean — e.g. a
        general-purpose model answering "new" rather than "clean"."""
        assert parse_vision_json('{"condition": "new"}').condition is Condition.CLEAN
        assert parse_vision_json('{"condition": "Brand New"}').condition is Condition.CLEAN
        assert parse_vision_json('{"condition": "good"}').condition is Condition.USABLE
        assert parse_vision_json('{"condition": "damaged"}').condition is Condition.ROUGH

    def test_garbage_returns_empty_not_raises(self) -> None:
        for text in ("", "not json at all", "{{{", "[]", "null"):
            result = parse_vision_json(text)
            assert result.condition is Condition.UNKNOWN
            assert result.canonical_description == ""

    def test_non_dict_attributes_ignored(self) -> None:
        assert parse_vision_json('{"attributes": "teak"}').attributes == {}

    def test_empty_attribute_values_dropped(self) -> None:
        result = parse_vision_json('{"attributes": {"material": "teak", "brand": ""}}')
        assert result.attributes == {"material": "teak"}


class TestBroadFromTitle:
    def test_strips_model_numbers(self) -> None:
        """No Facebook seller types 'Pyrex 444'."""
        assert "444" not in _broad_from_title("Pyrex 444 Spring Blossom bowl")

    def test_caps_length(self) -> None:
        assert len(_broad_from_title("one two three four five six").split()) == 4

    def test_all_numeric_title_falls_back(self) -> None:
        assert _broad_from_title("444 1960 2x4") != ""


class TestMediaType:
    def test_detects_jpeg(self) -> None:
        assert _media_type(b"\xff\xd8\xff\xe0rest") == "image/jpeg"

    def test_detects_png(self) -> None:
        assert _media_type(b"\x89PNG\r\n") == "image/png"

    def test_unknown_defaults_to_jpeg(self) -> None:
        assert _media_type(b"zzzz") == "image/jpeg"


class FakeOpenAI:
    """Counts calls so the cache test can assert the network path only runs once."""

    def __init__(self, reply: str) -> None:
        self.calls = 0
        self._reply = reply
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    async def _create(self, **_kwargs: Any) -> Any:
        self.calls += 1
        message = SimpleNamespace(content=self._reply)
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])


class TestVisionGraderCaching:
    """Comp photos repeat across re-analyzes of the same store; grading them
    again every run wastes a real network call per photo."""

    @pytest.fixture
    def grader(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> tuple[VisionGrader, FakeOpenAI]:
        monkeypatch.setattr("sellowl.vision.VISION_CACHE_DIR", tmp_path)
        settings = Settings(
            vision_provider="openai",
            vision_base_url="http://fake",
            vision_api_key="fake",
            vision_model="fake-model",
        )
        g = VisionGrader(settings)
        fake = FakeOpenAI('{"canonical_description": "a teak bowl", "condition": "clean"}')
        g._openai = fake  # type: ignore[assignment]
        return g, fake

    async def test_second_call_for_the_same_photo_hits_cache(
        self, grader: tuple[VisionGrader, FakeOpenAI]
    ) -> None:
        g, fake = grader
        photo = b"\xff\xd8\xff fake jpeg bytes"
        first = await g.grade(photo, "vintage bowl")
        second = await g.grade(photo, "vintage bowl")
        assert fake.calls == 1
        assert first.canonical_description == second.canonical_description == "a teak bowl"

    async def test_different_photo_is_a_separate_cache_entry(
        self, grader: tuple[VisionGrader, FakeOpenAI]
    ) -> None:
        g, fake = grader
        await g.grade(b"\xff\xd8\xff photo one", "vintage bowl")
        await g.grade(b"\xff\xd8\xff photo two", "vintage bowl")
        assert fake.calls == 2

    async def test_different_title_is_a_separate_cache_entry(
        self, grader: tuple[VisionGrader, FakeOpenAI]
    ) -> None:
        """Title is part of the prompt, so it must be part of the cache key."""
        g, fake = grader
        photo = b"\xff\xd8\xff fake jpeg bytes"
        await g.grade(photo, "vintage bowl")
        await g.grade(photo, "modern vase")
        assert fake.calls == 2

    async def test_prompt_change_invalidates_old_cache_entries(
        self, grader: tuple[VisionGrader, FakeOpenAI], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Editing PROMPT (e.g. adding an attribute) must not keep serving
        results shaped by the old prompt for the rest of the cache TTL."""
        g, fake = grader
        photo = b"\xff\xd8\xff fake jpeg bytes"
        await g.grade(photo, "vintage bowl")
        monkeypatch.setattr("sellowl.vision._PROMPT_VERSION", "a-different-version")
        await g.grade(photo, "vintage bowl")
        assert fake.calls == 2
