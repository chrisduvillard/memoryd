-- memoryd migration 007: idempotent daemon mutation requests
BEGIN;

CREATE TABLE IF NOT EXISTS api_request_ledger (
  request_id   TEXT PRIMARY KEY,
  endpoint     TEXT NOT NULL,
  body_sha256  TEXT NOT NULL,
  response     JSONB NOT NULL,
  committed_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);

COMMIT;
