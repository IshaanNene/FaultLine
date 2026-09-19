# 10. Retrieval, measured before believed

Date: 2026-09-19 · Status: accepted

## Context

"RAG and search" is a headline skill for the roles this project targets, and the
blueprint specifies hybrid BM25 + dense retrieval with reciprocal rank fusion,
parent-child chunks and an abstention threshold. Until now `search_knowledge` was
a stub that always abstained, so the agent investigated with no runbooks at all.

The blueprint is also explicit that the gains should be attributed to stages and
measured on this corpus rather than assumed to transfer. That instruction turned
out to matter more than the architecture.

## Decision

Build the retrieval stack, and build the measurement alongside it: a labelled
query set, Recall@K and MRR, a stage ablation, and a `--no-corpus` flag on the
benchmark so the agent can be scored with and without it.

Report what the numbers say, including when they contradict the design.

## Consequences

Retrieval works well in isolation — R@3 of 93%, MRR 0.91, no off-topic query
leaking through the gate and no on-topic query wrongly refused. Two measurements
went against expectation, and both are recorded rather than smoothed over.

**Dense alone beats hybrid on this corpus.** Hybrid ties at R@1 and is slightly
worse at R@3 and MRR. The corpus is prose-heavy and the labelled queries are
paraphrases, which is dense retrieval's best case and BM25's worst. Hybrid stays
the default for a reason the table cannot express — BM25 needs no model, so it is
what still works when the embedding service is down, which is the degraded mode
the failure playbook already promised. But the *quality* case for hybrid is not
currently supported by measurement here, and claiming otherwise would be
asserting the result everyone expects.

**Retrieval does not yet help the agent.** With the corpus the local tier holds
the same accuracy, abstains more (50% to 75%), and spends 39% more tokens. The
extra prose gives an 8B model more material to write plausible-but-unsupported
claims from, and entailment rejects them. The honest reading is not that
retrieval is worthless but that the stage before it is missing: the agent sends
one naive query built from the triage summary, so the bad-deploy capsule
retrieves the *frontend* runbook — correct for the question asked, and not the
document that would have helped. Query construction is the next stage, not a
better retriever.

**The abstention gate cannot be built on fused ranks.** RRF confidence measures
whether the retrievers agree, not whether anything is relevant, and dense search
always returns a ranked list. An off-topic query both retrievers rank identically
scores near the maximum: the first gate returned a payment runbook for "how do I
bake bread" at 0.88 confidence. The gate now reads raw per-retriever relevance
against floors measured on labelled queries, and the floors are OR'd because a
query full of exact identifiers has high BM25 and mediocre cosine while a
paraphrase has the reverse. The cosine floor is a property of the embedding
model, so it moves when the model does.

**Two implementation traps, both from crossing sync and async.** The embedder
cached an `httpx.AsyncClient`, which binds to the loop that created it; the
corpus is indexed on the main loop but a query is embedded inside the gateway's
synchronous tool surface, on a worker thread with a fresh loop. That took the
whole knowledge tool down with "bound to a different event loop", and it was
scored as a *quality regression* until the traceback was read. Separately, the
corpus builder called `asyncio.run` from inside the already-running CLI loop, and
a broad `except Exception` intended as graceful degradation swallowed it — so the
benchmark silently ran with retrieval disabled and looked like it had simply made
no difference. Degradation handlers that hide bugs are worse than no handler; that
one now logs at error level and the sync entry point refuses to run inside a loop.

The limits are worth stating: four capsules, fifteen labelled queries, and the
same author wrote the corpus and the queries. These are a starting point for
hill-climbing, not a finding.
