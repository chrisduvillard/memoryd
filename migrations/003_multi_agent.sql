-- memoryd migration 003: multi-agent support (Hermes integration)
BEGIN;

-- new event kinds: external memory-tool writes, subagent delegations
ALTER TABLE events DROP CONSTRAINT IF EXISTS events_kind_check;
ALTER TABLE events ADD CONSTRAINT events_kind_check CHECK (kind IN (
  'user_message','agent_response','tool_call','tool_result',
  'session_start','session_end','recall_packet','capture_ack',
  'promotion','supersession','veto','miss_signal','extraction_run',
  'external_note','delegation'));

-- recall log gains agent attribution (per-agent visa auditing)
ALTER TABLE recall_log ADD COLUMN IF NOT EXISTS agent TEXT;

COMMIT;
