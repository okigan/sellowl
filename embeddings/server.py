"""An OpenAI-compatible /v1/embeddings endpoint, backed by fastembed.

Exists because the app's search backend needs vectors and nothing else here
serves them: Elastic's `semantic_text` embeds server-side (the thing the
migration is removing), and the local llama.cpp proxy exposes only
chat/completions, messages and images -- no embeddings route at all, so
loading an embedding model there would leave no way to call it.

fastembed runs the model through ONNX Runtime on CPU. At this app's scale
(a few hundred short titles per job) that is fast enough that a GPU would be
idle, and it keeps torch out of the image entirely.

Default model: BAAI/bge-small-en-v1.5 -- 384-dim, ~133MB, near the top of
MTEB retrieval for its size class. Note it is an *asymmetric* model: the
query side wants an instruction prefix and the document side does not. That
prefix is applied by the caller (see backend embeddings.py QUERY_PREFIX), not
here, because this endpoint cannot tell a query from a document.
"""

from __future__ import annotations

import os
from typing import Any

from fastapi import FastAPI
from fastembed import TextEmbedding
from pydantic import BaseModel

MODEL_NAME = os.environ.get("EMBEDDING_MODEL", "BAAI/bge-small-en-v1.5")

app = FastAPI(title="sellowl-embeddings")
_model: TextEmbedding | None = None


def model() -> TextEmbedding:
    global _model
    if _model is None:
        _model = TextEmbedding(model_name=MODEL_NAME)
    return _model


class EmbeddingRequest(BaseModel):
    input: str | list[str]
    model: str | None = None


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "model": MODEL_NAME}


@app.post("/v1/embeddings")
def embeddings(request: EmbeddingRequest) -> dict[str, Any]:
    texts = [request.input] if isinstance(request.input, str) else request.input
    vectors = [v.tolist() for v in model().embed(texts)]
    return {
        "object": "list",
        "model": MODEL_NAME,
        "data": [
            {"object": "embedding", "index": i, "embedding": v} for i, v in enumerate(vectors)
        ],
        "usage": {"prompt_tokens": 0, "total_tokens": 0},
    }
