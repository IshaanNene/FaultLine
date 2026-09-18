# Faultline — AI Engineering Flagship Project Blueprint

2026-09-18

> This is the design document the implementation is built against. It plans
> roughly six months at ~15 hours a week. See the README's
> "What is not built yet" for where the code currently stands against it.

## Executive summary

Build Faultline: an agentic system that turns a production alert into a verified,
cited root-cause analysis (RCA) for a Kubernetes microservice application, with
human-approved remediation.

It beats the alternatives because the infrastructure is the problem domain rather
than decoration. Kubernetes, OpenTelemetry, queues and observability are what the
agent investigates, so each technology earns its place instead of padding the
architecture diagram.

The investigation loop works like a senior on-call engineer. Faultline groups
related alerts, retrieves runbooks, similar past incidents and recent changes,
then forms hypotheses. It tests them with read-only tools over metrics, logs,
traces and deploy history; every claim is checked against recorded evidence, and
any remediation waits for human approval.

The flagship differentiator is evaluation with ground truth. You inject known
faults into a real microservice application and record the telemetry into
replayable "incident capsules". Then you score whether the system found the true
root cause, and at what cost and latency.

The same harness compares a single-call RAG baseline, an improved RAG pipeline
and the full agent, and it runs in CI.

| Skill area | Where Faultline demonstrates it |
| --- | --- |
| RAG and search | Hybrid BM25 + dense retrieval, reranking and parent-child chunks over runbooks and postmortems; similar-incident retrieval |
| Agentic AI | LangGraph hypothesis-test-verify loop with checkpoints, budgets and human approval |
| LLM evaluation | Fault-injection benchmark, record/replay, ablations with confidence intervals, judge calibration |
| Backend and distributed systems | Idempotent webhooks, Redis Streams consumer groups, crash-resume, backpressure, alert-storm handling |
| Security | Prompt injection arriving through log lines, a credential-isolated tool gateway, tenant isolation |
| MLOps and DevOps | Model routing and fallback, prompt versioning, eval gates in CI, Helm, KEDA, OpenTelemetry |

Scope guardrails keep it buildable by one student: a modular monolith deployed as
three or four processes, Postgres (pgvector plus BM25) instead of a separate
vector database, Redis Streams instead of Kafka, and one Kubernetes cluster.

## Why incident investigation

Incident root-cause investigation scored 37 of 40 against twelve alternatives,
five points ahead of the runner-up, because it is the only candidate where
systems depth, live ground truth and a necessary agent all come from the problem
itself.

| Candidate | AI depth | Systems depth | Ground truth | Uniqueness | Feasibility | Demo | Interview | Longevity | Total /40 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1. Incident investigation | 5 | 5 | 5 | 4 | 3 | 5 | 5 | 5 | 37 |
| 2. Security alert triage | 5 | 3 | 4 | 4 | 3 | 4 | 4 | 5 | 32 |
| 3. Warehouse analytics | 4 | 3 | 5 | 2 | 4 | 4 | 4 | 4 | 30 |
| 7. Pipeline failure diagnosis | 4 | 4 | 3 | 4 | 3 | 4 | 4 | 4 | 30 |
| 5. Clinical trial matching | 4 | 2 | 5 | 3 | 3 | 3 | 3 | 4 | 27 |
| 4. Financial filings analyst | 4 | 2 | 4 | 2 | 4 | 3 | 3 | 4 | 26 |

**The infrastructure is the subject, not the packaging.** Kubernetes,
OpenTelemetry, Prometheus and deploy history are what the agent reasons over.

**The agent is necessary.** The evidence you need depends on the hypothesis you
hold, and it lives in live systems rather than a static corpus, so a single
retrieval pass cannot answer the question.

**Ground truth is live and public.** You inject a known fault, so you know the
right answer. [SREGym](https://arxiv.org/pdf/2605.07161) provides 90 live fault
scenarios on Kubernetes with published baselines for Claude Code, Codex and a
specialized SRE agent; [OpenRCA](https://github.com/microsoft/OpenRCA) provides
335 offline failures with over 68 GB of telemetry.

**The design targets documented failure modes.** SREGym found that agents anchor
on the first plausible anomaly, even noise, and that general coding agents used
3.0–3.6× more tokens than a specialized agent that preprocesses observability
data.

**Security is concrete rather than theoretical.** Log lines, span attributes and
ticket text are attacker-controllable, and they flow into an agent that can touch
production.

### What you give up

RAG is a supporting actor rather than the star. Setup costs more than a document
project: plan on 16 GB of RAM for a local kind cluster. The domain has a learning
curve — PromQL, traces, Kubernetes objects, failure patterns. Injected faults are
not real incidents; mitigate with ambient noise, postmortem-derived scenarios,
held-out fault families and an honest limitations section.

## Product definition

| Mode | Input | Output | Latency target |
| --- | --- | --- | --- |
| Investigate | Alertmanager webhook, or an "investigate" button | Streaming triage, then an RCA report: root cause, causal chain, evidence, confidence, rejected alternatives, proposed actions | First triage < 30 s; full RCA p90 < 5 min |
| Ask | A question in the UI or Slack | A cited answer from runbooks, postmortems and incident history, or an explicit "not found" | p95 < 8 s |

Non-goals: no autonomous writes to production, no replacement for monitoring or
alerting, no general-purpose chat.

## RAG architecture

Retrieval is four different problems, and only one is classic document RAG.

| Knowledge source | Retrieval method | Why |
| --- | --- | --- |
| Runbooks, architecture docs, service catalog | Hybrid BM25 + dense, RRF, cross-encoder rerank, parent-child sections | Alerts mix exact identifiers with fuzzy symptoms |
| Confirmed past incidents | Structured signature match plus dense similarity on the summary | "Has this happened before?" depends on services, error templates and fault class more than prose |
| Change events | Time-window and topology query in SQL, no vectors | Causality is temporal |
| Live telemetry | Tools that analyze next to the data and return summaries; never embedded | Volume and freshness |

Index child chunks of 200–400 tokens for precise matching, return the parent
section (~1,500 tokens) for generation. Take the top 50 from each retriever, fuse
with RRF at k = 60, rerank the top 50, keep three to eight parent sections.

Start with deterministic chunk context (document title, section path, service,
type), then test LLM-written context as an ablation.
[Anthropic reported](https://anthropic.com/news/contextual-retrieval) that
contextual embeddings plus contextual BM25 cut top-20 retrieval failures by 49%,
and 67% with reranking — measure it on your own corpus rather than assuming it
transfers.

When the top rerank score falls below a calibrated threshold, Faultline says "no
matching runbook" and logs a knowledge-gap event. A weekly gap report to runbook
owners is a genuinely useful product feature.

Only human-confirmed root causes enter incident memory, which prevents
hallucinated conclusions from poisoning future investigations. Benchmark
scenarios in the test split never seed memory.

The long-context baseline keeps RAG honest: a corpus under ~200k tokens can go
straight into the prompt, so the benchmark includes a "whole corpus in context"
arm. RAG must win on cost and latency at equal quality, or the README should say
it lost.

Deliberately left out: GraphRAG and LLM-extracted knowledge graphs (the real
graph comes from traces), HyDE, agentic chunking, fine-tuned embeddings — until
an ablation justifies them.

## Agent and tool architecture

| Tool | Returns (always compressed) | Guardrails |
| --- | --- | --- |
| `get_service_health` | Rate, errors, duration with change points and baseline deltas | Templated PromQL; window capped at 6 h |
| `query_metrics` | Series summary: range, change points, top series | Parsed and validated; escape hatch only |
| `search_logs` | Log templates with counts, new-vs-baseline diff, redacted examples | Line caps; PII and secret redaction; injection flagging |
| `get_trace_summary` | Error spans by operation, latency outliers, slowest critical paths | Sampling caps |
| `get_topology` | Dependency subgraph from the trace-derived service graph | Depth capped at 3 |
| `get_recent_changes` | Deploys, image tags, config and flag changes, Kubernetes events | Read-only |
| `get_k8s_state` | Pods, restarts, probes, limits, env var names but never values | RBAC get/list/watch only; no secrets; no exec |
| `rank_suspects` | Classical ranking by anomaly propagation over the service graph | Deterministic |
| `search_knowledge` | Runbook sections with citations | Tenant filter enforced server-side |
| `find_similar_incidents` | Confirmed past incidents and their fixes | Confirmed memory only |
| `rollback`, `set_flag`, `scale`, `restart` | An action proposal, never an execution | Catalog, policy engine, approval token |

Four principles, which matter more than the choice of framework:

1. **Intent-shaped tools beat raw query languages.** Models write invalid PromQL.
2. **Compress at the source.** The biggest cost lever available.
3. **Every tool result becomes a ledger entry** with an id, normalized query, time
   range, summary, content hash and a pointer to the raw result.
4. **Statistics in code, judgment in the model.**

Planning is differential diagnosis. The hypothesize node must produce competing
hypotheses; plan-checks prefers the checks whose outcomes differ most between the
top two. A cheap baseline check demotes anomalies that predate the first bad
minute as likely bystanders.

Verification, in order: entity grounding, citation integrity, numeric fidelity,
temporal logic, entailment, a refutation probe, and abstention as a first-class
outcome.

Memory is used only where it earns its place: working memory is graph state,
episodic memory holds human-confirmed incidents, semantic memory is the document
corpus. There is no per-user conversational memory — nothing needs personalizing,
and it would widen the injection surface.

## Behavior under production conditions

| Concern | Mechanism | Starting setting |
| --- | --- | --- |
| Horizontal scaling | Stateless API pods; workers pull from a consumer group | HPA on CPU; KEDA on pending entries |
| Concurrency | Async I/O; per-worker investigation limit; per-backend semaphores | 4 investigations/worker; 8 concurrent calls/backend |
| Rate limiting | Redis token buckets per tenant and per provider | 429 with Retry-After; severity lanes |
| Retries | Exponential backoff with full jitter, plus a retry budget | 3 attempts, idempotent operations only |
| Circuit breakers | Per LLM provider and per telemetry backend | Open at 50% failures over 20 calls; half-open after 30 s |
| Queue processing | Ack after the incident is persisted; reclaim stuck jobs; dead-letter | Reclaim after 5 min idle; dead-letter after 5 deliveries |
| Caching | Embedding, per-incident tool-result, provider prompt prefix, Ask-path semantic | TTLs per cache |
| Idempotency | Dedupe on fingerprint and group key; unique constraints; idempotency keys | Webhook retries never duplicate |
| Consistency | Postgres is truth; transactional outbox; reconciler re-enqueues expired leases | Reconciler every 60 s |
| Versioning | Every investigation records prompt, model, tool, index and graph-schema versions | Any past result reproducible |

### Latency and cost

Budgets are explicit in state: tokens, dollars, tool calls, deadline. A
reasonable ceiling is ~0.5M tokens, close to SREGym's measurement for a
specialized SRE agent against 1.6–1.9M for general coding agents.

Latency levers, in order: stream triage early, parallel prefetch and checks,
small model for small tasks, stable prompt prefixes for provider caching, start
prefetch the moment an alert lands.

Cost levers, in order: dedupe before any model runs, compress at the source,
cascade small → frontier only on low confidence, reuse cached prefixes, batch
non-urgent work.

### Failure playbook

| Scenario | System behavior | What responders see |
| --- | --- | --- |
| LLM unavailable | Fall back to the second provider; if all are down, post the deterministic evidence packet and park the checkpointed graph | "AI analysis delayed" plus changes, suspects, similar incidents, runbooks |
| Vector search unavailable | BM25-only retrieval; hybrid design makes this work | "Retrieval degraded" banner; no lost alerts |
| Poor retrieval | One corrective rewrite, then abstain and log a knowledge gap | "No matching runbook; telemetry-only investigation" |
| A tool fails | Retry idempotent reads, record an evidence gap, switch sources, lower confidence | Gaps listed explicitly |
| Model hallucinates | Verification catches it; one revision, then drop the claim or abstain | Never an invented service or command |
| Prompt injection via logs | All tool output is data; flagged lines quarantined; capability limits bound the impact | Flagged evidence highlighted; no unapproved writes |
| 100× alert storm | Correlator groups by fingerprint, topology and time; P1 first; per-tenant caps; P3/P4 triage only | P1s investigated first |
| Worker crashes | Job redelivered, graph resumes from checkpoint; execution idempotent | A short pause, no duplicate actions |
| Deploy mid-investigation | Graceful shutdown; new code resumes with schema-version checks | Nothing visible |

The 100× case holds the most important insight: the binding constraint is the
provider's tokens-per-minute quota, not CPU. Scaling workers past the quota only
converts queue time into 429s. The shared token bucket and severity scheduler are
what actually protect P1s.
