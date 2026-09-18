# 3. Redis Streams over Kafka

Date: 2026-09-18 · Status: accepted

## Context

Investigations are asynchronous: the webhook must ack fast and the work must
survive a worker dying mid-incident. That needs at-least-once delivery,
redelivery of stuck jobs, a backpressure signal and a dead-letter path.

## Decision

Redis Streams with consumer groups. Redis is already present for caching, token
buckets and progress pub/sub.

## Consequences

Worst-case alert volume is thousands per minute, and the correlator collapses a
storm into a handful of incidents before anything is enqueued. That is squarely
Redis Streams territory. Consumer groups already provide everything needed:
`XREADGROUP` for at-least-once delivery, the pending entry list for jobs a dead
worker never acked, `XAUTOCLAIM` for reclaim, stream lag as the KEDA scaling
signal, and a `:dead` stream for poison messages.

Kafka would add a second stateful system, a schema registry decision and an
operational burden, in exchange for throughput and retention this workload does
not need.

Two consequences shape the code. Ack happens only after the incident's terminal
state is persisted, so a crash between processing and ack causes redelivery
rather than a lost investigation — which means job handling must be idempotent,
enforced by the derived idempotency key on write actions. And Redis persistence
is weaker than Postgres, so Postgres is the source of truth: the outbox is what
makes a lost Redis entry recoverable by a reconciler rather than silently gone.
