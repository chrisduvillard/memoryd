-- memoryd migration 006: durable capture provenance and idempotency
BEGIN;

ALTER TABLE events ADD COLUMN IF NOT EXISTS source_adapter TEXT;
ALTER TABLE events ADD COLUMN IF NOT EXISTS source_event_id TEXT;
ALTER TABLE events ADD COLUMN IF NOT EXISTS source_seq BIGINT;
ALTER TABLE events ADD COLUMN IF NOT EXISTS ingest_job_id TEXT;

CREATE UNIQUE INDEX IF NOT EXISTS events_source_identity
  ON events (source_adapter, session_id, source_event_id)
  WHERE source_adapter IS NOT NULL AND source_event_id IS NOT NULL;

COMMIT;
