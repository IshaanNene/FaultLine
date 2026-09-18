# 5. Compress telemetry at the source

Date: 2026-09-18 · Status: accepted

## Context

An investigation touches metrics, logs, traces, Kubernetes state and deploy
history. Raw, that is gigabytes. The obvious design gives the agent a query
language and lets it read what it wants.

[SREGym](https://arxiv.org/pdf/2605.07161) measured what that costs: general
coding agents pulling raw observability data used 3.0–3.6× the tokens of a
specialized agent that preprocessed it first — without better results.

## Decision

Intent-shaped tools that do the querying and the statistics in Python and return
a summary plus typed facts. Raw payloads go to object storage and are referenced
by content hash.

Concretely: `get_service_health` returns change points and baseline deltas, not
series. `search_logs` returns Drain3-style templates with counts and a
new-versus-baseline diff, not lines. `rank_suspects` returns a deterministic
ranking computed by propagating blame across the trace-derived service graph.

Raw PromQL survives as a validated escape hatch, because sometimes the right
question has no tool.

## Consequences

This is the largest cost lever available, and it compounds: fewer tokens per
check means more checks inside the same budget, which means better differential
diagnosis.

It also improves correctness in a way that is easy to miss. Models write invalid
PromQL. Change-point detection, baseline comparison, log template mining and
graph ranking are deterministic, testable Python — the model decides what the
numbers *mean*, which is the part it is actually good at. The suspect ranking
identifies `checkout-service` over the louder `frontend` before any model runs,
and `tests/test_tools.py` asserts exactly that.

The cost is that each tool is an opinion about what matters, and a badly designed
tool hides the fact that would have cracked the case. That is a real risk, and
the mitigation is the evaluation harness: if a fault class is consistently
missed, the tool that should have surfaced it is the thing to fix.
