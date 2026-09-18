# 4. LangGraph for the investigation loop

Date: 2026-09-18 · Status: accepted

## Context

The investigation needs cycles with termination rules, a durable pause for human
approval that can outlive the process, parallel fan-out with deterministic
merging, and state that can be unit tested node by node.

The alternatives are a hand-rolled state machine, or a free-form ReAct loop.

## Decision

LangGraph, with a Postgres checkpointer keyed on the incident id.

## Consequences

A free-form ReAct loop is the wrong tool here for a specific reason: it cannot
guarantee that verification runs, that budgets hold, or where the approval gate
sits. Those are the properties that make this system safe to point at production,
and they have to be structural.

LangGraph gives durable interrupts, which is the hard part. `interrupt()` parks
an investigation; the API's approve endpoint enqueues a resume job; any worker
resumes from the checkpoint. The end-to-end test proves this by finishing in a
different worker than it started in.

One rule from LangGraph's semantics shapes the whole node layer: **on resume, an
interrupted node restarts from its beginning**, so anything before the
`interrupt()` call runs twice. Side effects are therefore confined to the
`execute` node, behind an idempotency key, and the `approval` node does nothing
but interrupt.

Two costs are accepted deliberately. Checkpointed state is a compatibility
surface, so `InvestigationState` carries a `schema_version` that is checked on
resume — a worker running new code fails loudly rather than misreading an
in-flight investigation. And LangGraph's default serializer will construct any
type it finds in a checkpoint, which matters because checkpoints share a database
with everything else; `worker/serde.py` pins it to an explicit allowlist.

What LangGraph is *not* allowed to own: routing policy, budgets, verification and
the evidence ledger are plain Python that we test directly.
