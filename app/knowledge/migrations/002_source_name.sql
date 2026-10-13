-- Optional display metadata for browser uploads; existing CLI documents stay valid.
ALTER TABLE incident_runbook_documents ADD COLUMN IF NOT EXISTS source_name TEXT
    CHECK (source_name IS NULL OR length(source_name) BETWEEN 1 AND 180);
