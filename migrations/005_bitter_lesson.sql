-- memoryd migration 005: Bitter-Lesson extension points.
--
-- Keep existing memory IDs and behavior, but move human-designed vocabularies
-- and policies out of database walls and into versioned registries/evidence.
BEGIN;

-- Event kinds become registry-governed text instead of a closed check list.
ALTER TABLE events DROP CONSTRAINT IF EXISTS events_kind_check;

-- Memory types become open text. Existing values survive unchanged.
ALTER TABLE memories ALTER COLUMN type TYPE TEXT USING type::text;
ALTER TABLE memories ADD COLUMN IF NOT EXISTS type_namespace TEXT NOT NULL DEFAULT 'core';
ALTER TABLE memories ADD COLUMN IF NOT EXISTS type_key TEXT;
UPDATE memories SET type_key = type WHERE type_key IS NULL;
ALTER TABLE memories ALTER COLUMN type_key SET DEFAULT 'technical_fact';
ALTER TABLE memories ALTER COLUMN type_key SET NOT NULL;

CREATE TABLE IF NOT EXISTS memory_type_registry (
  namespace   TEXT NOT NULL DEFAULT 'core',
  type_key    TEXT NOT NULL,
  title       TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  schema      JSONB NOT NULL DEFAULT '{}',
  builtin     BOOLEAN NOT NULL DEFAULT FALSE,
  active      BOOLEAN NOT NULL DEFAULT TRUE,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (namespace, type_key)
);

CREATE TABLE IF NOT EXISTS event_type_registry (
  namespace   TEXT NOT NULL DEFAULT 'core',
  event_type  TEXT NOT NULL,
  title       TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  schema      JSONB NOT NULL DEFAULT '{}',
  builtin     BOOLEAN NOT NULL DEFAULT FALSE,
  active      BOOLEAN NOT NULL DEFAULT TRUE,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (namespace, event_type)
);

CREATE TABLE IF NOT EXISTS model_runs (
  id            TEXT PRIMARY KEY,
  ts            TIMESTAMPTZ NOT NULL DEFAULT now(),
  profile       TEXT NOT NULL,
  provider      TEXT NOT NULL,
  model         TEXT NOT NULL,
  operation     TEXT NOT NULL,
  contract      TEXT,
  prompt_chars  INTEGER NOT NULL DEFAULT 0,
  output_chars  INTEGER NOT NULL DEFAULT 0,
  input_tokens  INTEGER,
  output_tokens INTEGER,
  cost_usd      NUMERIC,
  ok            BOOLEAN NOT NULL DEFAULT TRUE,
  error         TEXT,
  metadata      JSONB NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS policy_runs (
  id          TEXT PRIMARY KEY,
  ts          TIMESTAMPTZ NOT NULL DEFAULT now(),
  policy      TEXT NOT NULL,
  operation   TEXT NOT NULL,
  input       JSONB NOT NULL DEFAULT '{}',
  output      JSONB NOT NULL DEFAULT '{}',
  latency_ms  INTEGER,
  ok          BOOLEAN NOT NULL DEFAULT TRUE,
  error       TEXT
);

CREATE TABLE IF NOT EXISTS eval_cases (
  id          TEXT PRIMARY KEY,
  name        TEXT NOT NULL,
  kind        TEXT NOT NULL,
  input       JSONB NOT NULL DEFAULT '{}',
  expected    JSONB NOT NULL DEFAULT '{}',
  enabled     BOOLEAN NOT NULL DEFAULT TRUE,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS eval_runs (
  id          TEXT PRIMARY KEY,
  ts          TIMESTAMPTZ NOT NULL DEFAULT now(),
  profile     TEXT NOT NULL,
  status      TEXT NOT NULL,
  summary     JSONB NOT NULL DEFAULT '{}',
  metrics     JSONB NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS packet_runs (
  id                  TEXT PRIMARY KEY,
  ts                  TIMESTAMPTZ NOT NULL DEFAULT now(),
  recall_log_id       BIGINT REFERENCES recall_log(id),
  session_id          TEXT,
  project             TEXT,
  agent               TEXT,
  policy              TEXT NOT NULL,
  compiler            TEXT NOT NULL,
  rendered_packet     TEXT NOT NULL DEFAULT '',
  selected_memory_ids TEXT[] NOT NULL DEFAULT '{}',
  rejected            JSONB NOT NULL DEFAULT '[]',
  channels            TEXT[] NOT NULL DEFAULT '{}',
  latency_ms          INTEGER
);

INSERT INTO memory_type_registry (namespace, type_key, title, description, builtin)
VALUES
  ('core','identity','Identity','Stable identity information.', TRUE),
  ('core','preference','Preference','User preference or tendency.', TRUE),
  ('core','writing_style','Writing Style','Reusable writing-style guidance.', TRUE),
  ('core','project_state','Project State','Current or recent project state.', TRUE),
  ('core','decision','Decision','A decision and its rationale.', TRUE),
  ('core','open_question','Open Question','A question still awaiting resolution.', TRUE),
  ('core','commitment','Commitment','A promise or follow-up obligation.', TRUE),
  ('core','person','Person','Person-related memory.', TRUE),
  ('core','company','Company','Organization-related memory.', TRUE),
  ('core','technical_fact','Technical Fact','Fact about systems, code, or tools.', TRUE),
  ('core','workflow','Workflow','Reusable procedure or workflow.', TRUE),
  ('core','constraint','Constraint','Hard project or user constraint.', TRUE),
  ('core','procedure','Procedure','Stepwise procedure.', TRUE),
  ('core','directive','Directive','Explicit instruction with authority.', TRUE),
  ('core','warning','Warning','Risk, failed attempt, or caution.', TRUE),
  ('core','priming','Priming','Short-lived session context.', TRUE)
ON CONFLICT (namespace, type_key) DO NOTHING;

INSERT INTO event_type_registry (namespace, event_type, title, description, builtin)
VALUES
  ('core','user_message','User Message','User-authored turn content.', TRUE),
  ('core','agent_response','Agent Response','Assistant-authored turn content.', TRUE),
  ('core','tool_call','Tool Call','Tool invocation summary.', TRUE),
  ('core','tool_result','Tool Result','Tool result summary.', TRUE),
  ('core','session_start','Session Start','Session boundary start.', TRUE),
  ('core','session_end','Session End','Session boundary end.', TRUE),
  ('core','recall_packet','Recall Packet','Injected memory packet metadata.', TRUE),
  ('core','capture_ack','Capture Ack','Capture trigger acknowledgement.', TRUE),
  ('core','promotion','Promotion','Memory promotion event.', TRUE),
  ('core','supersession','Supersession','Memory supersession event.', TRUE),
  ('core','veto','Veto','Canary or blocking warning event.', TRUE),
  ('core','miss_signal','Miss Signal','User or tool reported retrieval miss.', TRUE),
  ('core','extraction_run','Extraction Run','Extractor run outcome.', TRUE),
  ('core','external_note','External Note','Adapter-supplied note or memory write.', TRUE),
  ('core','delegation','Delegation','Parent-observed subagent delegation.', TRUE),
  ('core','quarantine','Quarantine','Quarantine decision event.', TRUE)
ON CONFLICT (namespace, event_type) DO NOTHING;

COMMIT;
