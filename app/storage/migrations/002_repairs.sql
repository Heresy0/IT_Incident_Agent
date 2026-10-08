CREATE TABLE IF NOT EXISTS incident_repairs (
    incident_id TEXT PRIMARY KEY REFERENCES incidents(id) ON DELETE CASCADE,
    document JSONB NOT NULL DEFAULT '[]'::jsonb CHECK (jsonb_typeof(document)='array')
);
INSERT INTO incident_schema_versions(version) VALUES (2) ON CONFLICT DO NOTHING;
