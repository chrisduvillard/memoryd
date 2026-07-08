-- memoryd migration 001: canonical schema (slice architecture v1, §3)
-- Postgres 16 + pgvector. Idempotent-ish: guarded creates where cheap.

BEGIN;

CREATE EXTENSION IF NOT EXISTS vector;

-- ---------------------------------------------------------------
-- 3.2 Event ledger (the WAL; append-only, enforced by trigger)
-- ---------------------------------------------------------------
CREATE TABLE IF NOT EXISTS events (
  id            TEXT PRIMARY KEY,              -- evt_<ulid>
  ts            TIMESTAMPTZ NOT NULL,
  kind          TEXT NOT NULL CHECK (kind IN (
                  'user_message','agent_response','tool_call','tool_result',
                  'session_start','session_end','recall_packet','capture_ack',
                  'promotion','supersession','veto','miss_signal')),
  session_id    TEXT NOT NULL,
  agent         TEXT NOT NULL DEFAULT 'claude-code',
  project       TEXT,
  raw_sha256    TEXT,
  payload       JSONB NOT NULL DEFAULT '{}',
  meta          BOOLEAN NOT NULL DEFAULT FALSE,
  barcode       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS events_session_ts ON events (session_id, ts);
CREATE INDEX IF NOT EXISTS events_project_ts ON events (project, ts);
CREATE INDEX IF NOT EXISTS events_kind_ts    ON events (kind, ts) WHERE NOT meta;

CREATE OR REPLACE FUNCTION forbid_mutation() RETURNS trigger AS $$
BEGIN
  RAISE EXCEPTION 'events ledger is append-only (% on %)', TG_OP, TG_TABLE_NAME;
END $$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS events_append_only ON events;
CREATE TRIGGER events_append_only
  BEFORE UPDATE OR DELETE ON events
  FOR EACH ROW EXECUTE FUNCTION forbid_mutation();

-- ---------------------------------------------------------------
-- 3.3 Memory objects (dual-body canonical unit)
-- ---------------------------------------------------------------
DO $$ BEGIN
  CREATE TYPE mem_status AS ENUM
    ('candidate','active','quarantined','rejected','superseded');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
  CREATE TYPE mem_type AS ENUM
    ('identity','preference','writing_style','project_state','decision',
     'open_question','commitment','person','company','technical_fact',
     'workflow','constraint','procedure','directive','warning','priming');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

CREATE TABLE IF NOT EXISTS memories (
  id            TEXT PRIMARY KEY,              -- mem_<ulid>
  type          mem_type NOT NULL,
  text          TEXT NOT NULL,
  struct        JSONB NOT NULL DEFAULT '{}',
  project       TEXT,
  scope         TEXT NOT NULL DEFAULT 'work_private' CHECK (scope IN
                  ('personal_private','work_private','project_shared',
                   'agent_internal','public','untrusted_external')),
  sensitivity   TEXT NOT NULL DEFAULT 'normal' CHECK (sensitivity IN
                  ('public','normal','private','sealed')),
  authority     TEXT NOT NULL CHECK (authority IN ('A1','A2','B1','C1','D1','Q')),
  confidence    REAL NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
  status        mem_status NOT NULL DEFAULT 'candidate',
  valid_from    DATE NOT NULL DEFAULT CURRENT_DATE,
  valid_to      DATE,
  activation    JSONB NOT NULL DEFAULT '{}',
  half_life_d   INTEGER,
  last_confirmed_at TIMESTAMPTZ,
  recall_count  INTEGER NOT NULL DEFAULT 0,
  useful_count  INTEGER NOT NULL DEFAULT 0,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  is_canary     BOOLEAN NOT NULL DEFAULT FALSE,
  fts           tsvector GENERATED ALWAYS AS (to_tsvector('simple', text)) STORED
);
CREATE INDEX IF NOT EXISTS memories_fts    ON memories USING gin (fts);
CREATE INDEX IF NOT EXISTS memories_lookup ON memories (status, project, type);

CREATE TABLE IF NOT EXISTS memory_sources (
  memory_id TEXT NOT NULL REFERENCES memories(id),
  event_id  TEXT NOT NULL REFERENCES events(id),
  role      TEXT NOT NULL DEFAULT 'derived_from',
  PRIMARY KEY (memory_id, event_id)
);

CREATE TABLE IF NOT EXISTS supersessions (
  old_id    TEXT NOT NULL REFERENCES memories(id),
  new_id    TEXT NOT NULL REFERENCES memories(id),
  reason    TEXT,
  at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (old_id, new_id),
  CHECK (old_id <> new_id)
);

CREATE TABLE IF NOT EXISTS treatments (
  id        BIGSERIAL PRIMARY KEY,
  memory_id TEXT NOT NULL REFERENCES memories(id),
  kind      TEXT NOT NULL CHECK (kind IN (
              'affirmed','limited','distinguished','questioned',
              'confirmed_by_user','contradicted_by_user','stale','suppressed')),
  by_ref    TEXT,
  note      TEXT,
  at        TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS treatments_memory ON treatments (memory_id, at);

-- Firebreak: "no source thread, no authority".
-- A memory may not become ACTIVE unless it has >= 1 source event.
CREATE OR REPLACE FUNCTION enforce_source_on_active() RETURNS trigger AS $$
BEGIN
  IF NEW.status = 'active' THEN
    IF NOT EXISTS (SELECT 1 FROM memory_sources ms WHERE ms.memory_id = NEW.id) THEN
      RAISE EXCEPTION 'memory % cannot be active without a source event (Ariadne rule)', NEW.id;
    END IF;
  END IF;
  RETURN NEW;
END $$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS memories_source_gate ON memories;
CREATE TRIGGER memories_source_gate
  BEFORE INSERT OR UPDATE OF status ON memories
  FOR EACH ROW EXECUTE FUNCTION enforce_source_on_active();

-- Firebreak: memory text is immutable once active (supersede, don't edit).
CREATE OR REPLACE FUNCTION forbid_active_text_edit() RETURNS trigger AS $$
BEGIN
  IF OLD.status = 'active' AND NEW.text IS DISTINCT FROM OLD.text THEN
    RAISE EXCEPTION 'active memory % text is immutable; create a superseding memory instead', OLD.id;
  END IF;
  RETURN NEW;
END $$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS memories_text_immutable ON memories;
CREATE TRIGGER memories_text_immutable
  BEFORE UPDATE ON memories
  FOR EACH ROW EXECUTE FUNCTION forbid_active_text_edit();

-- Supersession side-effect: mark old memory superseded, close validity.
CREATE OR REPLACE FUNCTION apply_supersession() RETURNS trigger AS $$
BEGIN
  UPDATE memories
     SET status = 'superseded',
         valid_to = COALESCE(valid_to, CURRENT_DATE)
   WHERE id = NEW.old_id AND status <> 'superseded';
  RETURN NEW;
END $$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS supersession_apply ON supersessions;
CREATE TRIGGER supersession_apply
  AFTER INSERT ON supersessions
  FOR EACH ROW EXECUTE FUNCTION apply_supersession();

-- ---------------------------------------------------------------
-- 3.4 Entities (Authority Office, minimal; never auto-merge)
-- ---------------------------------------------------------------
CREATE TABLE IF NOT EXISTS entities (
  id         TEXT PRIMARY KEY,
  kind       TEXT NOT NULL CHECK (kind IN ('person','org','tool','repo','location')),
  label      TEXT NOT NULL,
  aliases    TEXT[] NOT NULL DEFAULT '{}',
  do_not_merge_with TEXT[] NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS memory_entities (
  memory_id TEXT NOT NULL REFERENCES memories(id),
  entity_id TEXT NOT NULL REFERENCES entities(id),
  PRIMARY KEY (memory_id, entity_id)
);

-- ---------------------------------------------------------------
-- 3.5 Disposable indexes (embeddings; FTS is generated above)
-- ---------------------------------------------------------------
CREATE TABLE IF NOT EXISTS mem_embeddings (
  memory_id TEXT PRIMARY KEY REFERENCES memories(id),
  model     TEXT NOT NULL,
  embedding vector(1024) NOT NULL
);
CREATE INDEX IF NOT EXISTS mem_embeddings_hnsw
  ON mem_embeddings USING hnsw (embedding vector_cosine_ops);

-- ---------------------------------------------------------------
-- 3.6 Instrumentation (schema, not afterthought)
-- ---------------------------------------------------------------
CREATE TABLE IF NOT EXISTS recall_log (
  id          BIGSERIAL PRIMARY KEY,
  ts          TIMESTAMPTZ NOT NULL DEFAULT now(),
  session_id  TEXT,
  project     TEXT,
  query_text  TEXT,
  packet      JSONB,
  latency_ms  INTEGER,
  served      BOOLEAN NOT NULL DEFAULT TRUE
);

CREATE TABLE IF NOT EXISTS miss_signals (
  id         BIGSERIAL PRIMARY KEY,
  ts         TIMESTAMPTZ NOT NULL DEFAULT now(),
  session_id TEXT,
  signal     TEXT NOT NULL CHECK (signal IN
               ('user_said_forgot','user_resupplied','answer_existed','manual')),
  detail     JSONB NOT NULL DEFAULT '{}'
);

-- Review queue (chaperone holds + entity resolution + contradictions)
CREATE TABLE IF NOT EXISTS review_queue (
  id         BIGSERIAL PRIMARY KEY,
  ts         TIMESTAMPTZ NOT NULL DEFAULT now(),
  kind       TEXT NOT NULL CHECK (kind IN
               ('chaperone_hold','contradiction','entity_pending','promotion_request')),
  memory_id  TEXT REFERENCES memories(id),
  detail     JSONB NOT NULL DEFAULT '{}',
  resolved   BOOLEAN NOT NULL DEFAULT FALSE,
  resolution TEXT
);

COMMIT;
