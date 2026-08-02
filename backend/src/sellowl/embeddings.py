"""Text embeddings for the self-hosted search backend.

Elastic's `semantic_text` does the embedding for you as part of indexing --
that convenience is the single most Elastic-specific thing in the codebase
(see docs/MIGRATION.md), so replacing it means owning the vectors.

Two implementations behind one protocol:

- `OpenAIEmbedder` calls any OpenAI-compatible `/v1/embeddings` endpoint.
  This is the real one; point it at whatever serves embeddings.
- `HashingEmbedder` needs no service, no model download, and no new
  dependency. It is the default precisely so the migration is testable and
  the app is runnable with nothing running alongside it.

`HashingEmbedder` is deliberately *not* pretending to be a semantic model:
hashed character n-grams capture morphological overlap ("radiator" vs
"radiators", "120mm" vs "120 mm"), which is a genuine improvement on
whole-token matching and nothing like real semantics. It exists so the
retrieval path is exercised end to end without a GPU; swapping in a real
endpoint is a config change. Keeping that honest matters, because a fake
semantic layer that silently scores badly is worse than an obviously
lexical one.
"""

from __future__ import annotations

import hashlib
import math
import re
from typing import Protocol

from openai import AsyncOpenAI

from .logging import get_logger

log = get_logger(__name__)

EMBED_DIM = 256
EMBED_BATCH = 64
_WORD_RE = re.compile(r"[a-z0-9]+")


# bge/e5-family retrieval models are *asymmetric*: the query side wants an
# instruction prefix and the document side does not. Embedding both the same
# way is a silent quality loss -- everything still returns results, they're
# just worse -- so the protocol distinguishes the two rather than leaving it
# to each caller to remember. Empty for symmetric models.
QUERY_PREFIX = "Represent this sentence for searching relevant passages: "


class Embedder(Protocol):
    async def embed(self, texts: list[str]) -> list[list[float]]: ...

    async def embed_query(self, text: str) -> list[float]: ...

    @property
    def dim(self) -> int: ...

    @property
    def relevance_floor(self) -> float:
        """Cosine below which a comp is not the same kind of thing at all.

        Lives on the embedder because the scale is model-specific: bge's
        unrelated pairs sit around 0.55 and related ones above 0.65, while
        hashed n-grams occupy a completely different range. A single global
        constant would be right for at most one embedder.
        """
        ...


def _normalize(vector: list[float]) -> list[float]:
    norm = math.sqrt(sum(v * v for v in vector))
    if norm == 0.0:
        return vector
    return [v / norm for v in vector]


def cosine(a: list[float], b: list[float]) -> float:
    """Both sides are stored L2-normalized, so this is just a dot product."""
    return sum(x * y for x, y in zip(a, b, strict=False))


class HashingEmbedder:
    """Hashed character-n-gram vectors. No model, no service, no dependency.

    Sublinear term weighting (1 + log tf) rather than raw counts, so a title
    that repeats a word doesn't dominate, and L2-normalized so cosine is a
    dot product.
    """

    def __init__(self, dim: int = EMBED_DIM, ngram: int = 4) -> None:
        self._dim = dim
        self._ngram = ngram

    @property
    def dim(self) -> int:
        return self._dim

    @property
    def relevance_floor(self) -> float:
        # Lexical overlap, so unrelated text scores near zero and the floor
        # can be low without letting nonsense through.
        return 0.10

    def _features(self, text: str) -> list[str]:
        words = _WORD_RE.findall(text.lower())
        features = list(words)
        for word in words:
            padded = f" {word} "
            if len(padded) <= self._ngram:
                features.append(padded)
                continue
            features += [padded[i : i + self._ngram] for i in range(len(padded) - self._ngram + 1)]
        return features

    def _embed_one(self, text: str) -> list[float]:
        counts: dict[int, float] = {}
        for feature in self._features(text):
            digest = hashlib.blake2b(feature.encode(), digest_size=8).digest()
            bucket = int.from_bytes(digest, "big") % self._dim
            counts[bucket] = counts.get(bucket, 0.0) + 1.0
        vector = [0.0] * self._dim
        for bucket, count in counts.items():
            vector[bucket] = 1.0 + math.log(count)
        return _normalize(vector)

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._embed_one(t) for t in texts]

    async def embed_query(self, text: str) -> list[float]:
        # Symmetric by construction -- no instruction prefix to add.
        return self._embed_one(text)


class OpenAIEmbedder:
    """Any OpenAI-compatible /v1/embeddings endpoint."""

    def __init__(
        self,
        client: AsyncOpenAI,
        model: str,
        dim: int = EMBED_DIM,
        query_prefix: str = QUERY_PREFIX,
        floor: float = 0.62,
    ) -> None:
        self._client = client
        self._model = model
        self._dim = dim
        self._query_prefix = query_prefix
        self._floor = floor
        self._fallback = HashingEmbedder(dim)
        self._warned = False

    @property
    def dim(self) -> int:
        return self._dim

    @property
    def relevance_floor(self) -> float:
        return self._floor

    async def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        # Chunked: a backfill embeds thousands of comps at once, and one
        # request that large is rejected or times out on most servers.
        if len(texts) > EMBED_BATCH:
            out: list[list[float]] = []
            for i in range(0, len(texts), EMBED_BATCH):
                out += await self.embed(texts[i : i + EMBED_BATCH])
            return out
        try:
            response = await self._client.embeddings.create(model=self._model, input=texts)
        except Exception as exc:  # noqa: BLE001 - retrieval must degrade, not die
            # Once, not once per batch: an unreachable endpoint would other-
            # wise bury the log, and the message is identical every time.
            if not self._warned:
                self._warned = True
                log.warning(
                    "embedding_endpoint_unavailable_falling_back_to_lexical",
                    model=self._model,
                    error=str(exc),
                )
            return await self._fallback.embed(texts)
        vectors = [_normalize(list(item.embedding)) for item in response.data]
        if vectors and len(vectors[0]) != self._dim:
            self._dim = len(vectors[0])
        return vectors

    async def embed_query(self, text: str) -> list[float]:
        [vector] = await self.embed([f"{self._query_prefix}{text}"])
        return vector
