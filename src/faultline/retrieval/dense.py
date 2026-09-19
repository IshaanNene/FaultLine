"""Dense retrieval: embeddings and cosine similarity.

The complement to BM25, not a replacement for it. Lexical search finds the
runbook that names `OOMKilled`; dense search finds the one about pods dying from
memory pressure when the alert never used that word. Each fails where the other
works, which is the entire argument for fusing them.

Embeddings come from a local model by default, for the same reasons as the local
chat tier: no key, no per-token cost, and nothing leaves the machine.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Protocol

import httpx

from faultline.ids import content_hash
from faultline.logging import get_logger

log = get_logger(__name__)

DEFAULT_EMBED_MODEL = "nomic-embed-text"
DEFAULT_HOST = "http://localhost:11434"
EMBED_TIMEOUT_S = 120.0


class Embedder(Protocol):
    """Anything that turns text into a vector. One call per batch, not per chunk.

    `kind` exists because asymmetric embedding models encode a stored passage and
    a search query differently, and handing them the same string is a silent
    quality loss rather than an error.
    """

    name: str

    async def embed(self, texts: list[str], kind: str = "document") -> list[list[float]]: ...


class OllamaEmbedder:
    """Local embeddings via Ollama.

    Caches by content hash, so re-ingesting an unchanged corpus costs nothing and
    a query repeated inside one investigation is embedded once. That cache is the
    reason ingestion is cheap enough to run on every push.
    """

    def __init__(
        self,
        model: str = DEFAULT_EMBED_MODEL,
        host: str = DEFAULT_HOST,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.name = f"ollama:{model}"
        self.model = model
        self._host = host.rstrip("/")
        self._client = client
        self._cache: dict[str, list[float]] = {}

    async def _post(self, payload: dict[str, object]) -> httpx.Response:
        """One request, on whatever loop is currently running.

        A cached AsyncClient is bound to the loop that created it, and this
        embedder is called from two places on different loops: the corpus is
        indexed on the main loop at start-up, and a query is embedded inside the
        gateway's synchronous tool surface, which runs in a worker thread with a
        fresh loop per call. Holding a client across those raised "bound to a
        different event loop" and took the whole knowledge tool down.

        The connection-reuse this gives up is worth nothing here -- an embedding
        call is dominated by inference, and repeated text never reaches the
        network at all because of the cache below.
        """
        if self._client is not None:
            return await self._client.post(f"{self._host}/api/embed", json=payload)
        async with httpx.AsyncClient(timeout=EMBED_TIMEOUT_S) as client:
            return await client.post(f"{self._host}/api/embed", json=payload)

    def _prefixed(self, text: str, kind: str) -> str:
        """nomic-embed-text is asymmetric and expects a task prefix.

        Without it, stored passages and queries land in slightly different regions
        of the space and every similarity is a little wrong -- which shows up as
        mediocre ranking rather than as a failure, so it is easy to ship.
        """
        if not self.model.startswith("nomic-embed"):
            return text
        return f"search_query: {text}" if kind == "query" else f"search_document: {text}"

    async def embed(self, texts: list[str], kind: str = "document") -> list[list[float]]:
        prepared = [self._prefixed(t, kind) for t in texts]
        missing = [t for t in prepared if content_hash(self.model, t) not in self._cache]
        if missing:
            response = await self._post({"model": self.model, "input": missing})
            response.raise_for_status()
            vectors = response.json()["embeddings"]
            for text, vector in zip(missing, vectors, strict=True):
                self._cache[content_hash(self.model, text)] = vector
        return [self._cache[content_hash(self.model, t)] for t in prepared]


class HashEmbedder:
    """A deterministic offline embedder.

    Not a pretend language model: it hashes tokens into a fixed number of buckets,
    which gives a real vector space with real cosine geometry and no network. It
    exists so the fusion, ranking and abstention logic can be tested for free and
    in CI. It is a poor *retriever* -- it has no semantics at all -- and the
    benchmark reports it separately rather than letting it stand in for one.
    """

    name = "hash"

    def __init__(self, dimensions: int = 256) -> None:
        self.dimensions = dimensions

    async def embed(self, texts: list[str], kind: str = "document") -> list[list[float]]:
        del kind  # symmetric by construction; there is no query/passage asymmetry to honour
        from faultline.retrieval.bm25 import tokenize

        vectors: list[list[float]] = []
        for text in texts:
            vector = [0.0] * self.dimensions
            for token in tokenize(text):
                bucket = int(content_hash(token)[:8], 16) % self.dimensions
                vector[bucket] += 1.0
            vectors.append(_normalize(vector))
        return vectors


@dataclass(slots=True)
class DenseIndex:
    """Cosine similarity over stored vectors.

    In production this is a pgvector HNSW index on halfvec (see ADR 2). At corpus
    scale here an exact scan is both faster and simpler, and it removes any
    question of whether an approximate index is hiding a recall problem.
    """

    vectors: dict[str, list[float]]

    def __init__(self) -> None:
        self.vectors = {}

    def add(self, doc_id: str, vector: list[float]) -> None:
        self.vectors[doc_id] = _normalize(vector)

    def __len__(self) -> int:
        return len(self.vectors)

    def search(self, query_vector: list[float], limit: int = 50) -> list[tuple[str, float]]:
        if not self.vectors:
            return []
        query = _normalize(query_vector)
        scored = [
            (doc_id, sum(a * b for a, b in zip(query, vector, strict=True)))
            for doc_id, vector in self.vectors.items()
        ]
        scored.sort(key=lambda pair: (-pair[1], pair[0]))
        return scored[:limit]


def _normalize(vector: list[float]) -> list[float]:
    magnitude = math.sqrt(sum(v * v for v in vector))
    return vector if magnitude == 0 else [v / magnitude for v in vector]


async def available(host: str = DEFAULT_HOST, model: str = DEFAULT_EMBED_MODEL) -> bool:
    """Whether a local embedding model is actually pulled and serving."""
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            response = await client.get(f"{host.rstrip('/')}/api/tags")
            response.raise_for_status()
            names = [m["name"] for m in response.json().get("models", [])]
    except (httpx.HTTPError, KeyError, ValueError):
        return False
    return any(n.split(":")[0] == model.split(":")[0] for n in names)
