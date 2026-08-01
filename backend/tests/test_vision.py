"""Vision output parsing.

The model is prompted for bare JSON but will occasionally fence it or wrap it
in a sentence. A malformed grade must degrade one row, never fail a job.
"""

from __future__ import annotations

from sellowl.models import Condition
from sellowl.vision import _broad_from_title, _media_type, parse_vision_json

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
