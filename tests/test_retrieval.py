"""Retrieval: chunking, BM25, fusion and the abstention gate.

Everything here runs offline. The dense side uses `HashEmbedder`, which has real
cosine geometry and no semantics, so it exercises fusion and ranking mechanics
without a model. Retrieval *quality* is measured separately, against a real
embedder, in `test_retrieval_live.py`.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from faultline.retrieval.bm25 import BM25Index, tokenize
from faultline.retrieval.chunking import (
    CHILD_MAX_TOKENS,
    chunk_document,
    parse_document,
    split_sections,
)
from faultline.retrieval.dense import DenseIndex, HashEmbedder
from faultline.retrieval.hybrid import HybridRetriever
from faultline.retrieval.ingest import load_corpus

CORPUS = Path("corpus")


# -- tokenization ---------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("OOMKilled", {"oomkilled", "oom", "killed"}),
        ("http_error_rate", {"http_error_rate", "http", "error", "rate"}),
        ("checkout-service", {"checkout-service", "checkout", "service"}),
        ("PodRestartingFrequently", {"pod", "restarting", "frequently"}),
        ("HTTPSConnection", {"https", "connection"}),
    ],
)
def test_identifiers_are_indexed_whole_and_split(text: str, expected: set[str]) -> None:
    """The reason BM25 earns its place: a query can name the exact symbol or the
    words inside it, and both have to match."""
    assert expected <= set(tokenize(text))


def test_the_acronym_boundary_splits() -> None:
    """OOMKilled is the token this domain cares most about, and the obvious
    camel-case rule misses it because the boundary is capital-to-capital."""
    assert "oom" in tokenize("OOMKilled")
    assert "killed" in tokenize("OOMKilled")


def test_stopwords_are_dropped_but_identifiers_survive() -> None:
    tokens = tokenize("the pod is in CrashLoopBackOff")
    assert "the" not in tokens
    assert "crashloopbackoff" in tokens


# -- chunking -------------------------------------------------------------


def test_a_procedure_is_never_split() -> None:
    """Half a rollback procedure leaves a deployment at an unknown revision."""
    document = parse_document(CORPUS / "runbooks/checkout-service-high-error-rate.md", CORPUS)
    rollback = next(s for s in split_sections(document) if "Rollback" in s.heading)
    chunks = [c for c in chunk_document(document) if c.section.heading == rollback.heading]

    assert len(chunks) == 1
    for step in range(1, 6):
        assert f"{step}." in chunks[0].text


def test_children_carry_deterministic_context() -> None:
    """Costs nothing, needs no model, and makes a chunk findable by the service
    it belongs to even when its own prose never names it."""
    document = parse_document(CORPUS / "runbooks/oomkilled-pods.md", CORPUS)
    chunk = chunk_document(document)[0]
    assert "Pods restarting" in chunk.indexed_text
    assert "runbook" in chunk.indexed_text


def test_sections_carry_a_resolvable_citation() -> None:
    document = parse_document(CORPUS / "runbooks/frontend-5xx.md", CORPUS)
    section = split_sections(document)[0]
    assert section.citation.startswith("runbooks/frontend-5xx.md#")


def test_frontmatter_becomes_metadata() -> None:
    document = parse_document(CORPUS / "runbooks/payment-provider-degradation.md", CORPUS)
    assert document.service == "payment-service"
    assert document.doc_type == "runbook"
    assert document.last_verified


def test_chunk_ids_are_content_addressed() -> None:
    """Re-ingesting an unchanged document is a no-op, which is what makes
    indexing on every push affordable."""
    document = parse_document(CORPUS / "runbooks/alert-flapping.md", CORPUS)
    assert [c.id for c in chunk_document(document)] == [c.id for c in chunk_document(document)]


def test_every_child_is_within_the_size_budget_unless_it_is_a_procedure() -> None:
    for chunk in load_corpus(CORPUS).chunks:
        if "1." in chunk.text and "2." in chunk.text:
            continue  # a procedure is allowed to exceed the target
        assert len(chunk.text) // 4 <= CHILD_MAX_TOKENS * 2, chunk.id


# -- bm25 -----------------------------------------------------------------


def test_bm25_ranks_the_document_that_names_the_term() -> None:
    index = BM25Index()
    index.add("oom", "pods are OOMKilled when the memory limit is too low")
    index.add("deploy", "roll back the deployment to the previous revision")
    assert index.search("OOMKilled")[0][0] == "oom"


def test_bm25_returns_nothing_for_absent_terms() -> None:
    """An empty result is what lets the gate tell 'no match' from 'weak match'."""
    index = BM25Index()
    index.add("a", "pods are OOMKilled")
    assert index.search("quarterly revenue forecast") == []


def test_readding_a_document_does_not_double_count_it() -> None:
    index = BM25Index()
    index.add("a", "OOMKilled pods")
    index.add("a", "OOMKilled pods")
    assert len(index) == 1


def test_an_empty_index_searches_safely() -> None:
    assert BM25Index().search("anything") == []


# -- dense ----------------------------------------------------------------


async def test_dense_index_ranks_by_cosine() -> None:
    embedder = HashEmbedder()
    index = DenseIndex()
    texts = {"oom": "pods OOMKilled memory limit", "deploy": "roll back the deployment"}
    for doc_id, vector in zip(texts, await embedder.embed(list(texts.values())), strict=True):
        index.add(doc_id, vector)

    query = (await embedder.embed(["pods OOMKilled memory limit"]))[0]
    assert index.search(query)[0][0] == "oom"


async def test_an_empty_dense_index_searches_safely() -> None:
    assert DenseIndex().search([0.0, 1.0]) == []


# -- fusion and the gate --------------------------------------------------


async def _retriever(**kwargs: object) -> HybridRetriever:
    retriever = HybridRetriever(embedder=HashEmbedder(), **kwargs)  # type: ignore[arg-type]
    await retriever.index(load_corpus(CORPUS).chunks)
    return retriever


async def test_an_on_topic_query_finds_its_runbook() -> None:
    retriever = await _retriever()
    result = await retriever.search("pods OOMKilled restarting", limit=3)
    assert not result.abstained
    assert any("oomkilled" in hit.citation for hit in result.hits)


async def test_the_gate_refuses_an_off_topic_query() -> None:
    """The bug this test exists for: RRF confidence measures whether the
    retrievers agree, not whether anything is relevant, so an off-topic query
    that both rank identically used to score 0.88 and return a payment runbook."""
    retriever = await _retriever()
    result = await retriever.search("best hiking trails in patagonia", limit=3)
    assert result.abstained
    assert result.hits == []
    assert "floor" in result.reason


async def test_an_empty_corpus_abstains_rather_than_erroring() -> None:
    result = await HybridRetriever(embedder=HashEmbedder()).search("anything")
    assert result.abstained
    assert "empty" in result.reason


async def test_children_are_collapsed_to_their_parent_section() -> None:
    """Two chunks of one section are one answer, and the model should read the
    section rather than the pieces."""
    retriever = await _retriever()
    result = await retriever.search("rollback procedure checkout-service", limit=5)
    citations = [hit.citation for hit in result.hits]
    assert len(citations) == len(set(citations))


async def test_lexical_only_still_works_without_an_embedder() -> None:
    """The documented degraded mode: if embeddings are unavailable, retrieval
    falls back to BM25 rather than failing."""
    retriever = HybridRetriever(embedder=None)
    await retriever.index(load_corpus(CORPUS).chunks)
    result = await retriever.search("OOMKilled pods restarting", limit=3)

    assert not retriever.has_dense
    assert not result.abstained
    assert all(hit.dense_rank is None for hit in result.hits)


async def test_agreement_between_retrievers_is_visible() -> None:
    retriever = await _retriever()
    result = await retriever.search("OOMKilled memory limit lowered", limit=3)
    assert any(hit.found_by_both for hit in result.hits)


# -- ingestion ------------------------------------------------------------


def test_the_corpus_loads_and_is_pinned_to_a_commit() -> None:
    ingestion = load_corpus(CORPUS)
    assert ingestion.documents
    assert ingestion.chunks
    assert ingestion.commit  # "unknown" outside a git checkout, never empty


def test_a_missing_corpus_is_an_explicit_error() -> None:
    with pytest.raises(FileNotFoundError):
        load_corpus(Path("no/such/corpus"))


def test_the_corpus_covers_every_capsule_fault_family() -> None:
    """A corpus that does not answer the benchmark's questions cannot help."""
    paths = {d.path for d in load_corpus(CORPUS).documents}
    for expected in (
        "runbooks/checkout-service-high-error-rate.md",
        "runbooks/oomkilled-pods.md",
        "runbooks/payment-provider-degradation.md",
        "runbooks/alert-flapping.md",
    ):
        assert expected in paths
