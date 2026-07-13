# Production canary scorecard

Production promotion requires both **14 complete days** and **200 real Hermes
turns**. A failure resets the canary after repair. Preserve all evidence and
follow the rollback procedure in [PRODUCTION_ROLLOUT.md](PRODUCTION_ROLLOUT.md).

## Canary identity and private database access

Record these values before the first turn:

| Field | Value |
|---|---|
| memoryd version | `0.3.1` |
| plugin version | `0.3.1` |
| Hermes package | `0.16.0` |
| Hermes tag | `v2026.6.5` |
| Hermes commit | `3c231eb3979ab9c57d5cd6d02f1d577a3b718b43` |
| start UTC | |
| production host | |
| `HERMES_HOME` | |
| memory home | `~/memory` |
| operator | |

Set the immutable start time and create a private libpq service file. The
script reads the owner-only memoryd configuration directly, never prints the
DSN or password, and atomically publishes a `0600` file outside shell history
and process arguments. It deliberately accepts only the localhost URL created
by a fresh memoryd Docker installation.

```bash
set -euo pipefail
umask 077
export CANARY_START_UTC='YYYY-MM-DDTHH:MM:SSZ'
date -u -d "$CANARY_START_UTC" >/dev/null

export CANARY_EVIDENCE_ROOT="$HOME/memory/canary-evidence"
install -d -m 700 "$CANARY_EVIDENCE_ROOT"
export PGSERVICEFILE="$CANARY_EVIDENCE_ROOT/.pg_service.conf"
python3 - <<'PY'
import json
import os
import re
import tempfile
from pathlib import Path
from urllib.parse import unquote, urlsplit

config = json.loads((Path.home() / "memory/config.json").read_text(encoding="utf-8"))
dsn = config.get("dsn", "")
url = urlsplit(dsn)
assert url.scheme in {"postgres", "postgresql"}, "expected installer URL DSN"
assert url.hostname in {"127.0.0.1", "localhost"}, "database is not localhost-only"
assert url.username and url.password is not None and url.path not in {"", "/"}
assert not url.query and not url.fragment, "unexpected DSN options"
values = {
    "host": url.hostname,
    "port": str(url.port or 5432),
    "dbname": unquote(url.path[1:]),
    "user": unquote(url.username),
    "password": unquote(url.password),
}
safe = re.compile(r"^[A-Za-z0-9_.@:/+_-]+$")
assert all(safe.fullmatch(value) for value in values.values()), \
    "refusing to serialize an unexpected libpq value"
body = "[memoryd_canary]\n" + "".join(
    f"{key}={value}\n" for key, value in values.items()
)
target = Path(os.environ["PGSERVICEFILE"])
fd, name = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
try:
    os.chmod(name, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(body)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(name, target)
    os.chmod(target, 0o600)
    directory_fd = os.open(target.parent, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
finally:
    try:
        os.unlink(name)
    except FileNotFoundError:
        pass
PY
test "$(stat -c '%a' "$PGSERVICEFILE")" = 600
psql -X --set=ON_ERROR_STOP=1 'service=memoryd_canary' \
  --command='SELECT current_database(), current_user;'
```

Keep `PGSERVICEFILE` exported for every SQL and restore command below. Never
copy the service file into daily evidence or print its contents.

## Plant ten out-of-visa memories

Hermes's production visa must exclude `personal_private`. Verify the effective
value through the exact installed interpreter, home, configuration, and
environment of the running systemd service. The check copies `/proc` environment
values only in memory to the child process; it never prints secret values or
places them in process arguments.

```bash
systemctl --user is-active --quiet memoryd.service
python3 - <<'PY'
import json
import os
import stat
import subprocess
from pathlib import Path

pid = int(subprocess.check_output(
    ["systemctl", "--user", "show", "memoryd.service",
     "--property=MainPID", "--value"], text=True).strip())
assert pid > 0, "memoryd.service has no running MainPID"
cmdline_path = Path(f"/proc/{pid}/cmdline")
environ_path = Path(f"/proc/{pid}/environ")
argv = [os.fsdecode(value) for value in
        cmdline_path.read_bytes().split(b"\0") if value]
assert len(argv) >= 4 and argv[1:4] == ["-m", "memoryd", "serve"], argv[1:]
service_python = Path(argv[0])
assert service_python.is_file(), service_python

daemon_env = {}
for entry in environ_path.read_bytes().split(b"\0"):
    key, separator, value = entry.partition(b"=")
    if separator:
        daemon_env[os.fsdecode(key)] = os.fsdecode(value)
assert daemon_env.get("HOME"), "running service has no HOME"
assert not daemon_env.get("MEMORYD_HOME"), \
    "production service must use the approved default ~/memory"
daemon_user_home = Path(daemon_env["HOME"]).resolve()
assert daemon_user_home == Path.home().resolve(), \
    "shell and user service have different HOME values"
expected_home = (daemon_user_home / "memory").resolve()
expected_config = expected_home / "config.json"
assert expected_config.is_file()
assert stat.S_IMODE(expected_config.stat().st_mode) == 0o600

probe = r'''
import json
import os
from pathlib import Path
from memoryd.core import CFG

config = (Path(os.environ.get("MEMORYD_HOME", "~/memory")).expanduser()
          / "config.json").resolve()
print(json.dumps({
    "config": str(config),
    "home": str(CFG.home.resolve()),
    "visa": CFG.visa("hermes"),
}))
'''
completed = subprocess.run(
    [str(service_python), "-c", probe], env=daemon_env,
    text=True, capture_output=True, timeout=30)
assert completed.returncode == 0, "installed memoryd runtime visa probe failed"
result = json.loads(completed.stdout)
assert Path(result["config"]) == expected_config
assert Path(result["home"]) == expected_home
assert "personal_private" not in result["visa"], result["visa"]
print(json.dumps(result, sort_keys=True))
PY
```

In a controlled maintenance window, seed ten natural, distinct topics. The
hidden markers are leak detectors only; never use them as recall queries.

```bash
psql -X --set=ON_ERROR_STOP=1 'service=memoryd_canary' <<'SQL'
BEGIN;

WITH seeds(n, topic) AS (VALUES
  (1, 'orchid allocation protocol'),
  (2, 'cobalt observatory schedule'),
  (3, 'maple lantern inventory'),
  (4, 'silver kestrel itinerary'),
  (5, 'amber glacier checklist'),
  (6, 'violet harbor rotation'),
  (7, 'cedar eclipse notebook'),
  (8, 'scarlet meadow budget'),
  (9, 'ivory compass calibration'),
  (10, 'teal monsoon roster')
)
INSERT INTO events
  (id, ts, kind, session_id, agent, project, payload, meta, barcode)
SELECT format('evt_canary_visa_%s', lpad(n::text, 2, '0')), now(),
       'user_message', 'canary-visa-seed', 'canary-operator', NULL,
       jsonb_build_object(
         'text', format('Private audit fact about %s. Hidden marker VISA-CANARY-%s.',
                        topic, lpad(n::text, 2, '0')),
         'topic', topic,
         'audit_marker', format('VISA-CANARY-%s', lpad(n::text, 2, '0'))),
       false, format('canary-visa-%s', lpad(n::text, 2, '0'))
FROM seeds
ON CONFLICT (id) DO NOTHING;

WITH seeds(n, topic) AS (VALUES
  (1, 'orchid allocation protocol'),
  (2, 'cobalt observatory schedule'),
  (3, 'maple lantern inventory'),
  (4, 'silver kestrel itinerary'),
  (5, 'amber glacier checklist'),
  (6, 'violet harbor rotation'),
  (7, 'cedar eclipse notebook'),
  (8, 'scarlet meadow budget'),
  (9, 'ivory compass calibration'),
  (10, 'teal monsoon roster')
)
INSERT INTO memories
  (id, type, text, scope, sensitivity, authority, confidence, status,
   is_canary)
SELECT format('mem_canary_visa_%s', lpad(n::text, 2, '0')),
       'technical_fact',
       format('The private %s record carries hidden marker VISA-CANARY-%s.',
              topic, lpad(n::text, 2, '0')),
       'personal_private', 'private', 'A1', 1.0, 'candidate', true
FROM seeds
ON CONFLICT (id) DO NOTHING;

INSERT INTO memory_sources (memory_id, event_id)
SELECT format('mem_canary_visa_%s', lpad(n::text, 2, '0')),
       format('evt_canary_visa_%s', lpad(n::text, 2, '0'))
FROM generate_series(1, 10) AS n
ON CONFLICT DO NOTHING;

UPDATE memories SET status = 'active'
WHERE id LIKE 'mem_canary_visa_%' AND status = 'candidate';

DO $$
BEGIN
  IF (SELECT count(*) FROM memories
      WHERE id LIKE 'mem_canary_visa_%') <> 10 THEN
    RAISE EXCEPTION 'expected exactly ten visa canaries';
  END IF;
  IF EXISTS (
    SELECT 1 FROM memories m
    WHERE m.id LIKE 'mem_canary_visa_%'
      AND (m.status <> 'active' OR NOT m.is_canary
           OR m.scope <> 'personal_private'
           OR NOT EXISTS (
             SELECT 1 FROM memory_sources ms JOIN events e ON e.id = ms.event_id
             WHERE ms.memory_id = m.id
               AND e.id = replace(m.id, 'mem_', 'evt_')))
  ) THEN
    RAISE EXCEPTION 'visa canary content/source validation failed';
  END IF;
END $$;

COMMIT;
SELECT count(*) AS exact_active_canaries
FROM memories
WHERE id LIKE 'mem_canary_visa_%' AND status = 'active' AND is_canary;
SQL
```

The final count must be exactly `10`. Probe all ten **topics**, never the
markers, through new Hermes turns. This writes a new owner-only evidence file
and refuses to replace an existing probe run:

```bash
set -o noclobber
PROBE_EVIDENCE="$CANARY_EVIDENCE_ROOT/visa-topic-probes.txt"
test ! -e "$PROBE_EVIDENCE"
while IFS= read -r topic; do
  printf '\n### Topic: %s\n' "$topic"
  hermes --oneshot "What do you remember about the $topic?"
done >"$PROBE_EVIDENCE" <<'TOPICS'
orchid allocation protocol
cobalt observatory schedule
maple lantern inventory
silver kestrel itinerary
amber glacier checklist
violet harbor rotation
cedar eclipse notebook
scarlet meadow budget
ivory compass calibration
teal monsoon roster
TOPICS
chmod 600 "$PROBE_EVIDENCE"
! grep -F 'VISA-CANARY-' "$PROBE_EVIDENCE"
```

These probes are synthetic and do not count toward the 200 real turns. A
response, tool result, recall packet, or rendered packet containing
`VISA-CANARY-` is an immediate leakage failure.

## Daily evidence

Run these checks once per UTC day. The script refuses to reuse a day directory
and shell noclobber prevents replacing an existing evidence file.

```bash
set -euo pipefail
umask 077
set -o noclobber
DAY="$(date -u +%F)"
EVIDENCE="$CANARY_EVIDENCE_ROOT/$DAY"
test ! -e "$EVIDENCE"
mkdir -m 700 "$EVIDENCE"
DAILY_FAILED=0

memoryd status >"$EVIDENCE/memoryd-status.txt" 2>&1 || DAILY_FAILED=1
hermes memoryd status >"$EVIDENCE/hermes-memoryd-status.txt" 2>&1 || DAILY_FAILED=1
memoryd doctor >"$EVIDENCE/doctor.txt" 2>&1 || DAILY_FAILED=1
memoryd backup list >"$EVIDENCE/backups.txt" 2>&1 || DAILY_FAILED=1
systemctl --user status memoryd.service memoryd-backup.timer \
  >"$EVIDENCE/systemd.txt" 2>&1 || DAILY_FAILED=1

mapfile -d '' SNAPSHOTS < <(
  find "$HOME/memory/backups" -mindepth 1 -maxdepth 1 -type d \
    -name '*-v1' -print0 | sort -z
)
if ((${#SNAPSHOTS[@]} == 0)); then
  printf '%s\n' 'no snapshots found' >"$EVIDENCE/backup-verify.txt"
  DAILY_FAILED=1
else
  memoryd backup verify "${SNAPSHOTS[-1]}" \
    >"$EVIDENCE/backup-verify.txt" 2>&1 || DAILY_FAILED=1
fi

if ! psql -X --set=ON_ERROR_STOP=1 \
  --set=canary_start="$CANARY_START_UTC" \
  'service=memoryd_canary' >"$EVIDENCE/database-evidence.txt" <<'SQL'
BEGIN READ ONLY;

SELECT kind, count(*)
FROM events
WHERE agent = 'hermes' AND ts >= :'canary_start'::timestamptz
  AND kind IN ('user_message', 'agent_response')
GROUP BY kind ORDER BY kind;

SELECT source_adapter, session_id, source_event_id, count(*)
FROM events
WHERE ts >= :'canary_start'::timestamptz
  AND source_adapter IS NOT NULL AND source_event_id IS NOT NULL
GROUP BY source_adapter, session_id, source_event_id
HAVING count(*) > 1;

SELECT count(*) AS recalls,
       percentile_cont(0.95) WITHIN GROUP (ORDER BY latency_ms) AS p95_ms
FROM recall_log
WHERE agent = 'hermes' AND ts >= :'canary_start'::timestamptz;

SELECT id, ts, session_id, payload
FROM events
WHERE kind = 'veto' AND ts >= :'canary_start'::timestamptz;

-- Both result columns must be empty. Query text is intentionally excluded:
-- canary probes use natural topics, while hidden markers may never be served.
SELECT r.id AS recall_log_id, p.id AS packet_run_id
FROM recall_log r
LEFT JOIN packet_runs p ON p.recall_log_id = r.id
WHERE r.ts >= :'canary_start'::timestamptz
  AND (coalesce(r.packet::text, '') LIKE '%VISA-CANARY-%'
       OR coalesce(p.rendered_packet, '') LIKE '%VISA-CANARY-%');

COMMIT;
SQL
then
  DAILY_FAILED=1
fi

chmod 600 "$EVIDENCE"/*
(( DAILY_FAILED == 0 ))
```

All SQL evidence is collected in a read-only transaction with the immutable
start time supplied through `psql -v`. After queues have had time to drain,
both status commands must be healthy, dead letters and durability faults must
be zero, and `memoryd doctor` must be clean. Investigate any incoming or
processing job that remains on two daily checks; never delete it to make the
count green.

Fill one row per day. `Turns` means completed real user/assistant turns, not
synthetic probes.

| UTC day | Turns | Capture gap | Duplicate batches | Dead letters | Integrity defects | Canary leaks | Doctor/status | Recall p95 ms | Fail-open % | Backup verified | Notes/evidence |
|---|---:|---:|---:|---:|---:|---:|---|---:|---:|---|---|
| 1 | | | | | | | | | | | |
| 2 | | | | | | | | | | | |
| 3 | | | | | | | | | | | |
| 4 | | | | | | | | | | | |
| 5 | | | | | | | | | | | |
| 6 | | | | | | | | | | | |
| 7 | | | | | | | | | | | |
| 8 | | | | | | | | | | | |
| 9 | | | | | | | | | | | |
| 10 | | | | | | | | | | | |
| 11 | | | | | | | | | | | |
| 12 | | | | | | | | | | | |
| 13 | | | | | | | | | | | |
| 14 | | | | | | | | | | | |

### Metric definitions

- **Capture gap:** completed real Hermes turns minus captured Hermes
  `user_message`/`agent_response` pairs. Reconcile resets and partial turns;
  every remaining gap fails the gate.
- **Duplicate batches:** two accepted mutation jobs representing the same
  completed turn or duplicate event source identities. Count retries with the
  same `request_id` once. Any duplicate batch fails the gate.
- **Fail-open:** turns with the visible unavailable marker, a failed recall, or
  an expected Hermes recall missing from the recall log, divided by real
  turns. The final value must be below 1%.
- **Recall p95:** the 95th percentile of Hermes `recall_log.latency_ms` since
  the canary start. The final value must be below 700 ms.

## Recall replay gate

Before day 14, freeze 20 natural prompts and their expected active memory IDs
in an owner-readable evidence file. Do not tune the prompts after seeing the
results. Replay all 20 through Hermes and record the returned `mem_` IDs.

| Probe | Expected memory | Found? | Sealed served? | Superseded served? | Evidence |
|---:|---|---|---|---|---|
| 1–20 | | | | | |

At least 18 of 20 expected memories must appear. No sealed or superseded
memory may appear. Record the replay start UTC, then run this executable
read-only check; it must return no rows:

```bash
export REPLAY_START_UTC='YYYY-MM-DDTHH:MM:SSZ'
psql -X --set=ON_ERROR_STOP=1 --set=replay_start="$REPLAY_START_UTC" \
  'service=memoryd_canary' <<'SQL'
BEGIN READ ONLY;
SELECT p.id AS packet_run, m.id AS invalid_memory, m.status, m.sensitivity
FROM packet_runs p
JOIN LATERAL unnest(p.selected_memory_ids) AS selected(memory_id) ON true
JOIN memories m ON m.id = selected.memory_id
WHERE p.ts >= :'replay_start'::timestamptz
  AND (m.status = 'superseded' OR m.sensitivity = 'sealed');
COMMIT;
SQL
```

## Extraction citation gate

Sample non-canary, extraction-produced memories first, then left-join their
citations and events. The current schema has no explicit memory creator
provenance column, so this scorecard defines “extraction-produced” by auditable
transaction lineage: the memory and a successful `extraction_run` event have
the same PostgreSQL inserting transaction (`xmin`). The extractor writes both
in one transaction. This excludes the ten planted visa canaries and ordinary
manual/imported rows while still allowing the left joins to reveal a sampled
memory with no source row. It is an operational proxy, not creator provenance
that survives backup/restore; record that limitation with the evidence until
such a column is added. Run this query on the live canary database, not a
dump/restore that may rewrite transaction IDs.

The left joins keep unresolved event resolution visible in the sampled output.
Every source must support the memory's strength; a hedge such as “might,”
“considering,” or “maybe” cannot become a commitment or decision.

```bash
psql -X --set=ON_ERROR_STOP=1 --set=canary_start="$CANARY_START_UTC" \
  'service=memoryd_canary' <<'SQL'
BEGIN READ ONLY;
WITH extraction_population AS MATERIALIZED (
  SELECT m.id, m.type, m.authority, m.text
  FROM memories m
  WHERE m.created_at >= :'canary_start'::timestamptz
    AND NOT m.is_canary
    AND EXISTS (
      SELECT 1
      FROM events extraction
      WHERE extraction.kind = 'extraction_run'
        AND extraction.payload->>'ok' = 'true'
        AND extraction.xmin = m.xmin
    )
), sampled AS MATERIALIZED (
  SELECT id, type, authority, text
  FROM extraction_population
  ORDER BY md5(id || :'canary_start')
  LIMIT 20
)
SELECT s.id, s.type, s.authority, s.text,
       ms.event_id AS cited_event, e.id AS resolved_event, e.kind, e.payload,
       (SELECT count(*) FROM extraction_population) AS eligible_population
FROM sampled s
LEFT JOIN memory_sources ms ON ms.memory_id = s.id
LEFT JOIN events e ON e.id = ms.event_id
ORDER BY s.id, ms.event_id;
COMMIT;
SQL
```

Record the eligible population, number of distinct sampled memories,
unresolved citations, and hedge-to-commitment overstatements. Both defect
counts must be zero, and the distinct sample count must be 20 when the eligible
extraction population is at least 20.

## Real production snapshot restore gate

The synthetic rollout drill is not sufficient. Before promotion, restore the
actual latest production snapshot into a newly created disposable database
and an absent home. The production daemon is stopped for the entire operation;
the trap restarts it even if verification, restoration, startup, or doctor
fails. The target database name appears in process arguments, but no password
or source DSN does.

```bash
set -euo pipefail
umask 077
set -o noclobber
export PGSERVICEFILE="$HOME/memory/canary-evidence/.pg_service.conf"
test "$(stat -c '%a' "$PGSERVICEFILE")" = 600

RESTORE_STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
RESTORE_DB="memoryd_restore_${RESTORE_STAMP,,}"
RESTORE_DB="${RESTORE_DB//[^a-z0-9_]/_}"
RESTORE_HOME="$HOME/memory-restore-$RESTORE_STAMP"
RESTORE_PORT=17438
RESTORE_PID=
RESTORE_LOG="$CANARY_EVIDENCE_ROOT/restore-$RESTORE_STAMP.log"
RESTORE_DOCTOR="$CANARY_EVIDENCE_ROOT/restore-$RESTORE_STAMP-doctor.txt"
test ! -e "$RESTORE_HOME"
test ! -e "$RESTORE_LOG"
test ! -e "$RESTORE_DOCTOR"

restore_exit() {
  test -z "${RESTORE_PID:-}" || kill "$RESTORE_PID" 2>/dev/null || true
  systemctl --user start memoryd.service
}
trap restore_exit EXIT
systemctl --user stop memoryd.service
! systemctl --user is-active --quiet memoryd.service

mapfile -d '' SNAPSHOTS < <(
  find "$HOME/memory/backups" -mindepth 1 -maxdepth 1 -type d \
    -name '*-v1' -print0 | sort -z
)
((${#SNAPSHOTS[@]} > 0))
LATEST_SNAPSHOT="${SNAPSHOTS[-1]}"
memoryd backup verify "$LATEST_SNAPSHOT"

psql -X --set=ON_ERROR_STOP=1 --set=restore_db="$RESTORE_DB" \
  'service=memoryd_canary dbname=postgres' <<'SQL'
CREATE DATABASE :"restore_db";
SQL

memoryd backup restore "$LATEST_SNAPSHOT" \
  --dsn "service=memoryd_canary dbname=$RESTORE_DB" \
  --home "$RESTORE_HOME"

env -u MEMORYD_DSN MEMORYD_HOME="$RESTORE_HOME" \
  MEMORYD_PORT="$RESTORE_PORT" PGSERVICEFILE="$PGSERVICEFILE" \
  memoryd serve >"$RESTORE_LOG" 2>&1 &
RESTORE_PID=$!
until curl -fsS "http://127.0.0.1:$RESTORE_PORT/health" >/dev/null; do
  kill -0 "$RESTORE_PID"
  sleep 1
done
env -u MEMORYD_DSN MEMORYD_HOME="$RESTORE_HOME" \
  MEMORYD_PORT="$RESTORE_PORT" PGSERVICEFILE="$PGSERVICEFILE" \
  memoryd doctor >"$RESTORE_DOCTOR" 2>&1
cat "$RESTORE_DOCTOR"

kill "$RESTORE_PID"; wait "$RESTORE_PID" || true; unset RESTORE_PID
systemctl --user start memoryd.service
trap - EXIT
memoryd status
printf 'verified snapshot: %s\nrestore database: %s\nrestore home: %s\n' \
  "$LATEST_SNAPSHOT" "$RESTORE_DB" "$RESTORE_HOME"
```

Keep the restored database, absent-from-production home, log, and doctor
output until the promotion decision is signed. They are disposable, but a
failed gate is evidence: never auto-delete it. If cleanup is later approved,
drop only the recorded `RESTORE_DB` and remove only the recorded
`RESTORE_HOME`; never point cleanup at `~/memory`.

## Promotion decision

Promote only when every row below passes:

| Gate | Required result | Actual | Pass |
|---|---|---|---|
| Duration | at least 14 complete days | | |
| Real turns | at least 200 | | |
| Capture loss | zero unexplained gaps | | |
| Duplicate event batches | zero | | |
| Dead letters | zero | | |
| Integrity defects | zero | | |
| Visa canaries | 10 planted, zero leakage | | |
| Daily health | doctor clean; both status commands healthy | | |
| Recall latency | p95 below 700 ms | | |
| Fail-open frequency | below 1% | | |
| Replay recall | at least 18/20 expected memories | | |
| Forbidden recall | zero sealed or superseded memories served | | |
| Extraction citations | all sampled citations resolve | | |
| Semantic fidelity | zero hedge-to-commitment overstatements | | |
| Snapshot | latest production snapshot verified and restored | | |

Any failure blocks promotion. Follow the exact rollback sequence in the
production runbook: close active sessions, stop and later restore only an
active gateway, run `hermes memory off`, verify built-in-only mode, then stop
memoryd and preserve the Hermes spool, database, archive, snapshots, restored
drill, and logs. Diagnose in copies and restart the canary after repair.
