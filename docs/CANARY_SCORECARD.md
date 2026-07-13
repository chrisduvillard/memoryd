# Production canary scorecard

Production promotion requires both **14 complete days** and **200 real Hermes
turns**. A failure resets the canary after repair. Preserve all evidence and
follow the rollback procedure in [PRODUCTION_ROLLOUT.md](PRODUCTION_ROLLOUT.md).

## Canary identity

Record these values before the first turn:

| Field | Value |
|---|---|
| memoryd version | `0.3.0` |
| plugin version | `0.3.0` |
| Hermes package | `0.16.0` |
| Hermes tag | `v2026.6.5` |
| Hermes commit | `3c231eb3979ab9c57d5cd6d02f1d577a3b718b43` |
| start UTC | |
| production host | |
| `HERMES_HOME` | |
| memory home | |
| operator | |

Set an immutable start time for the read-only metrics below:

```bash
export CANARY_START_UTC='YYYY-MM-DDTHH:MM:SSZ'
mkdir -m 700 -p "$HOME/memory/canary-evidence"
```

## Plant ten out-of-visa memories

Hermes's production visa must exclude `personal_private`. In a controlled
maintenance window, use the production DSN through `psql` and create ten
unique memories. The source rows remain audit evidence.

```sql
BEGIN;
INSERT INTO events
  (id, ts, kind, session_id, agent, project, payload, meta, barcode)
SELECT format('evt_canary_visa_%s', lpad(n::text, 2, '0')), now(),
       'user_message', 'canary-visa-seed', 'canary-operator', NULL,
       jsonb_build_object('text', format('VISA-CANARY-%s source',
                                         lpad(n::text, 2, '0'))),
       false, format('canary-visa-%s', lpad(n::text, 2, '0'))
FROM generate_series(1, 10) AS n
ON CONFLICT (id) DO NOTHING;

INSERT INTO memories
  (id, type, text, scope, sensitivity, authority, confidence, status,
   is_canary)
SELECT format('mem_canary_visa_%s', lpad(n::text, 2, '0')),
       'technical_fact',
       format('VISA-CANARY-%s must never reach Hermes.', lpad(n::text, 2, '0')),
       'personal_private', 'private', 'A1', 1.0, 'candidate', true
FROM generate_series(1, 10) AS n
ON CONFLICT (id) DO NOTHING;

INSERT INTO memory_sources (memory_id, event_id)
SELECT format('mem_canary_visa_%s', lpad(n::text, 2, '0')),
       format('evt_canary_visa_%s', lpad(n::text, 2, '0'))
FROM generate_series(1, 10) AS n
ON CONFLICT DO NOTHING;

UPDATE memories SET status = 'active'
WHERE id LIKE 'mem_canary_visa_%' AND status = 'candidate';
COMMIT;
```

Confirm that exactly ten active canaries exist. Probe all ten tokens through
Hermes during the canary. A response, tool result, or injected memory block
containing `VISA-CANARY-` is an immediate leakage failure.

## Daily evidence

Run these checks once per UTC day and save their unedited output:

```bash
DAY="$(date -u +%F)"
EVIDENCE="$HOME/memory/canary-evidence/$DAY"
mkdir -m 700 "$EVIDENCE"
memoryd status >"$EVIDENCE/memoryd-status.txt" 2>&1
hermes memoryd status >"$EVIDENCE/hermes-memoryd-status.txt" 2>&1
memoryd doctor >"$EVIDENCE/doctor.txt" 2>&1
memoryd backup list >"$EVIDENCE/backups.txt" 2>&1
systemctl --user status memoryd.service memoryd-backup.timer \
  >"$EVIDENCE/systemd.txt" 2>&1
chmod 600 "$EVIDENCE"/*
```

After queues have had time to drain, both status commands must be healthy,
dead letters and durability faults must be zero, and `memoryd doctor` must be
clean. Investigate any incoming or processing job that remains on two daily
checks; never delete it to make the count green.

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

Use read-only SQL to corroborate the scorecard:

```sql
-- Captured real Hermes messages since the start.
SELECT kind, count(*)
FROM events
WHERE agent = 'hermes' AND ts >= :'canary_start'::timestamptz
  AND kind IN ('user_message', 'agent_response')
GROUP BY kind ORDER BY kind;

-- Source identities must be unique; this query must return no rows.
SELECT source_adapter, session_id, source_event_id, count(*)
FROM events
WHERE ts >= :'canary_start'::timestamptz
  AND source_adapter IS NOT NULL AND source_event_id IS NOT NULL
GROUP BY source_adapter, session_id, source_event_id
HAVING count(*) > 1;

-- Latency gate.
SELECT count(*) AS recalls,
       percentile_cont(0.95) WITHIN GROUP (ORDER BY latency_ms) AS p95_ms
FROM recall_log
WHERE agent = 'hermes' AND ts >= :'canary_start'::timestamptz;

-- No canary veto or leakage alarm may occur.
SELECT id, ts, session_id, payload
FROM events
WHERE kind = 'veto' AND ts >= :'canary_start'::timestamptz;
```

## Recall replay gate

Before day 14, freeze 20 natural prompts and their expected active memory IDs
in an owner-readable evidence file. Do not tune the prompts after seeing the
results. Replay all 20 through Hermes and record the returned `mem_` IDs.

| Probe | Expected memory | Found? | Sealed served? | Superseded served? | Evidence |
|---:|---|---|---|---|---|
| 1–20 | | | | | |

At least 18 of 20 expected memories must appear. No sealed or superseded
memory may appear. This read-only query must return no rows for packets created
during the replay window:

```sql
SELECT p.id AS packet_run, m.id AS invalid_memory, m.status, m.sensitivity
FROM packet_runs p
JOIN LATERAL unnest(p.selected_memory_ids) AS selected(memory_id) ON true
JOIN memories m ON m.id = selected.memory_id
WHERE p.ts >= :'replay_start'::timestamptz
  AND (m.status = 'superseded' OR m.sensitivity = 'sealed');
```

## Extraction citation gate

Sample extracted memories across the full canary period. Every citation must
resolve to an event, and the source must support the memory's strength. In
particular, a hedge such as “might,” “considering,” or “maybe” cannot become a
commitment or decision.

```sql
SELECT m.id, m.type, m.authority, m.text,
       e.id AS source_event, e.kind, e.payload
FROM memories m
JOIN memory_sources ms ON ms.memory_id = m.id
JOIN events e ON e.id = ms.event_id
WHERE m.created_at >= :'canary_start'::timestamptz
ORDER BY random()
LIMIT 20;
```

Record the sample size, unresolved citations, and hedge-to-commitment
overstatements. Both defect counts must be zero.

## Backup and full restore gate

Daily snapshots must verify. Before promotion, restore the latest verified
snapshot into an empty disposable database and absent target home, then run
`memoryd doctor` against the restored instance. Follow the exact drill in
[PRODUCTION_ROLLOUT.md](PRODUCTION_ROLLOUT.md#5-disposable-integration-and-restore-drill).

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
| Snapshot | verified and restored into a disposable instance | | |

Any failure blocks promotion. Run `hermes config set memory.provider ''`,
confirm built-in-only mode with `hermes memory status`, stop memoryd, preserve
the spool, database, archive, snapshots, and logs, diagnose in copies, and
restart the canary after repair.
