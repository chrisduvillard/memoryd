-- memoryd migration 004: quarantine ledger events (extract.py no longer
-- mislabels quarantines as promotions)
BEGIN;

ALTER TABLE events DROP CONSTRAINT IF EXISTS events_kind_check;
ALTER TABLE events ADD CONSTRAINT events_kind_check CHECK (kind IN (
  'user_message','agent_response','tool_call','tool_result',
  'session_start','session_end','recall_packet','capture_ack',
  'promotion','supersession','veto','miss_signal','extraction_run',
  'external_note','delegation','quarantine'));

COMMIT;
