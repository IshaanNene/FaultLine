# 7. Two model tiers behind one protocol

Date: 2026-09-18 · Status: accepted

## Context

The walking skeleton shipped with a deterministic stub standing in for every
model call. That was the right call for building the graph, but a stub cannot be
meaningfully evaluated and gains nothing from better retrieval — everything
downstream was measuring a placeholder.

The obvious move is to replace the stub. That would be a mistake: CI would then
need an API key, every test run would cost money and take seconds instead of
milliseconds, and the queue, verification, approval and recovery paths would stop
being testable in isolation from model quality.

## Decision

Keep both tiers behind the same `Model` protocol. `StubModel` stays the default
and remains what CI runs on; `AnthropicModel` is opt-in via
`FAULTLINE_MODEL_PROVIDER=anthropic`.

Routing is per task, not per investigation: a small model handles triage, query
writing, entailment and summaries; a frontier model handles hypothesize, plan,
assess and synthesize. Each tier declares a second model the router falls back to
when the first one's breaker opens.

## Consequences

The 194-test suite still runs in under a second with no credentials, and the
end-to-end investigation remains a CI gate rather than a thing you hope works.

Three design points follow from taking the boundary seriously.

**Model-facing schemas are separate from domain types.** Structured outputs need
a closed JSON Schema, and the domain types carry `dict[str, Any]` fields that
cannot be expressed under `additionalProperties: false`. Rather than loosening
the domain types, `worker/responses.py` defines narrow schemas and
`worker/adapt.py` is the single place they become domain objects. The narrowness
turns out to be a guardrail worth more than the schema compliance: the tool field
is an enum, the arguments are typed, and the synthesis schema has no field for a
proposed action — so the model cannot name a tool, an argument or a command that
does not exist.

**The system prompt is a cached constant.** Caching is a prefix match, so a
single per-incident detail in the system block would cost full price on every
request. Everything volatile is in the user message, and a test asserts the
system block is byte-identical across two different incidents. Cache reads bill
at a tenth of the input rate.

**Pricing is explicit and fails loudly.** `price()` reads usage off the response
and charges cached reads and writes at their own rates. An unknown model raises
rather than billing zero, because a silent zero would quietly disable every
budget ceiling in `core/budget.py`.

The cost accepted: two tiers is two code paths, and a bug that only appears under
a real model will not be caught by CI. The mitigation is that the tiers share the
graph, the verification and the budget accounting — the divergence is confined to
prompt rendering and response adaptation, both of which are tested against a fake
client. The evaluation harness is what will eventually compare the two on the same
scenarios, which is the real answer to "is the model actually better than the
stub".
