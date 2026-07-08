#!/usr/bin/env bash
# UserPromptSubmit hook: deterministic recall. Thin by design — no logic here.
# Fail-open with a VISIBLE marker (spec P9): silent degradation is forbidden.
set -u
PORT="${MEMORYD_PORT:-7437}"
INPUT="$(cat)"

PROMPT=$(printf '%s' "$INPUT" | jq -r '.prompt // ""')
SESSION=$(printf '%s' "$INPUT" | jq -r '.session_id // "unknown"')
CWD=$(printf '%s' "$INPUT" | jq -r '.cwd // ""')
PROJECT=$(basename "$CWD" 2>/dev/null || echo "")

RESP=$(jq -n --arg p "$PROMPT" --arg s "$SESSION" --arg pr "$PROJECT" \
        '{prompt:$p, session_id:$s, project:$pr}' \
      | curl -s --max-time 1.5 -X POST "http://127.0.0.1:${PORT}/recall" \
             -H 'Content-Type: application/json' -d @- ) || RESP=""

MD=$(printf '%s' "$RESP" | jq -r '.markdown // empty' 2>/dev/null)

if [ -n "$MD" ]; then
  jq -n --arg ctx "$MD" \
    '{hookSpecificOutput:{hookEventName:"UserPromptSubmit", additionalContext:$ctx}}'
elif [ -n "$RESP" ]; then
  # daemon answered but had nothing relevant: stay silent, that is fine
  exit 0
else
  jq -n '{hookSpecificOutput:{hookEventName:"UserPromptSubmit",
          additionalContext:"[memory: unavailable this turn — proceeding without recall]"}}'
fi
exit 0
