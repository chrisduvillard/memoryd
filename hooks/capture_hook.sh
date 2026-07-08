#!/usr/bin/env bash
# Stop / SessionEnd / PreCompact hook: fire-and-forget capture.
# If the daemon is down, spool locally; daemon drains on start (spec §4.4).
set -u
TRIGGER="${1:-unknown}"
PORT="${MEMORYD_PORT:-7437}"
SPOOL="${MEMORYD_HOME:-$HOME/memory}/spool"
INPUT="$(cat)"

TRANSCRIPT=$(printf '%s' "$INPUT" | jq -r '.transcript_path // ""')
SESSION=$(printf '%s' "$INPUT" | jq -r '.session_id // "unknown"')
CWD=$(printf '%s' "$INPUT" | jq -r '.cwd // ""')
PROJECT=$(basename "$CWD" 2>/dev/null || echo "")

[ -z "$TRANSCRIPT" ] && exit 0

PAYLOAD=$(jq -n --arg t "$TRANSCRIPT" --arg s "$SESSION" --arg pr "$PROJECT" --arg tr "$TRIGGER" \
  '{transcript_path:$t, session_id:$s, project:$pr, trigger:$tr}')

if ! printf '%s' "$PAYLOAD" \
     | curl -s --max-time 2 -o /dev/null -f -X POST "http://127.0.0.1:${PORT}/capture" \
            -H 'Content-Type: application/json' -d @- ; then
  mkdir -p "$SPOOL"
  printf '%s' "$PAYLOAD" > "$SPOOL/$(date +%s%N).json"
fi
exit 0
