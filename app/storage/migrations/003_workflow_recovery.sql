CREATE TABLE IF NOT EXISTS run_execution (
    run_id TEXT PRIMARY KEY REFERENCES research_runs(run_id) ON DELETE CASCADE,
    schema_version TEXT NOT NULL, spec_sha256 TEXT NOT NULL,
    epoch BIGINT NOT NULL DEFAULT 0, recoveries INTEGER NOT NULL DEFAULT 0,
    started_at TIMESTAMPTZ, deadline_at TIMESTAMPTZ,
    limits JSONB NOT NULL, counts JSONB NOT NULL DEFAULT '{"model_calls":0,"tool_calls":0,"web_calls":0}',
    tokens JSONB NOT NULL DEFAULT '{"input_tokens":0,"output_tokens":0,"known_calls":0}',
    allowances JSONB NOT NULL DEFAULT '{}', active_stage TEXT,
    clocks JSONB NOT NULL DEFAULT '[]', candidate JSONB, candidate_sha256 TEXT,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW());
CREATE TABLE IF NOT EXISTS run_operations (
    run_id TEXT NOT NULL REFERENCES run_execution(run_id) ON DELETE CASCADE,
    stage TEXT NOT NULL, ordinal INTEGER NOT NULL, kind TEXT NOT NULL,
    request_sha256 TEXT NOT NULL, request JSONB NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('reserved','responded','failed','unknown')),
    response JSONB, error JSONB, reserved_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), settled_at TIMESTAMPTZ,
    PRIMARY KEY (run_id,stage,ordinal));
CREATE TABLE IF NOT EXISTS run_snapshots (
    run_id TEXT NOT NULL REFERENCES run_execution(run_id) ON DELETE CASCADE,
    stage TEXT NOT NULL, boundary TEXT NOT NULL CHECK (boundary IN ('entry','done')),
    payload JSONB NOT NULL, sha256 TEXT NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (run_id,stage,boundary));
ALTER TABLE research_events ADD COLUMN IF NOT EXISTS event_key TEXT;
CREATE UNIQUE INDEX IF NOT EXISTS research_event_once ON research_events(run_id,event_key)
    WHERE event_key IS NOT NULL;
INSERT INTO incident_schema_versions(version) VALUES (3) ON CONFLICT DO NOTHING;
