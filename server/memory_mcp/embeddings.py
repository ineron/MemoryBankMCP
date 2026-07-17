"""Embedding provider abstraction.

EMBED_PROVIDER=voyage (default) uses Voyage AI; EMBED_PROVIDER=openai uses
OpenAI's embeddings endpoint. Both return plain lists of floats sized to
match schema.sql's `vector(1024)` column — if you switch providers to one
with a different dimension, update the schema column width first (see the
comment at the top of schema.sql).

EMBED_PROVIDER=mock is also available: a deterministic, hash-based vector
with no network call and no API key. It has none of the semantic-similarity
quality real embeddings provide — it exists only so the server, schema, and
retrieval plumbing can be exercised offline/in tests. Never use it against
data you actually want to search over meaningfully.
"""

from __future__ import annotations

import hashlib
import os
import struct
from typing import Sequence

EXPECTED_DIM = 1024

# Conservative char-based proxy for OpenAI's 8192-token embedding limit (the
# tightest of the supported providers). ~4 chars/token is typical for English
# prose, so 20000 chars stays safely under 8192 tokens even for denser text
# (code blocks, non-English). This truncates only the text SENT for
# embedding — callers still store the full, untruncated text as the node's
# body, so nothing is lost from what's searchable-by-title/gettable; only
# very long documents lose embedding coverage of their tail.
MAX_EMBED_CHARS = 20000


def _provider() -> str:
    return os.environ.get("EMBED_PROVIDER", "voyage").lower()


def _truncate(text: str) -> str:
    return text[:MAX_EMBED_CHARS] if len(text) > MAX_EMBED_CHARS else text


async def embed(texts: Sequence[str]) -> list[list[float]]:
    """Embed a batch of texts, returning one vector per input in order."""
    if not texts:
        return []

    truncated = [_truncate(t) for t in texts]
    provider = _provider()
    if provider == "voyage":
        vectors = await _embed_voyage(truncated)
    elif provider == "openai":
        vectors = await _embed_openai(truncated)
    elif provider == "mock":
        vectors = [_embed_mock(t) for t in truncated]
    else:
        raise ValueError(f"Unknown EMBED_PROVIDER '{provider}' (expected 'voyage', 'openai', or 'mock')")

    for v in vectors:
        if len(v) != EXPECTED_DIM:
            raise ValueError(
                f"Embedding provider '{provider}' returned dim {len(v)}, "
                f"but schema.sql expects vector({EXPECTED_DIM}). Update the "
                "column width (and re-embed existing rows) if you intend to "
                "switch models/providers."
            )
    return vectors


async def embed_one(text: str) -> list[float]:
    (vec,) = await embed([text])
    return vec


def _embed_mock(text: str) -> list[float]:
    """Deterministic pseudo-embedding: repeatedly hash the text to fill
    EXPECTED_DIM floats in [-1, 1]. Same input -> same vector, different
    inputs -> effectively-random (not semantically meaningful) vectors."""
    values: list[float] = []
    counter = 0
    while len(values) < EXPECTED_DIM:
        digest = hashlib.sha256(f"{text}:{counter}".encode("utf-8")).digest()
        for i in range(0, len(digest) - 3, 4):
            if len(values) >= EXPECTED_DIM:
                break
            (as_uint,) = struct.unpack(">I", digest[i : i + 4])
            values.append((as_uint / 0xFFFFFFFF) * 2 - 1)
        counter += 1
    return values


async def _embed_voyage(texts: Sequence[str]) -> list[list[float]]:
    import voyageai

    api_key = os.environ.get("VOYAGE_API_KEY")
    if not api_key:
        raise RuntimeError("VOYAGE_API_KEY is not set (required for EMBED_PROVIDER=voyage)")
    model = os.environ.get("VOYAGE_MODEL", "voyage-3.5")

    client = voyageai.AsyncClient(api_key=api_key)
    result = await client.embed(list(texts), model=model, input_type="document")
    return result.embeddings


async def _embed_openai(texts: Sequence[str]) -> list[list[float]]:
    from openai import AsyncOpenAI

    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is not set (required for EMBED_PROVIDER=openai)")
    model = os.environ.get("OPENAI_EMBED_MODEL", "text-embedding-3-small")

    client = AsyncOpenAI(api_key=api_key)
    # text-embedding-3-* support Matryoshka truncation via `dimensions`, so we
    # ask for EXPECTED_DIM directly rather than requiring a schema change to
    # accommodate the model's native 1536-dim output.
    result = await client.embeddings.create(input=list(texts), model=model, dimensions=EXPECTED_DIM)
    return [d.embedding for d in result.data]
