# 1. Modular monolith, four entrypoints

Date: 2026-09-18 · Status: accepted

## Context

Faultline needs an HTTP surface that acks webhooks in milliseconds, workers that
run investigations for minutes, and a process that holds production credentials.
Those are genuinely different things. The tempting move is a microservice per
module; the honest constraint is that this is built by one person.

## Decision

One repository, one container image, four entrypoints: `api`, `worker`,
`gateway`, `ingest`.

Each split exists for a reason that is not architectural fashion:

- **api** scales on request rate and must never block on an investigation.
- **worker** scales on queue lag and runs for minutes per job.
- **gateway** is a security boundary — it holds every credential the
  investigation is not allowed to see.
- **ingest** is batch work that must not compete with incident traffic.

Everything else stays in-process behind module boundaries and the protocols in
`ports.py`.

## Consequences

One artifact to build, version and scan. Shared types are a function call away
rather than a schema negotiation. Refactoring a boundary is an import change.

The cost is that a runaway worker can exhaust a shared dependency, and that the
module boundaries are conventions enforced by review rather than by the network.
The protocols in `ports.py` are what keeps them honest: anything crossing a
boundary is a typed interface with two implementations, so a module that grows
into its own service already has its seam.
