# 8. A local model tier

Date: 2026-09-18 · Status: accepted

## Context

The Anthropic tier shipped without ever making a live API call — no credentials
were available in the environment it was written in. That left the most important
claim in the project untested: that a real model can actually drive the
investigation graph.

Ollama removes the blocker. It runs models locally, needs no key, costs nothing
per token, and supports constrained decoding against a JSON Schema — which is the
one feature the design depends on.

## Decision

Add `ollama` as a third provider behind the same `Model` protocol, using the same
prompts and the same response schemas as the hosted tier. Do not remove either
existing tier.

Three providers, one protocol:

- `stub` — deterministic, free, still the default and still what CI runs on.
- `anthropic` — hosted, billed per token.
- `ollama` — local, free, slow.

## Consequences

**The design is now falsifiable.** A full investigation has run end to end on
`llama3.1:8b`. It identified `checkout-service` rather than the louder `frontend`,
its drafted claims failed verification twice, and the report degraded to
abstention rather than asserting an unsupported conclusion. That is the intended
behaviour under a weak model, and it is the strongest available argument for
having built verification before model quality.

**Shared prompts are the point, not a convenience.** Because all three tiers send
identical prompts and parse identical schemas, they are directly comparable on the
same incident. The evaluation harness can therefore ask the question that matters
— is a hosted model worth its cost on this workload? — rather than measuring three
different prompt sets against each other.

**A weak model is a better test of the guardrails than a strong one.** Two real
bugs surfaced within minutes of the first local run and both were provider-
agnostic. `TriageResponse.severity` was a free string with a "P1, P2, P3 or P4"
description; the local model answered `"critical"`. A description is a request, an
enum is a constraint, and the weaker the model the more that distinction matters —
it is now an enum for every tier. Separately, the demo assumed a report always
carries a proposed action and crashed with an `IndexError` when the investigation
abstained, despite abstention being a designed outcome. A strong model would have
hidden both.

**The binding constraint changes.** On the hosted tier the dollar ceiling bounds
an investigation; locally there is nothing to bill, so `usd` is genuinely zero and
the *deadline* does the work instead. Local inference is slow enough that the
300-second default is not realistic — the documented local setting is 1800.
Two details follow: `num_ctx` is set explicitly, because a local default of 2–4k
silently truncates a prompt carrying a full evidence ledger and the failure looks
like a bad answer rather than an error; and `num_predict` is bounded per task, so
a small model cannot generate until the heat death of the universe.

**Live tests are opt-in.** `tests/test_ollama_live.py` runs against a real daemon
and is excluded from the default suite by an `ollama` marker, so CI and a laptop
with nothing running both stay green. `pytest -m ollama` runs them, and they skip
cleanly rather than failing when the model in question is not pulled.
