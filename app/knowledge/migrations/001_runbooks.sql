-- Applied only by explicit ingestion; ordinary diagnosis never creates tables.
CREATE TABLE IF NOT EXISTS incident_runbook_documents (
    scope_id TEXT NOT NULL CHECK (scope_id ~ '^[a-f0-9]{64}$'),
    document_id TEXT NOT NULL CHECK (document_id ~ '^[A-Za-z0-9][A-Za-z0-9_.-]{0,79}$'),
    tenant_id TEXT NOT NULL, service TEXT NOT NULL,
    environment TEXT NOT NULL CHECK (environment IN ('staging','production')),
    service_version TEXT NOT NULL, schema_version TEXT NOT NULL,
    embedding_model TEXT NOT NULL,
    embedding_dimension INTEGER NOT NULL CHECK (embedding_dimension BETWEEN 2 AND 4096),
    category TEXT NOT NULL CHECK (category IN ('resource','dependency','configuration')),
    updated_at TIMESTAMPTZ NOT NULL,
    revision TEXT NOT NULL CHECK (revision ~ '^[a-f0-9]{64}$'),
    chunk_count INTEGER NOT NULL CHECK (chunk_count BETWEEN 1 AND 200),
    PRIMARY KEY (scope_id, document_id),
    UNIQUE (scope_id, document_id, embedding_dimension)
);

CREATE TABLE IF NOT EXISTS incident_runbook_chunks (
    scope_id TEXT NOT NULL, document_id TEXT NOT NULL,
    chunk_id TEXT NOT NULL CHECK (chunk_id ~ '^[a-f0-9]{64}$'),
    text TEXT NOT NULL CHECK (length(btrim(text)) BETWEEN 1 AND 1800),
    embedding_dimension INTEGER NOT NULL,
    embedding vector NOT NULL,
    CHECK (vector_dims(embedding) = embedding_dimension AND vector_norm(embedding) > 0),
    PRIMARY KEY (scope_id, document_id, chunk_id),
    FOREIGN KEY (scope_id, document_id, embedding_dimension)
        REFERENCES incident_runbook_documents(scope_id, document_id, embedding_dimension) ON DELETE CASCADE
);
