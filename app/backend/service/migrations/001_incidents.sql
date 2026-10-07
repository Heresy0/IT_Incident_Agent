CREATE TABLE IF NOT EXISTS incident_schema_versions (
    version INTEGER PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW());
CREATE TABLE IF NOT EXISTS incidents (
    id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, user_id TEXT NOT NULL,
    document JSONB NOT NULL,
    business_status TEXT NOT NULL DEFAULT 'open'
        CHECK (business_status IN ('open','investigating','awaiting_confirmation','resolved')),
    revision INTEGER NOT NULL DEFAULT 1,
    latest_run_id TEXT REFERENCES research_runs(run_id) ON DELETE SET NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW());
CREATE INDEX IF NOT EXISTS incidents_owner ON incidents(tenant_id,user_id,created_at DESC);
CREATE UNIQUE INDEX IF NOT EXISTS incident_running_once
    ON research_runs(tenant_id,user_id,(config->>'incident_id'))
    WHERE config->>'task_type'='incident' AND status='running';
CREATE UNIQUE INDEX IF NOT EXISTS incident_start_key
    ON research_runs(tenant_id,user_id,(config->>'incident_id'),(config->>'request_key'))
    WHERE config->>'task_type'='incident';
CREATE TABLE IF NOT EXISTS confirmed_incident_cases (
    id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, user_id TEXT NOT NULL,
    incident_id TEXT NOT NULL UNIQUE REFERENCES incidents(id), run_id TEXT NOT NULL,
    request_key TEXT NOT NULL, document JSONB NOT NULL,
    confirmation_source TEXT NOT NULL DEFAULT 'operator_manual',
    validity TEXT NOT NULL DEFAULT 'active', revision INTEGER NOT NULL DEFAULT 1,
    confirmed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(tenant_id,user_id,incident_id,request_key));
INSERT INTO incident_schema_versions(version) VALUES (1) ON CONFLICT DO NOTHING;
