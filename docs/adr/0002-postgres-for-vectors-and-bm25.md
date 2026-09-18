# 2. Postgres for vectors and BM25

Date: 2026-09-18 · Status: accepted

## Context

Faultline stores incidents, LangGraph checkpoints, an outbox, an audit log and a
retrieval corpus of runbooks and postmortems. The corpus needs both lexical and
dense search: alerts mix exact identifiers (`OOMKilled`, `ECONNREFUSED`, metric
names) with fuzzy symptom descriptions, and neither retrieval method handles both.

The default reach is a dedicated vector database alongside Postgres.

## Decision

One Postgres, using the ParadeDB image, which ships pgvector and pg_search
(BM25) in the same instance.

## Consequences

The decisive argument is transactional, not performance. The webhook must write
an incident row and enqueue an investigation job atomically. With a second store
in the picture that becomes a distributed write; with one Postgres it is an
`INSERT` and an outbox row in a single transaction (ADR 3 and `db/init/001_schema.sql`).
Tenant isolation likewise lives in one place, as row-level security keyed on a
per-transaction `app.tenant_id`, instead of being reimplemented per store.

Vectors are stored as `halfvec` with an HNSW index, which roughly halves index
size at negligible recall cost.

The cost is that Postgres will be the first thing to hurt at scale, and that
ParadeDB is a less common base image than stock Postgres. The mitigation is the
repository interface: retrieval already sits behind a method, so moving to a
dedicated engine is a new adapter rather than a rewrite. That move should be
triggered by a *measured* regression in recall or latency, not by anticipation.
