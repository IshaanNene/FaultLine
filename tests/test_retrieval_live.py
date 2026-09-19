"""Retrieval quality against a real embedding model.

Opt-in like the other live tests, because quality is the one thing a hash-based
stand-in cannot measure: `HashEmbedder` has real geometry and no semantics, so it
proves the fusion mechanics and nothing about whether the right runbook comes
back.

Run with:  pytest -m ollama
"""

from __future__ import annotations

import pytest

from faultline.retrieval.dense import DEFAULT_EMBED_MODEL, OllamaEmbedder, available
from faultline.retrieval.evaluate import LABELLED, OFF_TOPIC, ablation, evaluate
from faultline.retrieval.hybrid import HybridRetriever
from faultline.retrieval.ingest import load_corpus

pytestmark = pytest.mark.ollama


async def _require() -> OllamaEmbedder:
    if not await available():
        pytest.skip(f"{DEFAULT_EMBED_MODEL} is not pulled or Ollama is not running")
    return OllamaEmbedder()


async def _hybrid() -> HybridRetriever:
    retriever = HybridRetriever(embedder=await _require())
    await retriever.index(load_corpus().chunks)
    return retriever


async def test_recall_clears_the_bar_on_the_labelled_set() -> None:
    score = await evaluate(await _hybrid(), "hybrid")
    assert score.recall_at_3 >= 0.85, score.misses
    assert score.mrr >= 0.75, score.misses


async def test_the_gate_lets_nothing_off_topic_through() -> None:
    """Each leak is a confidently irrelevant runbook handed to the model."""
    score = await evaluate(await _hybrid(), "hybrid")
    assert score.missed_abstentions == 0, score.misses


async def test_the_gate_does_not_refuse_on_topic_queries() -> None:
    """Each false abstention is a runbook a responder never saw."""
    score = await evaluate(await _hybrid(), "hybrid")
    assert score.false_abstentions == 0, score.misses


@pytest.mark.parametrize("query", OFF_TOPIC)
async def test_each_off_topic_query_abstains(query: str) -> None:
    assert (await (await _hybrid()).search(query, limit=1)).abstained


async def test_dense_retrieval_beats_lexical_alone_on_this_corpus() -> None:
    """Recorded because it is the opposite of what the design assumed.

    The corpus is prose-heavy and the labelled queries are paraphrases, which is
    dense retrieval's best case and BM25's worst. If a later corpus full of exact
    identifiers flips this, the README's claim has to flip with it.
    """
    scores = {s.configuration: s for s in await ablation(await _require())}
    assert scores["dense only"].mrr > scores["bm25 only"].mrr


async def test_the_labelled_set_is_not_trivially_small() -> None:
    assert len(LABELLED) >= 15
