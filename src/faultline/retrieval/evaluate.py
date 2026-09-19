"""Retrieval evaluation: labelled queries, Recall@K, MRR, and the stage ablation.

The blueprint asks for gains attributed to specific stages rather than a claim
that hybrid retrieval is better. That requires a labelled set and a run per
configuration, which is all this module is.

The labels are deliberately written as an on-call engineer would phrase the
question -- alert text, symptoms, half-remembered error strings -- rather than as
a search for a document that is known to exist.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from faultline.retrieval.dense import Embedder
from faultline.retrieval.hybrid import HybridRetriever
from faultline.retrieval.ingest import DEFAULT_CORPUS, load_corpus


@dataclass(slots=True)
class LabelledQuery:
    query: str
    relevant: set[str]
    """Document paths that answer this query. A section under any of them counts."""


# Written from the alerts and symptoms in the benchmark capsules, plus the
# phrasings a responder actually uses. Small and honest about being small.
LABELLED: list[LabelledQuery] = [
    LabelledQuery(
        "checkout-service HighErrorRate 5xx above 5 percent",
        {"runbooks/checkout-service-high-error-rate.md"},
    ),
    LabelledQuery(
        "failed to serialize order unknown field",
        {
            "runbooks/checkout-service-high-error-rate.md",
            "postmortems/2026-03-checkout-serializer.md",
        },
    ),
    LabelledQuery(
        "how do I roll back a deployment safely", {"runbooks/checkout-service-high-error-rate.md"}
    ),
    LabelledQuery(
        "errors started right after a release went out",
        {
            "runbooks/checkout-service-high-error-rate.md",
            "postmortems/2026-03-checkout-serializer.md",
        },
    ),
    LabelledQuery("PodRestartingFrequently OOMKilled", {"runbooks/oomkilled-pods.md"}),
    LabelledQuery(
        "container keeps dying memory limit too low",
        {"runbooks/oomkilled-pods.md", "postmortems/2026-05-cart-oom.md"},
    ),
    LabelledQuery(
        "cart-service restarting after a config change",
        {"postmortems/2026-05-cart-oom.md", "runbooks/oomkilled-pods.md"},
    ),
    LabelledQuery(
        "upstream authorisation timeout payment provider",
        {"runbooks/payment-provider-degradation.md"},
    ),
    LabelledQuery(
        "service is slow but nothing was deployed", {"runbooks/payment-provider-degradation.md"}
    ),
    LabelledQuery(
        "should I roll back payment-service", {"runbooks/payment-provider-degradation.md"}
    ),
    LabelledQuery("warning alert fired and resolved by itself", {"runbooks/alert-flapping.md"}),
    LabelledQuery("DiskUsageWarning resolved metrics in baseline", {"runbooks/alert-flapping.md"}),
    LabelledQuery("frontend 502 errors is frontend broken", {"runbooks/frontend-5xx.md"}),
    LabelledQuery(
        "which service should I blame when everything alerts",
        {"runbooks/frontend-5xx.md", "architecture/service-catalog.md"},
    ),
    LabelledQuery("what does checkout-service call", {"architecture/service-catalog.md"}),
]

OFF_TOPIC = [
    "how do I bake bread",
    "best hiking trails in patagonia",
    "quarterly revenue forecast spreadsheet",
    "kubernetes etcd raft consensus tuning",
]


@dataclass(slots=True)
class RetrievalScore:
    configuration: str
    recall_at_1: float = 0.0
    recall_at_3: float = 0.0
    recall_at_5: float = 0.0
    mrr: float = 0.0
    false_abstentions: int = 0
    """On-topic queries the gate wrongly refused. Each is a runbook a responder never saw."""
    missed_abstentions: int = 0
    """Off-topic queries answered anyway. Each is a confidently irrelevant runbook."""
    misses: list[str] = field(default_factory=list)

    def row(self) -> str:
        return (
            f"{self.configuration:14} R@1={self.recall_at_1:5.0%} R@3={self.recall_at_3:5.0%} "
            f"R@5={self.recall_at_5:5.0%} MRR={self.mrr:5.2f} "
            f"false-abstain={self.false_abstentions} leaked={self.missed_abstentions}"
        )


async def evaluate(
    retriever: HybridRetriever,
    configuration: str,
    queries: list[LabelledQuery] | None = None,
) -> RetrievalScore:
    queries = queries or LABELLED
    score = RetrievalScore(configuration=configuration)
    hits_at = {1: 0, 3: 0, 5: 0}
    reciprocal = 0.0

    for labelled in queries:
        result = await retriever.search(labelled.query, limit=5)
        if result.abstained:
            score.false_abstentions += 1
            score.misses.append(f"abstained: {labelled.query}")
            continue
        paths = [hit.section.document.path for hit in result.hits]
        for k in hits_at:
            if any(p in labelled.relevant for p in paths[:k]):
                hits_at[k] += 1
        rank = next((i for i, p in enumerate(paths, 1) if p in labelled.relevant), None)
        if rank:
            reciprocal += 1 / rank
        else:
            score.misses.append(f"missed: {labelled.query} -> got {paths[0] if paths else '-'}")

    for query in OFF_TOPIC:
        if not (await retriever.search(query, limit=1)).abstained:
            score.missed_abstentions += 1
            score.misses.append(f"leaked: {query}")

    n = len(queries)
    score.recall_at_1 = hits_at[1] / n
    score.recall_at_3 = hits_at[3] / n
    score.recall_at_5 = hits_at[5] / n
    score.mrr = reciprocal / n
    return score


async def ablation(embedder: Embedder | None, root: Path = DEFAULT_CORPUS) -> list[RetrievalScore]:
    """What does each stage actually contribute?

    Three configurations over one corpus and one query set. If hybrid does not
    beat both halves, the README should say so rather than assuming the result
    everyone expects.
    """
    chunks = load_corpus(root).chunks
    scores: list[RetrievalScore] = []

    lexical_only = HybridRetriever(embedder=None)
    await lexical_only.index(chunks)
    scores.append(await evaluate(lexical_only, "bm25 only"))

    if embedder is not None:
        dense_only = HybridRetriever(embedder=embedder, lexical=False)
        await dense_only.index(chunks)
        scores.append(await evaluate(dense_only, "dense only"))

        hybrid = HybridRetriever(embedder=embedder)
        await hybrid.index(chunks)
        scores.append(await evaluate(hybrid, "hybrid (rrf)"))

    return scores


def render(scores: list[RetrievalScore]) -> str:
    lines = [
        "configuration    R@1    R@3    R@5   MRR   gate",
        "-" * 70,
        *(s.row() for s in scores),
        "",
        "false-abstain = on-topic query the gate refused (a runbook nobody saw)",
        "leaked        = off-topic query answered anyway (a confidently irrelevant runbook)",
    ]
    return "\n".join(lines)
