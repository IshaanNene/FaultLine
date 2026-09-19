"""Hybrid retrieval: BM25 + dense, fused, with an abstention gate.

Reciprocal rank fusion rather than score blending, because BM25 scores and cosine
similarities are not on the same scale and normalising them is a tuning exercise
that never quite finishes. RRF only needs the ranks, which is why it survives a
corpus change without re-tuning.

The abstention gate matters as much as the ranking. A retriever that always
returns its best guess will hand the model a runbook for the wrong failure, and
a confidently irrelevant runbook is worse than none -- it invites the model to
reason from it. Saying "no matching runbook" and logging a knowledge gap is the
correct output, and a weekly gap report to runbook owners is a genuinely useful
product feature rather than an apology.
"""

from __future__ import annotations

from dataclasses import dataclass

from faultline.logging import get_logger
from faultline.retrieval.bm25 import BM25Index
from faultline.retrieval.chunking import Chunk, Section
from faultline.retrieval.dense import DenseIndex, Embedder

log = get_logger(__name__)

# RRF's smoothing constant. 60 is the value from the original paper and the one
# every comparable system uses; it makes the contribution of ranks beyond the
# first page decay gently rather than falling off a cliff.
RRF_K = 60

# Per-retriever candidate depth before fusion.
CANDIDATES = 50

# Relevance floors, per retriever, measured on the labelled query set rather than
# guessed. On-topic queries score BM25 6.2-14.8 and cosine 0.68-0.79; off-topic
# ones score BM25 0.0-2.6 and cosine 0.43-0.55. These floors sit in the gap.
#
# They are OR'd, not AND'd: a query full of exact identifiers can have a huge
# BM25 score and mediocre cosine, and a paraphrased one the reverse. Requiring
# both would abstain on half the queries the system exists to answer.
#
# The cosine floor is specific to the embedding model -- a different model has a
# different similarity range -- so it moves when the embedder does.
BM25_FLOOR = 4.0
COSINE_FLOOR = 0.62


@dataclass(slots=True)
class Hit:
    section: Section
    score: float
    lexical_rank: int | None
    dense_rank: int | None

    @property
    def citation(self) -> str:
        return self.section.citation

    @property
    def found_by_both(self) -> bool:
        """Agreement between two independent methods is the strongest signal RRF has."""
        return self.lexical_rank is not None and self.dense_rank is not None


@dataclass(slots=True)
class RetrievalResult:
    query: str
    hits: list[Hit]
    confidence: float
    abstained: bool
    reason: str = ""

    @property
    def sections(self) -> list[Section]:
        return [h.section for h in self.hits]


class HybridRetriever:
    """BM25 + dense over parent-child chunks."""

    def __init__(
        self,
        embedder: Embedder | None = None,
        rrf_k: int = RRF_K,
        bm25_floor: float = BM25_FLOOR,
        cosine_floor: float = COSINE_FLOOR,
        lexical: bool = True,
    ) -> None:
        # `lexical=False` exists for the ablation: it is the only way to ask what
        # dense retrieval contributes on its own, which is the question the
        # blueprint wants answered before anyone pays for an embedding model.
        self._lexical_enabled = lexical
        self._bm25 = BM25Index()
        self._dense = DenseIndex()
        self._embedder = embedder
        self._rrf_k = rrf_k
        self._bm25_floor = bm25_floor
        self._cosine_floor = cosine_floor
        self._chunks: dict[str, Chunk] = {}

    def __len__(self) -> int:
        return len(self._chunks)

    @property
    def has_dense(self) -> bool:
        return self._embedder is not None and len(self._dense) > 0

    async def index(self, chunks: list[Chunk]) -> None:
        """Index children lexically and densely. Parents are returned, never indexed."""
        for chunk in chunks:
            self._chunks[chunk.id] = chunk
            self._bm25.add(chunk.id, chunk.indexed_text)

        if self._embedder is not None:
            vectors = await self._embedder.embed([c.indexed_text for c in chunks], kind="document")
            for chunk, vector in zip(chunks, vectors, strict=True):
                self._dense.add(chunk.id, vector)

    async def search(self, query: str, limit: int = 5) -> RetrievalResult:
        if not self._chunks:
            return RetrievalResult(query, [], 0.0, True, "corpus is empty")

        lexical = self._bm25.search(query, limit=CANDIDATES) if self._lexical_enabled else []
        dense: list[tuple[str, float]] = []
        if self._embedder is not None and len(self._dense):
            vector = (await self._embedder.embed([query], kind="query"))[0]
            dense = self._dense.search(vector, limit=CANDIDATES)

        ranks: dict[str, tuple[int | None, int | None]] = {}
        for rank, (chunk_id, _score) in enumerate(lexical, start=1):
            ranks[chunk_id] = (rank, None)
        for rank, (chunk_id, _score) in enumerate(dense, start=1):
            lexical_rank = ranks.get(chunk_id, (None, None))[0]
            ranks[chunk_id] = (lexical_rank, rank)

        fused = [
            (chunk_id, self._rrf(lexical_rank, dense_rank), lexical_rank, dense_rank)
            for chunk_id, (lexical_rank, dense_rank) in ranks.items()
        ]
        fused.sort(key=lambda row: (-row[1], row[0]))

        # Collapse children to their parent section: two chunks of one section are
        # one answer, and the model should read the section rather than the pieces.
        seen: set[str] = set()
        hits: list[Hit] = []
        for chunk_id, score, lexical_rank, dense_rank in fused:
            section = self._chunks[chunk_id].section
            if section.citation in seen:
                continue
            seen.add(section.citation)
            hits.append(Hit(section, score, lexical_rank, dense_rank))
            if len(hits) >= limit:
                break

        # The gate reads raw relevance, not fused ranks.
        #
        # RRF confidence measures whether the retrievers *agree*, which is not the
        # same question as whether anything is relevant. Dense search always
        # returns a ranked list, so something is always first, and an off-topic
        # query that both retrievers rank identically scores near the maximum. The
        # first version of this gate happily returned a payment runbook for "how
        # do I bake bread" at 0.88 confidence.
        top_lexical = lexical[0][1] if lexical else 0.0
        top_cosine = dense[0][1] if dense else 0.0
        relevant = top_lexical >= self._bm25_floor or top_cosine >= self._cosine_floor

        confidence = hits[0].score / self._max_score() if hits else 0.0
        if not hits or not relevant:
            return RetrievalResult(
                query,
                [],
                confidence,
                True,
                f"nothing relevant: best BM25 {top_lexical:.1f} "
                f"(floor {self._bm25_floor:.1f}), best cosine {top_cosine:.2f} "
                f"(floor {self._cosine_floor:.2f})",
            )
        return RetrievalResult(query, hits, confidence, False)

    def _rrf(self, lexical_rank: int | None, dense_rank: int | None) -> float:
        score = 0.0
        if lexical_rank is not None:
            score += 1 / (self._rrf_k + lexical_rank)
        if dense_rank is not None:
            score += 1 / (self._rrf_k + dense_rank)
        return score

    def _max_score(self) -> float:
        """What a result ranked first by every active retriever would score.

        Normalising against this turns an uninterpretable RRF number into a 0-1
        confidence that means the same thing across queries, which is what makes a
        single abstention threshold possible at all.
        """
        retrievers = int(self._lexical_enabled) + int(self.has_dense)
        return max(retrievers, 1) / (self._rrf_k + 1)
