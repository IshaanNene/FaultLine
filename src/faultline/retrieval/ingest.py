"""Corpus ingestion.

Sources live in git, so every chunk carries the commit it came from and
re-ingestion is idempotent: chunk ids are content hashes, so an unchanged
document produces identical ids and an embedding cache never recomputes a vector
it already holds. That is what makes it cheap enough to re-ingest on every push.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from faultline.logging import get_logger
from faultline.retrieval.chunking import Chunk, Document, chunk_document, parse_document
from faultline.retrieval.dense import Embedder
from faultline.retrieval.hybrid import HybridRetriever

log = get_logger(__name__)

DEFAULT_CORPUS = Path("corpus")


@dataclass(slots=True)
class Ingestion:
    documents: list[Document]
    chunks: list[Chunk]
    commit: str

    @property
    def summary(self) -> str:
        return f"{len(self.documents)} documents, {len(self.chunks)} chunks at {self.commit[:8]}"


def load_corpus(root: Path = DEFAULT_CORPUS) -> Ingestion:
    if not root.exists():
        raise FileNotFoundError(f"corpus not found at {root}")
    documents = [parse_document(path, root) for path in sorted(root.rglob("*.md"))]
    chunks = [chunk for document in documents for chunk in chunk_document(document)]
    return Ingestion(documents=documents, chunks=chunks, commit=_commit(root))


async def build_retriever(
    root: Path = DEFAULT_CORPUS, embedder: Embedder | None = None
) -> HybridRetriever:
    ingestion = load_corpus(root)
    retriever = HybridRetriever(embedder=embedder)
    await retriever.index(ingestion.chunks)
    log.info(
        "corpus_indexed",
        documents=len(ingestion.documents),
        chunks=len(ingestion.chunks),
        commit=ingestion.commit[:8],
        dense=retriever.has_dense,
    )
    return retriever


def _commit(root: Path) -> str:
    """The commit the corpus was read at, so a citation is pinned to a version."""
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    return result.stdout.strip() or "unknown"
