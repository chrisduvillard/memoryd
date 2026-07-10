# Durable Capture and Recovery Design

**Status:** Approved design
**Date:** 2026-07-10
**Scope:** Durable failed-capture spooling, idempotent transcript ingestion, archive verification, and conservative recovery

## Purpose

memoryd must preserve raw evidence when the daemon or database is unavailable. The current hook stores only the original transcript path after delivery fails. If that file disappears before replay, memoryd loses the capture. This design replaces pointer-only spooling with checksum-addressed transcript snapshots and adds the provenance needed for idempotent ingestion.

This slice also adds archive diagnostics and conservative recovery. It preserves legacy jobs whose source files have disappeared as dead-letter evidence.

## Goals

- Preserve transcript bytes before a failed capture hook returns.
- Recover captures after daemon, database, or process restarts.
- Prevent duplicate ledger events when memoryd replays the same snapshot.
- Preserve all text and tool activity from mixed transcript lines.
- Detect corrupt jobs, missing archive objects, bad hashes, and unsafe paths.
- Preserve every unrecoverable job with its failure reason.
- Keep the capture hook stdlib-only and fail-open.
- Add no runtime dependency.

## Non-goals

This slice does not add daemon authentication, encrypted storage, working memory, episodic retrieval, cursor-based extraction, backup and restore, or Hermes disk spooling. Later designs will address those systems. The interfaces introduced here must not block them.

## Architecture

Add a stdlib-only `memoryd/spool.py`. Both `memoryd.hook` and the daemon-side ingestion code may import it; the module must not import `memoryd.core`, psycopg, or any network client.

The spool uses four directories under `MEMORYD_HOME/spool`:

```text
spool/
  blobs/          checksum-addressed transcript snapshots
  incoming/       complete jobs ready for processing
  processing/     jobs claimed by one worker
  dead-letter/    preserved jobs that cannot be processed
```

The hook first attempts the existing `/capture` request. On failure, it streams the transcript into a temporary file under `blobs/`, calculates SHA-256, flushes the file, and publishes it without overwriting `blobs/<sha256>`. It syncs each newly created parent entry and the containing directory where the platform permits it. Known unsupported directory-sync errors are safe fallbacks; genuine I/O errors abort acknowledgement. An existing regular file wins only after its size and digest match. If the incumbent is corrupt, redirected, not a regular file, or replaced before final validation, memoryd preserves the known-good temporary bytes and refuses to acknowledge the capture.

After the blob is durable, the hook writes a versioned job manifest to a temporary file under `incoming/` and atomically renames it to `<job_id>.json`. A visible warning reports failure if the hook can neither reach the daemon nor persist the bundle.

The daemon claims a job by atomically moving its manifest from `incoming/` to `processing/`. Cross-directory state moves sync the destination and source directories, lease touches sync the processing file, and completion syncs the directory after unlink. Rename-race catches cover only the rename syscall; post-rename durability failures propagate with the manifest already in its new state. It opens and validates the blob without following redirects, then passes the exact verified bytes to archival and classification. It removes the processing manifest only after the database commit succeeds. Blob garbage collection may remove a spool blob only when no incoming, processing, or dead-letter job references it and immediate locked rechecks confirm both the spool blob and canonical archive object.

## Job Manifest

Version 2 capture jobs use this shape:

```json
{
  "schema_version": 2,
  "job_id": "job_<time-ns>_<pid>_<16-hex-random-chars>",
  "kind": "capture_snapshot",
  "created_at": "2026-07-10T08:00:00+00:00",
  "session_id": "source session id",
  "project": "project label or null",
  "trigger": "stop|session_end|pre_compact|unknown",
  "original_transcript_path": "path retained for audit only",
  "blob_sha256": "64 lowercase hexadecimal characters",
  "blob_bytes": 12345,
  "attempts": 0,
  "last_error": null,
  "next_attempt_at": null
}
```

The manifest never treats `original_transcript_path` as a recovery source. Version 2 replay reads only the verified spool blob.

The implementation accepts legacy version 1 JSON jobs. If their transcript exists, memoryd upgrades them to a version 2 snapshot before ingestion. If it does not exist, memoryd moves the original JSON file intact to `dead-letter/` and writes a separate `<job_id>.reason.json` record. It never deletes or rewrites the legacy evidence.

## Stable Event Provenance

Migration `006_durable_capture.sql` adds nullable columns to `events`:

```sql
source_adapter TEXT,
source_event_id TEXT,
source_seq BIGINT,
ingest_job_id TEXT
```

A partial unique index enforces uniqueness when an adapter supplies a stable event ID:

```sql
CREATE UNIQUE INDEX events_source_identity
ON events (source_adapter, session_id, source_event_id)
WHERE source_adapter IS NOT NULL AND source_event_id IS NOT NULL;
```

Historical rows remain valid because their new columns are null.

For Claude transcript JSONL, ingestion assigns each source line a stable base identity:

1. Use the transcript entry's native UUID when present.
2. Otherwise use `line:<zero-based-line-number>:<full-line-sha256>`.

A line may emit more than one event. Each emitted event appends a deterministic suffix containing its ordinal and kind, such as `:0:user_message` and `:1:tool_result`. Replaying the same snapshot therefore reaches the unique index rather than inserting duplicate events.

`append_event` accepts the new provenance fields as optional keyword arguments. Existing callers retain their current behavior.

## Transcript Classification

Replace the one-result classifier with a classifier that returns a list of events. It must preserve:

- user text blocks;
- assistant text blocks;
- each tool call summary;
- each tool-result summary; and
- their order within the source line.

Unknown transcript shapes remain in the raw snapshot even when the ledger classifier cannot interpret them.
Malformed top-level values, messages, content blocks, tool inputs, typed text,
and typed tool results emit no ledger event. They complete as raw-only captures instead of entering a
transient retry loop.

## Archive Safety

All archive fonds paths pass through one validator before filesystem access. The validator rejects:

- absolute paths;
- drive-qualified paths;
- empty path components;
- `.` and `..` components; and
- any resolved path outside `archive/fonds`.

Archive object writes use unique temporary names, file flush, `fsync` where the platform supports it, and no-overwrite publication. Concurrent writers of identical bytes converge on the same checksum object. Publication and garbage collection validate the archive root, `objects`, `sha256`, and both shard directories without following redirects. Required existing namespace entries also prompt parent sync, closing the concurrent-winner crash window. Fonds paths are validated before byte publication. Publication retains the known-good temporary inode and an open canonical descriptor until manifest-lock preconditions verify leaf identity before and after append. A failed binding condition rolls back the line and preserves the temporary evidence; unrelated failures clean the temp when the canonical leaf remains verified. Namespace publication syncs every new parent entry and the containing directory where supported.

The manifest records each archival occurrence. Object content remains deduplicated, but repeated captures retain separate provenance records. Each record includes checksum, byte count, MIME type, first-seen time for the object, occurrence time, fonds path, and ingest job ID when available.

Capture fonds paths use the capture job's UTC `created_at`, not processing time. Retries therefore retain one stable path across midnight. Fonds construction normalizes session backslashes before both ingestion and repair. Repair derives the same path and rechecks exact occurrence identity while holding the manifest append lock.

## Failure Handling

Workers retain retryable jobs in `processing/` while an attempt runs. A failed attempt updates the manifest's attempt count, last error, and next-attempt time, then atomically returns it to `incoming/`.

Daemon startup requeues processing jobs older than 15 minutes. This slice keeps the interval fixed to avoid introducing new configuration.

The worker dead-letters a job when:

- the manifest is invalid;
- the referenced blob is missing;
- the blob size or checksum differs from the manifest;
- a legacy job's original transcript no longer exists; or
- its archive path is unsafe.

Database outages, pool timeouts, and temporary filesystem errors remain retryable. The implementation must not classify those failures as permanent.

## `memoryd doctor`

Add `memoryd doctor` and `memoryd doctor --repair`.

The read-only command reports:

- daemon and database health;
- incoming, processing, and dead-letter job counts;
- legacy jobs with missing source paths;
- missing or corrupt spool blobs;
- archive object hash failures;
- manifest entries whose objects are missing;
- archive objects absent from the manifest; and
- unsafe fonds paths.

The command exits nonzero when it finds an integrity defect.

`--repair` performs only these conservative actions:

- requeue stale processing jobs;
- upgrade processable legacy jobs to version 2 bundles;
- dead-letter unrecoverable or corrupt spool jobs with reason records; and
- reconstruct a manifest occurrence when an intact checksum object and sufficient job evidence exist.

Repair never deletes jobs, transcript evidence, archive objects, memory rows, or ledger events. It reports conditions it cannot repair.

## Compatibility

- Healthy `/capture` requests retain their current request and response shapes.
- Existing pointer-only spool JSON remains readable.
- Existing event callers need no provenance arguments.
- The new migration leaves historical rows untouched.
- The hook continues to exit zero and never blocks the agent's primary task.
- Windows, macOS, and Linux use the same on-disk schema and atomic-rename protocol.

## Testing

Add DB-free tests for spool and archive behavior and DB-backed tests for event idempotency.

Required cases:

1. A failed capture remains ingestible after the original transcript is deleted.
2. Replaying one snapshot inserts no duplicate ledger events.
3. Mixed text and tool blocks produce every expected event in source order.
4. Concurrent writers of identical content produce one valid blob.
5. Multiple manifests may safely reference one blob.
6. Missing and corrupt blobs move to dead-letter with reasons.
7. Legacy missing-source jobs remain intact in dead-letter.
8. Absolute and traversing fonds paths are rejected.
9. Stale processing jobs return to incoming after restart.
10. Archive verification detects altered and missing objects.
11. Repair preserves all evidence.
12. Existing capture, extraction, recall, vector, Hermes, and extension tests remain green.

## Success Criteria

- Deleting the original transcript after failed delivery causes no data loss.
- Replaying any capture snapshot is ledger-idempotent.
- The hook reads transcript data with streaming I/O and bounded memory.
- The healthy-daemon path retains its current latency behavior.
- Every permanent failure produces a preserved dead-letter job and reason.
- `memoryd doctor` detects the current missing-source and archive-manifest defects.
- The package gains no runtime dependency.

## Implementation Boundaries

The slice should add `memoryd/spool.py`, migration 006, focused diagnostics, and tests. It should make surgical changes to `hook.py`, `ingest.py`, `core.py`, `cli.py`, and the existing verification scripts. It should not restructure unrelated retrieval, extraction, or server code.
