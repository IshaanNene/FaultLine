-- Faultline schema.
--
-- One store for incidents, the transactional outbox, approvals, the audit log,
-- and (next phase) the retrieval corpus with both pgvector and BM25 indexes.
-- The ParadeDB image ships Postgres with pgvector and pg_search already in it.

CREATE EXTENSION IF NOT EXISTS vector;

-- ---------------------------------------------------------------- incidents

CREATE TABLE IF NOT EXISTS incidents (
    incident_id   text PRIMARY KEY,
    tenant_id     text        NOT NULL,
    group_key     text        NOT NULL,
    status        text        NOT NULL,
    severity      text        NOT NULL,
    services      text[]      NOT NULL DEFAULT '{}',
    title         text        NOT NULL DEFAULT '',
    alert_count   integer     NOT NULL DEFAULT 0,
    report        jsonb,
    created_at    timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now(),

    -- The idempotency guarantee behind the webhook: a redelivered alert group
    -- maps to the same key, so the INSERT conflicts instead of duplicating.
    CONSTRAINT incidents_tenant_group_key UNIQUE (tenant_id, group_key)
);

CREATE INDEX IF NOT EXISTS incidents_tenant_created_idx
    ON incidents (tenant_id, created_at DESC);

-- Most queries are "what is still open?", which is a small slice of the table.
CREATE INDEX IF NOT EXISTS incidents_open_idx
    ON incidents (tenant_id, updated_at DESC)
    WHERE status NOT IN ('resolved', 'escalated', 'closed_noise', 'closed_duplicate');

-- ---------------------------------------------------------------- approvals

CREATE TABLE IF NOT EXISTS approvals (
    id          bigserial PRIMARY KEY,
    incident_id text        NOT NULL REFERENCES incidents (incident_id) ON DELETE CASCADE,
    action_id   text        NOT NULL,
    decision    text        NOT NULL CHECK (decision IN ('approve', 'reject', 'edit')),
    actor       text        NOT NULL,
    payload     jsonb       NOT NULL,
    decided_at  timestamptz NOT NULL DEFAULT now(),

    -- A double-click must not become two approvals of the same action.
    CONSTRAINT approvals_once_per_action UNIQUE (incident_id, action_id)
);

-- ------------------------------------------------------------------- outbox
-- Written in the same transaction as the incident row. Without this, the
-- database can commit an incident while the enqueue silently fails, leaving an
-- alert nobody investigates.

CREATE TABLE IF NOT EXISTS outbox (
    id         bigserial PRIMARY KEY,
    stream     text        NOT NULL,
    payload    jsonb       NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    sent_at    timestamptz
);

CREATE INDEX IF NOT EXISTS outbox_unsent_idx ON outbox (id) WHERE sent_at IS NULL;

-- ---------------------------------------------------------------- audit log
-- Append-only and hash-chained. No UPDATE or DELETE grant is ever issued on it.

CREATE TABLE IF NOT EXISTS audit_log (
    id            bigserial PRIMARY KEY,
    incident_id   text        NOT NULL,
    actor         text        NOT NULL,
    action        text        NOT NULL,
    detail        jsonb       NOT NULL DEFAULT '{}'::jsonb,
    at            timestamptz NOT NULL DEFAULT now(),
    previous_hash text        NOT NULL,
    entry_hash    text        NOT NULL UNIQUE
);

CREATE INDEX IF NOT EXISTS audit_log_incident_idx ON audit_log (incident_id, id);

-- --------------------------------------------------------- retrieval corpus
-- Populated by the ingestion workers. Child chunks are indexed for precise
-- matching; generation is handed the parent section.

CREATE TABLE IF NOT EXISTS documents (
    document_id text PRIMARY KEY,
    tenant_id   text        NOT NULL,
    path        text        NOT NULL,
    doc_type    text        NOT NULL,
    service     text,
    commit_sha  text        NOT NULL,
    owner       text,
    trust_level text        NOT NULL DEFAULT 'internal',
    updated_at  timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT documents_tenant_path UNIQUE (tenant_id, path)
);

CREATE TABLE IF NOT EXISTS chunks (
    chunk_id      text PRIMARY KEY,
    document_id   text  NOT NULL REFERENCES documents (document_id) ON DELETE CASCADE,
    tenant_id     text  NOT NULL,
    parent_id     text,
    section_path  text  NOT NULL DEFAULT '',
    content       text  NOT NULL,
    content_hash  text  NOT NULL,
    token_count   integer NOT NULL DEFAULT 0,
    -- halfvec keeps the HNSW index roughly half the size at negligible recall cost.
    embedding     halfvec(1024),
    CONSTRAINT chunks_dedupe UNIQUE (tenant_id, content_hash)
);

CREATE INDEX IF NOT EXISTS chunks_embedding_idx
    ON chunks USING hnsw (embedding halfvec_cosine_ops);

CREATE INDEX IF NOT EXISTS chunks_tenant_service_idx ON chunks (tenant_id, section_path);

-- BM25, via ParadeDB's pg_search. Guarded so the schema still applies on a plain
-- Postgres image during early development.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_available_extensions WHERE name = 'pg_search') THEN
        CREATE EXTENSION IF NOT EXISTS pg_search;
        EXECUTE 'CREATE INDEX IF NOT EXISTS chunks_bm25_idx ON chunks
                 USING bm25 (chunk_id, content, section_path)
                 WITH (key_field=''chunk_id'')';
    END IF;
END
$$;

-- ------------------------------------------------------- tenant isolation
-- The filter lives in the database, not in application code, so a forgotten
-- WHERE clause is a no-op rather than a cross-tenant leak.

ALTER TABLE incidents ENABLE ROW LEVEL SECURITY;
ALTER TABLE documents ENABLE ROW LEVEL SECURITY;
ALTER TABLE chunks    ENABLE ROW LEVEL SECURITY;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_policies WHERE tablename = 'incidents' AND policyname = 'incidents_tenant_isolation') THEN
        CREATE POLICY incidents_tenant_isolation ON incidents
            USING (tenant_id = current_setting('app.tenant_id', true)
                   OR current_setting('app.tenant_id', true) IS NULL);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_policies WHERE tablename = 'documents' AND policyname = 'documents_tenant_isolation') THEN
        CREATE POLICY documents_tenant_isolation ON documents
            USING (tenant_id = current_setting('app.tenant_id', true)
                   OR current_setting('app.tenant_id', true) IS NULL);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_policies WHERE tablename = 'chunks' AND policyname = 'chunks_tenant_isolation') THEN
        CREATE POLICY chunks_tenant_isolation ON chunks
            USING (tenant_id = current_setting('app.tenant_id', true)
                   OR current_setting('app.tenant_id', true) IS NULL);
    END IF;
END
$$;
