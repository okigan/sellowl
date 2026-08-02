"""The self-hosted search backend (docs/MIGRATION.md step 1).

Same CompStore protocol as ElasticCompStore, so these tests are really
asking: could this replace the cluster without the pipeline noticing?
"""

from __future__ import annotations

from pathlib import Path

import pytest

from sellowl.embeddings import HashingEmbedder, cosine
from sellowl.models import Comp, Condition, Venue
from sellowl.sqlite_store import SqliteCompStore, _fts_query


def comp(ext: str, title: str, *, price: float = 20.0, venue: Venue = Venue.EBAY_SOLD) -> Comp:
    return Comp(
        external_id=ext,
        venue=venue,
        title=title,
        price=price,
        condition=Condition.USABLE,
        description=title,
    )


@pytest.fixture
async def store(tmp_path: Path):
    s = SqliteCompStore(tmp_path / "comps.db")
    await s.ensure_indices()
    yield s
    await s.close()


class TestRoundTrip:
    async def test_upsert_then_find(self, store: SqliteCompStore) -> None:
        await store.upsert_comps([comp("1", "Apricorn Aegis Secure Key 4GB USB drive")])
        found = await store.find_comps(
            bm25_query="Apricorn Aegis Secure Key",
            semantic_query="encrypted usb flash drive",
            venue=Venue.EBAY_SOLD,
            size=8,
            job_id="j",
        )
        assert [c.external_id for c in found] == ["1"]
        assert found[0].price == 20.0
        assert found[0].condition is Condition.USABLE

    async def test_upsert_is_idempotent(self, store: SqliteCompStore) -> None:
        """Re-running a job re-indexes the same comps; they must not double."""
        for _ in range(3):
            await store.upsert_comps([comp("1", "Apricorn Aegis Secure Key 4GB")])
        found = await store.find_comps(
            bm25_query="Apricorn",
            semantic_query="Apricorn",
            venue=Venue.EBAY_SOLD,
            size=8,
            job_id="j",
        )
        assert len(found) == 1

    async def test_update_replaces_the_old_text(self, store: SqliteCompStore) -> None:
        await store.upsert_comps([comp("1", "teak sideboard")])
        await store.upsert_comps([comp("1", "walnut bookcase")])
        found = await store.find_comps(
            bm25_query="walnut bookcase",
            semantic_query="walnut bookcase",
            venue=Venue.EBAY_SOLD,
            size=8,
            job_id="j",
        )
        assert found[0].title == "walnut bookcase"

    async def test_venue_is_a_hard_filter(self, store: SqliteCompStore) -> None:
        await store.upsert_comps(
            [
                comp("sold", "Apricorn Aegis 4GB", venue=Venue.EBAY_SOLD),
                comp("local", "Apricorn Aegis 4GB", venue=Venue.FB_LOCAL),
            ]
        )
        found = await store.find_comps(
            bm25_query="Apricorn Aegis",
            semantic_query="Apricorn Aegis",
            venue=Venue.FB_LOCAL,
            size=8,
            job_id="j",
        )
        assert [c.external_id for c in found] == ["local"]

    async def test_empty_store_returns_nothing(self, store: SqliteCompStore) -> None:
        found = await store.find_comps(
            bm25_query="anything",
            semantic_query="anything",
            venue=Venue.EBAY_SOLD,
            size=8,
            job_id="j",
        )
        assert found == []

    async def test_size_caps_results(self, store: SqliteCompStore) -> None:
        await store.upsert_comps([comp(str(i), f"case fan {i}") for i in range(20)])
        found = await store.find_comps(
            bm25_query="case fan",
            semantic_query="case fan",
            venue=Venue.EBAY_SOLD,
            size=5,
            job_id="j",
        )
        assert len(found) == 5

    async def test_relevant_comp_outranks_an_unrelated_one(self, store: SqliteCompStore) -> None:
        await store.upsert_comps(
            [
                comp("drive", "Apricorn Aegis Secure Key 4GB encrypted USB drive"),
                comp("fan", "Thermaltake Riing 120mm RGB case fan"),
            ]
        )
        found = await store.find_comps(
            bm25_query="Apricorn Aegis Secure Key",
            semantic_query="encrypted usb flash drive with keypad",
            venue=Venue.EBAY_SOLD,
            size=8,
            job_id="j",
        )
        assert found[0].external_id == "drive"

    async def test_survives_a_restart(self, tmp_path: Path) -> None:
        """The point of SQLite over MemoryCompStore: it's still there."""
        first = SqliteCompStore(tmp_path / "comps.db")
        await first.ensure_indices()
        await first.upsert_comps([comp("1", "teak sideboard")])
        await first.close()

        second = SqliteCompStore(tmp_path / "comps.db")
        await second.ensure_indices()
        found = await second.find_comps(
            bm25_query="teak sideboard",
            semantic_query="teak sideboard",
            venue=Venue.EBAY_SOLD,
            size=8,
            job_id="j",
        )
        await second.close()
        assert [c.external_id for c in found] == ["1"]


class TestFtsQuerySafety:
    """FTS5 MATCH is a query language. Real titles are full of characters it
    treats as operators, and an unescaped one raises mid-query."""

    @pytest.mark.parametrize(
        "raw",
        [
            'Thermaltake V-Tubler 4T Water Cooling Tube 3/4"OD 1/2"ID PVC',
            "AT&T cable (OEM) *new*",
            "NOT a boolean OR an operator AND neither is this",
            "^caret -minus +plus : colon",
            "",
            "   ",
        ],
    )
    async def test_hostile_titles_do_not_raise(self, store: SqliteCompStore, raw: str) -> None:
        await store.upsert_comps([comp("1", raw or "placeholder")])
        found = await store.find_comps(
            bm25_query=raw,
            semantic_query=raw,
            venue=Venue.EBAY_SOLD,
            size=8,
            job_id="j",
        )
        assert isinstance(found, list)

    def test_quotes_every_term(self) -> None:
        assert _fts_query("a b") == '"a" OR "b"'

    def test_empty_query_is_empty(self) -> None:
        assert _fts_query("   ") == ""


class TestHashingEmbedder:
    async def test_vectors_are_normalized(self) -> None:
        [vector] = await HashingEmbedder().embed(["teak sideboard"])
        assert cosine(vector, vector) == pytest.approx(1.0)

    async def test_identical_text_is_identical(self) -> None:
        a, b = await HashingEmbedder().embed(["case fan", "case fan"])
        assert a == b

    async def test_related_text_scores_above_unrelated(self) -> None:
        embedder = HashingEmbedder()
        query, related, unrelated = await embedder.embed(
            ["120mm case fan", "case fan 120mm RGB", "teak dining sideboard"]
        )
        assert cosine(query, related) > cosine(query, unrelated)

    async def test_morphological_overlap_is_caught(self) -> None:
        """The thing whole-token matching misses, and the only reason a
        character-n-gram stand-in is worth having at all."""
        embedder = HashingEmbedder()
        query, plural, unrelated = await embedder.embed(
            ["radiator", "radiators", "usb flash drive"]
        )
        assert cosine(query, plural) > cosine(query, unrelated)

    async def test_empty_input(self) -> None:
        assert await HashingEmbedder().embed([]) == []
