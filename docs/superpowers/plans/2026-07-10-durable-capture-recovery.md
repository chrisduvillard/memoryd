# Durable Capture and Recovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Preserve failed captures as self-contained transcript snapshots, ingest them idempotently, and diagnose or conservatively repair spool and archive defects.

**Architecture:** A new stdlib-only spool module writes checksum-addressed blobs and atomic versioned manifests. Transcript ingestion gains stable source identities backed by a partial PostgreSQL unique index. A doctor module inspects spool and archive state, while daemon replay uses explicit incoming, processing, and dead-letter states.

**Tech Stack:** Python 3.11+, Python standard library, psycopg 3, PostgreSQL 16, pgvector, existing script-based test harness

## Global Constraints

- Python remains compatible with version 3.11 and later.
- Add no runtime dependency.
- `memoryd.hook` and `memoryd.spool` remain stdlib-only; neither may import psycopg or `memoryd.core`.
- Capture remains fail-open, but failure to persist evidence must produce a visible warning.
- Preserve legacy pointer-only jobs and all dead-letter evidence.
- Repair operations never delete jobs, transcript evidence, archive objects, memory rows, or ledger events.
- Keep the on-disk spool format identical on Windows, macOS, and Linux.
- Preserve the existing `/capture` request and response contract.
- Follow test-driven development: run each new test in a failing state before writing production code.
- Do not refactor retrieval, extraction, or unrelated server behavior.

## File Map

- Create `memoryd/spool.py`: stdlib-only blob, manifest, claim, retry, dead-letter, and stale-job primitives.
- Create `memoryd/doctor.py`: pure spool/archive inspection plus CLI orchestration.
- Create `migrations/006_durable_capture.sql`: stable source provenance and uniqueness.
- Create `scripts/test_durable_capture.py`: DB-free behavioral tests.
- Modify `memoryd/hook.py`: durable failure-path spooling and visible terminal warning.
- Modify `memoryd/core.py`: optional event provenance, safe fonds paths, atomic archive writes, and occurrence manifests.
- Modify `memoryd/ingest.py`: multi-event transcript classification, stable event IDs, and stateful spool replay.
- Modify `memoryd/server.py`: stale processing recovery at startup.
- Modify `memoryd/microsleep.py`: use the stateful spool replay path.
- Modify `memoryd/cli.py`: `doctor` command and state-aware spool reporting.
- Modify `scripts/smoke_test.py`: DB-backed idempotency and durable replay checks.
- Modify `.github/workflows/tests.yml`: run the new DB-free test and updated integration suite.
- Modify `README.md` and `docs/REFERENCE.md`: document durable capture and recovery commands.

---

### Task 1: Atomic Spool Blobs and Versioned Manifests

**Files:**
- Create: `memoryd/spool.py`
- Create: `scripts/test_durable_capture.py`

**Interfaces:**
- Consumes: a spool root and an existing transcript path.
- Produces: `ensure_layout(spool_root: Path) -> dict[str, Path]`, `enqueue_capture(*, spool_root: Path, transcript_path: Path, session_id: str, project: str | None, trigger: str) -> dict`, `load_job(path: Path) -> dict`, and `validate_blob(spool_root: Path, job: dict) -> Path`.

- [ ] **Step 1: Write failing tests for durable snapshots and deduplication**

Create `scripts/test_durable_capture.py` with this initial content:

```python
from __future__ import annotations

import json
import tempfile
import threading
from pathlib import Path

from memoryd.spool import enqueue_capture, ensure_layout, validate_blob


def test_snapshot_survives_original_deletion() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "spool"
        transcript = Path(td) / "session.jsonl"
        transcript.write_bytes(b'{"type":"user"}\n')

        job = enqueue_capture(
            spool_root=root,
            transcript_path=transcript,
            session_id="session-1",
            project="memoryd",
            trigger="stop",
        )
        transcript.unlink()

        blob = validate_blob(root, job)
        assert blob.read_bytes() == b'{"type":"user"}\n'
        manifests = list((root / "incoming").glob("*.json"))
        assert len(manifests) == 1
        assert json.loads(manifests[0].read_text())["schema_version"] == 2


def test_identical_snapshots_share_one_blob() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "spool"
        transcript = Path(td) / "session.jsonl"
        transcript.write_text("same", encoding="utf-8")
        jobs = []

        def write() -> None:
            jobs.append(enqueue_capture(
                spool_root=root,
                transcript_path=transcript,
                session_id="session-1",
                project=None,
                trigger="stop",
            ))

        threads = [threading.Thread(target=write) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert len(jobs) == 4
        assert len({job["blob_sha256"] for job in jobs}) == 1
        assert len(list((root / "blobs").glob("[0-9a-f]" * 64))) == 1
        assert len(list((root / "incoming").glob("*.json"))) == 4


if __name__ == "__main__":
    test_snapshot_survives_original_deletion()
    test_identical_snapshots_share_one_blob()
    print("2 passed, 0 failed")
```

- [ ] **Step 2: Run the test and verify the missing module failure**

Run: `python scripts/test_durable_capture.py`

Expected: FAIL with `ModuleNotFoundError: No module named 'memoryd.spool'`.

- [ ] **Step 3: Implement atomic snapshot creation**

Create `memoryd/spool.py` with these definitions:

```python
from __future__ import annotations

import hashlib
import json
import os
import secrets
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import BinaryIO

SCHEMA_VERSION = 2
BUFFER_BYTES = 1024 * 1024


class SpoolError(RuntimeError):
    pass


class PermanentSpoolError(SpoolError):
    pass


def ensure_layout(spool_root: Path) -> dict[str, Path]:
    paths = {name: spool_root / name for name in
             ("blobs", "incoming", "processing", "dead-letter")}
    for path in paths.values():
        path.mkdir(parents=True, exist_ok=True)
    return paths


def _job_id() -> str:
    return f"job_{time.time_ns()}_{os.getpid()}_{secrets.token_hex(8)}"


def _copy_and_hash(src: BinaryIO, dst: BinaryIO) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    while chunk := src.read(BUFFER_BYTES):
        dst.write(chunk)
        digest.update(chunk)
        size += len(chunk)
    return digest.hexdigest(), size


def _atomic_json(path: Path, value: dict) -> None:
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    try:
        with tmp.open("x", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def enqueue_capture(*, spool_root: Path, transcript_path: Path,
                    session_id: str, project: str | None,
                    trigger: str) -> dict:
    paths = ensure_layout(spool_root)
    source = transcript_path.expanduser()
    if not source.is_file():
        raise PermanentSpoolError(f"transcript not found: {source}")
    job_id = _job_id()
    tmp_blob = paths["blobs"] / f".{job_id}.tmp"
    try:
        with source.open("rb") as src, tmp_blob.open("xb") as dst:
            sha, size = _copy_and_hash(src, dst)
            dst.flush()
            os.fsync(dst.fileno())
        blob = paths["blobs"] / sha
        if blob.exists():
            tmp_blob.unlink(missing_ok=True)
        else:
            try:
                os.replace(tmp_blob, blob)
            except OSError:
                if blob.exists():
                    tmp_blob.unlink(missing_ok=True)
                else:
                    raise
        job = {
            "schema_version": SCHEMA_VERSION,
            "job_id": job_id,
            "kind": "capture_snapshot",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "session_id": session_id,
            "project": project,
            "trigger": trigger,
            "original_transcript_path": str(source),
            "blob_sha256": sha,
            "blob_bytes": size,
            "attempts": 0,
            "last_error": None,
            "next_attempt_at": None,
        }
        _atomic_json(paths["incoming"] / f"{job_id}.json", job)
        return job
    finally:
        tmp_blob.unlink(missing_ok=True)


def load_job(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise PermanentSpoolError(f"invalid job manifest: {exc}") from exc
    if value.get("schema_version") != SCHEMA_VERSION:
        raise PermanentSpoolError("unsupported job schema")
    required = {"job_id", "kind", "session_id", "trigger", "blob_sha256", "blob_bytes"}
    missing = required - value.keys()
    if missing:
        raise PermanentSpoolError(f"missing manifest fields: {sorted(missing)}")
    if value["kind"] != "capture_snapshot" or not str(value["session_id"]):
        raise PermanentSpoolError("invalid capture job identity")
    if not isinstance(value["blob_bytes"], int) or value["blob_bytes"] < 0:
        raise PermanentSpoolError("invalid blob byte count")
    return value


def validate_blob(spool_root: Path, job: dict) -> Path:
    sha = str(job.get("blob_sha256", ""))
    if len(sha) != 64 or any(c not in "0123456789abcdef" for c in sha):
        raise PermanentSpoolError("invalid blob checksum")
    blob = ensure_layout(spool_root)["blobs"] / sha
    if not blob.is_file():
        raise PermanentSpoolError(f"missing spool blob: {sha}")
    if blob.stat().st_size != int(job["blob_bytes"]):
        raise PermanentSpoolError(f"spool blob size mismatch: {sha}")
    digest = hashlib.sha256()
    with blob.open("rb") as handle:
        while chunk := handle.read(BUFFER_BYTES):
            digest.update(chunk)
    if digest.hexdigest() != sha:
        raise PermanentSpoolError(f"spool blob checksum mismatch: {sha}")
    return blob
```

- [ ] **Step 4: Run the DB-free test and compile check**

Run: `python scripts/test_durable_capture.py && python -m compileall -q memoryd scripts`

Expected: `2 passed, 0 failed`; compile exits 0.

- [ ] **Step 5: Commit the spool primitive**

```bash
git add memoryd/spool.py scripts/test_durable_capture.py
git commit -m "feat: add durable capture spool"
```

---

### Task 2: Hook Failure-Path Integration

**Files:**
- Modify: `memoryd/hook.py:14-19,66-86`
- Modify: `scripts/test_durable_capture.py`

**Interfaces:**
- Consumes: `enqueue_capture` from Task 1.
- Produces: capture failure snapshots under `<home>/spool` and a visible stderr warning when persistence fails.

- [ ] **Step 1: Add failing hook integration tests**

Add these imports and tests to `scripts/test_durable_capture.py`:

```python
import contextlib
import io
from unittest.mock import patch

from memoryd.hook import capture


def test_hook_spools_bytes_when_daemon_is_down() -> None:
    with tempfile.TemporaryDirectory() as td:
        home = Path(td) / "memory"
        transcript = Path(td) / "session.jsonl"
        transcript.write_text("durable", encoding="utf-8")
        stdin = {"transcript_path": str(transcript), "session_id": "s1", "cwd": td}
        with patch("memoryd.hook._post", side_effect=OSError("down")):
            capture(stdin, "stop", 7437, home)
        transcript.unlink()
        jobs = list((home / "spool" / "incoming").glob("*.json"))
        assert len(jobs) == 1
        job = json.loads(jobs[0].read_text())
        assert validate_blob(home / "spool", job).read_text() == "durable"


def test_hook_warns_when_delivery_and_spooling_fail() -> None:
    stderr = io.StringIO()
    stdin = {"transcript_path": "missing.jsonl", "session_id": "s1", "cwd": ""}
    with patch("memoryd.hook._post", side_effect=OSError("down")), \
         contextlib.redirect_stderr(stderr):
        capture(stdin, "stop", 7437, Path("unused"))
    assert "capture not durably saved" in stderr.getvalue()
```

Update the script footer to call all four tests and print `4 passed, 0 failed`.

- [ ] **Step 2: Run the tests and verify the failure**

Run: `python scripts/test_durable_capture.py`

Expected: FAIL because `capture` still writes a pointer-only JSON file and emits no warning.

- [ ] **Step 3: Replace pointer-only hook spooling**

Add `from .spool import enqueue_capture` to `memoryd/hook.py`. Replace the `except` block in `capture` with:

```python
    except Exception:  # daemon unavailable: preserve transcript bytes locally
        try:
            enqueue_capture(
                spool_root=home / "spool",
                transcript_path=Path(transcript),
                session_id=body["session_id"],
                project=body["project"],
                trigger=trigger,
            )
        except Exception as exc:  # fail-open, but never hide evidence loss
            print(f"[memoryd: capture not durably saved: {exc}]", file=sys.stderr)
```

Remove the now-unused `time` import from `hook.py`.

- [ ] **Step 4: Verify hook behavior**

Run: `python scripts/test_durable_capture.py && python -m compileall -q memoryd scripts`

Expected: `4 passed, 0 failed`; compile exits 0.

- [ ] **Step 5: Commit hook integration**

```bash
git add memoryd/hook.py scripts/test_durable_capture.py
git commit -m "fix: persist failed captures with transcript bytes"
```

---

### Task 3: Stable Event Provenance and Multi-Event Classification

**Files:**
- Create: `migrations/006_durable_capture.sql`
- Modify: `memoryd/core.py:199-224`
- Modify: `memoryd/ingest.py:28-61,64-116`
- Modify: `scripts/test_durable_capture.py`
- Modify: `scripts/smoke_test.py:140-164`

**Interfaces:**
- Consumes: transcript lines from files or spool blobs.
- Produces: `_classify_all(entry: dict) -> list[tuple[str, dict]]`; `append_event(conn, *, kind: str, session_id: str, ts: datetime | None = None, agent: str = "claude-code", project: str | None = None, raw_sha256: str | None = None, payload: dict | None = None, meta: bool = False, source_adapter: str | None = None, source_event_id: str | None = None, source_seq: int | None = None, ingest_job_id: str | None = None) -> str | None`.

- [ ] **Step 1: Write failing DB-free classification tests**

Add this import and test to `scripts/test_durable_capture.py`:

```python
from memoryd.ingest import _classify_all


def test_mixed_transcript_line_preserves_text_and_tools() -> None:
    assistant = {"type": "assistant", "message": {"content": [
        {"type": "text", "text": "final answer"},
        {"type": "tool_use", "name": "shell", "input": {"command": "pwd"}},
    ]}}
    user = {"type": "user", "message": {"content": [
        {"type": "text", "text": "remember this"},
        {"type": "tool_result", "content": "ok"},
    ]}}
    assert [kind for kind, _ in _classify_all(assistant)] == [
        "agent_response", "tool_call"]
    assert [kind for kind, _ in _classify_all(user)] == [
        "user_message", "tool_result"]
```

Update the footer count to five.

- [ ] **Step 2: Run the test and verify the import failure**

Run: `python scripts/test_durable_capture.py`

Expected: FAIL with `ImportError` because `_classify_all` does not exist.

- [ ] **Step 3: Add migration 006**

Create `migrations/006_durable_capture.sql`:

```sql
-- memoryd migration 006: durable capture provenance and idempotency
BEGIN;

ALTER TABLE events ADD COLUMN IF NOT EXISTS source_adapter TEXT;
ALTER TABLE events ADD COLUMN IF NOT EXISTS source_event_id TEXT;
ALTER TABLE events ADD COLUMN IF NOT EXISTS source_seq BIGINT;
ALTER TABLE events ADD COLUMN IF NOT EXISTS ingest_job_id TEXT;

CREATE UNIQUE INDEX IF NOT EXISTS events_source_identity
  ON events (source_adapter, session_id, source_event_id)
  WHERE source_adapter IS NOT NULL AND source_event_id IS NOT NULL;

COMMIT;
```

- [ ] **Step 4: Extend `append_event` with optional provenance**

Add keyword parameters to `append_event`:

```python
    source_adapter: str | None = None,
    source_event_id: str | None = None,
    source_seq: int | None = None,
    ingest_job_id: str | None = None,
) -> str | None:
```

Replace its insert with:

```python
    row = conn.execute(
        """INSERT INTO events (id, ts, kind, session_id, agent, project,
                               raw_sha256, payload, meta, barcode,
                               source_adapter, source_event_id, source_seq,
                               ingest_job_id)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
           ON CONFLICT (source_adapter, session_id, source_event_id)
             WHERE source_adapter IS NOT NULL AND source_event_id IS NOT NULL
           DO NOTHING
           RETURNING id""",
        (eid, ts, kind, session_id, agent, project, raw_sha256,
         Jsonb(payload), meta, barcode(ts, session_id, kind, content_hash),
         source_adapter, source_event_id, source_seq, ingest_job_id),
    ).fetchone()
    if not row:
        return None
    return row["id"] if isinstance(row, dict) else row[0]
```

- [ ] **Step 5: Implement ordered multi-event classification**

Replace `_classify` with:

```python
def _block_text(block: dict) -> str:
    value = block.get("text")
    if value is None:
        value = block.get("content", "")
    if isinstance(value, list):
        return _text_of(value)
    return str(value or "")


def _classify_all(entry: dict) -> list[tuple[str, dict]]:
    etype = entry.get("type")
    content = (entry.get("message") or {}).get("content")
    if not isinstance(content, list):
        text = str(content or "")[:4000]
        if not text:
            return []
        if etype == "user":
            return [("user_message", {"text": text})]
        if etype == "assistant":
            return [("agent_response", {"text": text})]
        return []

    events: list[tuple[str, dict]] = []
    for block in content:
        if isinstance(block, str):
            kind = "user_message" if etype == "user" else "agent_response"
            events.append((kind, {"text": block[:4000]}))
        elif not isinstance(block, dict):
            continue
        elif block.get("type") == "text":
            kind = "user_message" if etype == "user" else "agent_response"
            text = _block_text(block)[:4000]
            if text:
                events.append((kind, {"text": text}))
        elif etype == "assistant" and block.get("type") == "tool_use":
            events.append(("tool_call", {"tools": [{
                "name": block.get("name"),
                "input_keys": sorted((block.get("input") or {}).keys()),
            }]}))
        elif etype == "user" and block.get("type") == "tool_result":
            events.append(("tool_result", {"summary": _block_text(block)[:2000]}))
    return events
```

- [ ] **Step 6: Use stable source IDs during ingestion**

Extend `ingest_transcript` with optional parameters:

```python
def ingest_transcript(transcript_path: str, session_id: str, project: str | None,
                      trigger: str, *, ingest_job_id: str | None = None,
                      source_adapter: str = "claude-code") -> dict:
```

Replace the line loop with an enumerated loop. For each parsed line:

```python
        for line_no, raw_line in enumerate(
                path.read_text(encoding="utf-8", errors="replace").splitlines()):
            line = raw_line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            native = entry.get("uuid")
            base_id = (f"uuid:{native}" if native else
                       f"line:{line_no}:{hashlib.sha256(raw_line.encode()).hexdigest()}")
            ts = _parse_ts(entry)
            for ordinal, (kind, payload) in enumerate(_classify_all(entry)):
                event_id = f"{base_id}:{ordinal}:{kind}"
                inserted = append_event(
                    conn, kind=kind, session_id=session_id, ts=ts,
                    project=project, raw_sha256=sha, payload=payload,
                    source_adapter=source_adapter,
                    source_event_id=event_id,
                    source_seq=line_no,
                    ingest_job_id=ingest_job_id,
                )
                new_events += int(inserted is not None)
```

Remove the old barcode prefetch and probe logic. Import `hashlib` at module scope.

Give `capture_ack` the same replay protection by replacing its append call with:

```python
            append_event(
                conn, kind="capture_ack", session_id=session_id,
                project=project, raw_sha256=sha, meta=True,
                payload={"trigger": trigger, "new_events": new_events},
                source_adapter=source_adapter,
                source_event_id=f"capture_ack:{sha}:{trigger}",
                ingest_job_id=ingest_job_id,
            )
```

The source ID makes exact replay idempotent even for `session_end` and `pre_compact`, which always write an acknowledgment.

- [ ] **Step 7: Add a DB-backed idempotency regression to the smoke test**

After the existing re-ingestion check in `scripts/smoke_test.py`, create a mixed transcript in the existing temporary directory, capture it twice under a new session ID, wait for the worker after each call, and assert:

```python
        mixed_session = f"smoke-mixed-{int(time.time())}"
        mixed = td / "mixed.jsonl"
        mixed.write_text(json.dumps({
            "uuid": "stable-mixed-line",
            "type": "assistant",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "message": {"content": [
                {"type": "text", "text": "kept answer"},
                {"type": "tool_use", "name": "shell", "input": {"command": "pwd"}},
            ]},
        }) + "\n", encoding="utf-8")
        body = {"transcript_path": str(mixed), "session_id": mixed_session,
                "project": "smoketest", "trigger": "stop"}
        http("/capture", body)
        time.sleep(1)
        http("/capture", body)
        time.sleep(1)
        with psycopg.connect(DSN) as conn:
            kinds = [r[0] for r in conn.execute(
                "SELECT kind FROM events WHERE session_id=%s ORDER BY source_event_id",
                (mixed_session,)).fetchall() if r[0] != "capture_ack"]
        check("mixed transcript preserves text and tool call",
              sorted(kinds) == ["agent_response", "tool_call"], str(kinds))
        check("stable source ids prevent duplicate replay", len(kinds) == 2, str(kinds))
```

- [ ] **Step 8: Run DB-free tests, then the integration test with a migrated test database**

Run:

```bash
python scripts/test_durable_capture.py
python -c "import os; from memoryd.cli import apply_migrations; print(apply_migrations(os.environ['MEMORYD_DSN']))"
python scripts/smoke_test.py
```

Expected: DB-free test prints `5 passed, 0 failed`; smoke test includes the two new passing checks.

- [ ] **Step 9: Commit event provenance**

```bash
git add migrations/006_durable_capture.sql memoryd/core.py memoryd/ingest.py scripts/test_durable_capture.py scripts/smoke_test.py
git commit -m "feat: make transcript ingestion source-idempotent"
```

---

### Task 4: Safe Archive Paths and Occurrence Manifests

**Files:**
- Modify: `memoryd/core.py:155-195`
- Modify: `scripts/test_durable_capture.py`

**Interfaces:**
- Produces: `validate_fonds_path(archive_root: Path, fonds_path: str) -> Path`; `archive_bytes(data: bytes, mime: str, fonds_path: str, ingest_job_id: str | None = None) -> str`.
- Preserves: existing `archive_file` and `read_blob` callers.

- [ ] **Step 1: Add failing path and occurrence tests**

Add to `scripts/test_durable_capture.py`:

```python
from memoryd import core


def test_fonds_paths_cannot_escape_archive() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "archive"
        for unsafe in ("../escape", "/absolute/path", r"C:\escape", r"a\..\escape"):
            try:
                core.validate_fonds_path(root, unsafe)
            except ValueError:
                pass
            else:
                raise AssertionError(f"unsafe path accepted: {unsafe}")
        safe = core.validate_fonds_path(root, "claude-code/2026/07/session.jsonl")
        assert safe.is_relative_to((root / "fonds").resolve())


def test_archive_records_each_occurrence() -> None:
    with tempfile.TemporaryDirectory() as td:
        old_home = core.CFG.home
        core.CFG.home = Path(td)
        try:
            core.CFG.ensure_dirs()
            sha1 = core.archive_bytes(b"same", "text/plain", "a/one.txt", ingest_job_id="j1")
            sha2 = core.archive_bytes(b"same", "text/plain", "b/two.txt", ingest_job_id="j2")
            assert sha1 == sha2
            entries = [json.loads(line) for line in
                       (core.CFG.archive / "manifest.jsonl").read_text().splitlines()]
            assert [entry["fonds_path"] for entry in entries] == ["a/one.txt", "b/two.txt"]
            assert [entry["ingest_job_id"] for entry in entries] == ["j1", "j2"]
        finally:
            core.CFG.home = old_home
```

Update the footer count to seven.

- [ ] **Step 2: Run tests and verify the failures**

Run: `python scripts/test_durable_capture.py`

Expected: FAIL because `validate_fonds_path` and `ingest_job_id` do not exist.

- [ ] **Step 3: Implement path validation**

Add imports `contextlib`, `secrets`, `PurePosixPath`, and `PureWindowsPath` to `core.py`. Add:

```python
def validate_fonds_path(archive_root: Path, fonds_path: str) -> Path:
    if not fonds_path or PureWindowsPath(fonds_path).drive:
        raise ValueError(f"unsafe fonds path: {fonds_path!r}")
    normalized = fonds_path.replace("\\", "/")
    rel = PurePosixPath(normalized)
    parts = normalized.split("/")
    if rel.is_absolute() or any(part in ("", ".", "..") for part in parts):
        raise ValueError(f"unsafe fonds path: {fonds_path!r}")
    root = (archive_root / "fonds").resolve()
    target = (root / Path(*parts)).resolve()
    if not target.is_relative_to(root):
        raise ValueError(f"fonds path escapes archive: {fonds_path!r}")
    return target
```

- [ ] **Step 4: Make object and manifest writes atomic**

Change `archive_bytes` to accept `ingest_job_id`. Use a unique temp object, flush and fsync it, call `os.replace`, and validate the link through `validate_fonds_path`. Append one compact JSON line for every call:

```python
def archive_bytes(data: bytes, mime: str, fonds_path: str,
                  ingest_job_id: str | None = None) -> str:
    sha = hashlib.sha256(data).hexdigest()
    obj_dir = CFG.archive / "objects" / "sha256" / sha[:2] / sha[2:4]
    obj_path = obj_dir / sha
    obj_dir.mkdir(parents=True, exist_ok=True)
    if not obj_path.exists():
        tmp = obj_dir / f".{sha}.{os.getpid()}.{secrets.token_hex(8)}.tmp"
        try:
            with tmp.open("xb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, obj_path)
        finally:
            tmp.unlink(missing_ok=True)

    link = validate_fonds_path(CFG.archive, fonds_path)
    link.parent.mkdir(parents=True, exist_ok=True)
    try:
        if not link.exists():
            os.symlink(os.path.relpath(obj_path, link.parent), link)
    except OSError:
        pass

    seen = datetime.fromtimestamp(obj_path.stat().st_mtime, timezone.utc).isoformat()
    occurrence = {
        "sha256": sha,
        "bytes": len(data),
        "mime": mime,
        "first_seen": seen,
        "occurrence_at": datetime.now(timezone.utc).isoformat(),
        "fonds_path": fonds_path.replace("\\", "/"),
        "ingest_job_id": ingest_job_id,
    }
    append_manifest_occurrence(CFG.archive, occurrence)
    return sha
```

Add this cross-process lock and occurrence writer:

```python
@contextlib.contextmanager
def _manifest_file_lock(manifest: Path):
    lock = manifest.with_suffix(".lock")
    deadline = time.monotonic() + 5
    fd = None
    while fd is None:
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, f"{os.getpid()}\n".encode())
        except FileExistsError:
            try:
                if time.time() - lock.stat().st_mtime > 900:
                    lock.unlink(missing_ok=True)
                    continue
            except FileNotFoundError:
                continue
            if time.monotonic() >= deadline:
                raise TimeoutError(f"manifest lock timeout: {lock}")
            time.sleep(0.01)
    try:
        yield
    finally:
        os.close(fd)
        lock.unlink(missing_ok=True)


def append_manifest_occurrence(archive_root: Path, occurrence: dict) -> None:
    manifest = archive_root / "manifest.jsonl"
    line = json.dumps(occurrence, sort_keys=True, default=str) + "\n"
    with _manifest_file_lock(manifest):
        with manifest.open("a", encoding="utf-8") as handle:
            handle.write(line)
            handle.flush()
            os.fsync(handle.fileno())
```

Call `append_manifest_occurrence(CFG.archive, occurrence)` from `archive_bytes`. Replace `archive_file` with:

```python
def archive_file(path: Path, fonds_path: str,
                 mime: str = "application/octet-stream",
                 ingest_job_id: str | None = None) -> str:
    return archive_bytes(path.read_bytes(), mime, fonds_path,
                         ingest_job_id=ingest_job_id)
```

Change the call at the start of `ingest_transcript` to:

```python
    sha = archive_file(path, fonds, mime="application/x-jsonl",
                       ingest_job_id=ingest_job_id)
```

Pass `ingest_job_id` through `archive_file` and from `ingest_transcript`.

- [ ] **Step 5: Verify archive behavior and existing DB-free checks**

Run:

```bash
python scripts/test_durable_capture.py
python scripts/test_bitter_lesson.py
python -m compileall -q memoryd scripts
```

Expected: durable test prints `7 passed, 0 failed`; bitter-lesson test prints `37 passed, 0 failed`; compile exits 0.

- [ ] **Step 6: Commit archive hardening**

```bash
git add memoryd/core.py scripts/test_durable_capture.py
git commit -m "fix: harden archive paths and occurrence tracking"
```

---

### Task 5: Stateful Replay, Retry, and Dead-Letter Preservation

**Files:**
- Modify: `memoryd/spool.py`
- Modify: `memoryd/ingest.py:119-138`
- Modify: `memoryd/server.py:311-325`
- Modify: `memoryd/microsleep.py:23-28`
- Modify: `scripts/test_durable_capture.py`
- Modify: `scripts/smoke_test.py`

**Interfaces:**
- Produces: `claim_next`, `release_job`, `dead_letter`, `complete_job`, `requeue_stale`, `upgrade_legacy_job`, and `gc_blob_if_unreferenced`.
- Changes: `drain_spool() -> dict[str, int]` returns state counts instead of one integer.

- [ ] **Step 1: Add failing state-transition tests**

Add to `scripts/test_durable_capture.py`:

```python
import os
from datetime import datetime, timedelta, timezone

from memoryd.spool import (
    claim_next, dead_letter, requeue_stale, release_job, upgrade_legacy_job,
)


def test_claim_retry_and_dead_letter_preserve_manifest() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "spool"
        transcript = Path(td) / "session.jsonl"
        transcript.write_text("x", encoding="utf-8")
        enqueue_capture(spool_root=root, transcript_path=transcript,
                        session_id="s", project=None, trigger="stop")
        claimed = claim_next(root)
        assert claimed and claimed.parent.name == "processing"
        released = release_job(root, claimed, "database down", delay_s=1)
        value = json.loads(released.read_text())
        assert value["attempts"] == 1
        assert value["last_error"] == "database down"
        claimed = claim_next(root, ignore_schedule=True)
        assert claimed
        preserved = dead_letter(root, claimed, "checksum mismatch")
        assert preserved.exists()
        reason = preserved.with_suffix(".reason.json")
        assert json.loads(reason.read_text())["reason"] == "checksum mismatch"


def test_legacy_missing_source_is_preserved() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "spool"
        paths = ensure_layout(root)
        legacy = root / "cap-old.json"
        original = {"transcript_path": str(Path(td) / "gone.jsonl"),
                    "session_id": "old", "trigger": "stop"}
        legacy.write_text(json.dumps(original), encoding="utf-8")
        result = upgrade_legacy_job(root, legacy)
        assert result is None
        preserved = paths["dead-letter"] / legacy.name
        assert json.loads(preserved.read_text()) == original
        assert preserved.with_suffix(".reason.json").exists()


def test_stale_processing_job_is_requeued() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "spool"
        transcript = Path(td) / "session.jsonl"
        transcript.write_text("x", encoding="utf-8")
        enqueue_capture(spool_root=root, transcript_path=transcript,
                        session_id="s", project=None, trigger="stop")
        claimed = claim_next(root)
        assert claimed
        old = (datetime.now(timezone.utc) - timedelta(minutes=20)).timestamp()
        os.utime(claimed, (old, old))
        assert requeue_stale(root, stale_after_s=900) == 1
        assert list((root / "incoming").glob("*.json"))
```

Update the footer count to ten.

- [ ] **Step 2: Run tests and verify missing function failures**

Run: `python scripts/test_durable_capture.py`

Expected: FAIL with imports missing from `memoryd.spool`.

- [ ] **Step 3: Implement job state primitives**

Add these implementations to `memoryd/spool.py`:

```python
def _scheduled(job: dict) -> bool:
    raw = job.get("next_attempt_at")
    if not raw:
        return False
    try:
        due = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return False
    if due.tzinfo is None:
        due = due.replace(tzinfo=timezone.utc)
    return due > datetime.now(timezone.utc)


def claim_next(spool_root: Path, *, ignore_schedule: bool = False) -> Path | None:
    paths = ensure_layout(spool_root)
    sources = sorted([*spool_root.glob("*.json"), *paths["incoming"].glob("*.json")])
    for source in sources:
        try:
            job = json.loads(source.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            job = {}
        if not ignore_schedule and _scheduled(job):
            continue
        target = paths["processing"] / source.name
        try:
            os.replace(source, target)
        except FileNotFoundError:
            continue
        return target
    return None


def release_job(spool_root: Path, processing_path: Path, error: str,
                *, delay_s: int) -> Path:
    job = json.loads(processing_path.read_text(encoding="utf-8"))
    job["attempts"] = int(job.get("attempts", 0)) + 1
    job["last_error"] = error
    job["next_attempt_at"] = (
        datetime.now(timezone.utc) + timedelta(seconds=delay_s)).isoformat()
    _atomic_json(processing_path, job)
    target = ensure_layout(spool_root)["incoming"] / processing_path.name
    os.replace(processing_path, target)
    return target


def dead_letter(spool_root: Path, job_path: Path, reason: str) -> Path:
    paths = ensure_layout(spool_root)
    target = paths["dead-letter"] / job_path.name
    if target.exists():
        target = target.with_name(f"{target.stem}-{secrets.token_hex(4)}{target.suffix}")
    os.replace(job_path, target)
    _atomic_json(target.with_suffix(".reason.json"), {
        "dead_lettered_at": datetime.now(timezone.utc).isoformat(),
        "reason": reason,
        "manifest": target.name,
    })
    return target


def complete_job(job_path: Path) -> None:
    job_path.unlink()


def requeue_stale(spool_root: Path, *, stale_after_s: int = 900) -> int:
    paths = ensure_layout(spool_root)
    cutoff = time.time() - stale_after_s
    moved = 0
    for source in paths["processing"].glob("*.json"):
        if source.stat().st_mtime >= cutoff:
            continue
        target = paths["incoming"] / source.name
        try:
            os.replace(source, target)
            moved += 1
        except FileNotFoundError:
            continue
    return moved


def upgrade_legacy_job(spool_root: Path, legacy_path: Path) -> Path | None:
    job = json.loads(legacy_path.read_text(encoding="utf-8"))
    source = Path(job.get("transcript_path", "")).expanduser()
    if not source.is_file():
        dead_letter(spool_root, legacy_path, "legacy transcript source missing")
        return None
    upgraded = enqueue_capture(
        spool_root=spool_root,
        transcript_path=source,
        session_id=job.get("session_id", "unknown"),
        project=job.get("project"),
        trigger=job.get("trigger", "unknown"),
    )
    dead_letter(spool_root, legacy_path, "upgraded to schema 2")
    return ensure_layout(spool_root)["incoming"] / f"{upgraded['job_id']}.json"


def gc_blob_if_unreferenced(spool_root: Path, sha: str,
                            canonical_object: Path) -> bool:
    if not canonical_object.is_file():
        return False
    paths = ensure_layout(spool_root)
    manifests = [*spool_root.glob("*.json")]
    for state in ("incoming", "processing", "dead-letter"):
        manifests.extend(p for p in paths[state].glob("*.json")
                         if not p.name.endswith(".reason.json"))
    for manifest in manifests:
        try:
            if json.loads(manifest.read_text()).get("blob_sha256") == sha:
                return False
        except (OSError, ValueError):
            continue
    blob = paths["blobs"] / sha
    blob.unlink(missing_ok=True)
    return True
```

Add `timedelta` to the existing datetime import in `spool.py`.

`upgrade_legacy_job` must parse the original JSON without modifying it. If the transcript exists, call `enqueue_capture`, then preserve the legacy manifest in dead-letter with reason `upgraded to schema 2`. If the transcript is missing, preserve it with reason `legacy transcript source missing` and return `None`.

- [ ] **Step 4: Rewrite `drain_spool` around explicit states**

Change `drain_spool` to:

```python
def drain_spool() -> dict[str, int]:
    from .spool import (
        PermanentSpoolError, claim_next, complete_job, dead_letter,
        gc_blob_if_unreferenced, load_job, release_job, requeue_stale,
        upgrade_legacy_job, validate_blob,
    )
    stats = {"processed": 0, "retried": 0, "dead_lettered": 0, "requeued": 0}
    stats["requeued"] = requeue_stale(CFG.spool)
    while job_path := claim_next(CFG.spool):
        try:
            raw = json.loads(job_path.read_text(encoding="utf-8"))
            if raw.get("extract_only"):
                from .extract import run_extraction
                result = run_extraction(raw["session_id"])
                if not result.get("ok"):
                    raise RuntimeError(result.get("error", "extraction retry failed"))
                complete_job(job_path)
                stats["processed"] += 1
                continue
            if raw.get("schema_version") != 2:
                upgraded = upgrade_legacy_job(CFG.spool, job_path)
                stats["dead_lettered"] += int(upgraded is None)
                continue
            job = load_job(job_path)
            blob = validate_blob(CFG.spool, job)
            result = ingest_transcript(
                str(blob), job["session_id"], job.get("project"), job["trigger"],
                ingest_job_id=job["job_id"], source_adapter="claude-code")
            if not result.get("ok"):
                raise RuntimeError(result.get("error", "capture ingestion failed"))
            if job["trigger"] in ("session_end", "pre_compact"):
                from .extract import run_extraction
                extracted = run_extraction(job["session_id"])
                if not extracted.get("ok") and not extracted.get("skipped"):
                    raise RuntimeError(extracted.get("error", "extraction failed"))
            complete_job(job_path)
            sha = result["sha256"]
            canonical = (CFG.archive / "objects" / "sha256" /
                         sha[:2] / sha[2:4] / sha)
            gc_blob_if_unreferenced(CFG.spool, sha, canonical)
            stats["processed"] += 1
        except (PermanentSpoolError, ValueError) as exc:
            dead_letter(CFG.spool, job_path, str(exc))
            stats["dead_lettered"] += 1
        except Exception as exc:
            attempts = int(json.loads(job_path.read_text()).get("attempts", 0)) + 1
            release_job(CFG.spool, job_path, str(exc)[:1000],
                        delay_s=min(3600, 2 ** min(attempts, 10)))
            stats["retried"] += 1
    return stats
```

- [ ] **Step 5: Persist `/capture` before returning 202 and requeue stale work**

Import `Path` and `enqueue_capture` in `memoryd/server.py`. Replace the `/capture` handler branch with:

```python
        elif self.path == "/capture":
            required = {"transcript_path", "session_id"}
            if not required <= body.keys():
                self._json(400, {"error": f"missing fields: {required - body.keys()}"})
                return
            try:
                job = enqueue_capture(
                    spool_root=CFG.spool,
                    transcript_path=Path(body["transcript_path"]),
                    session_id=body["session_id"],
                    project=body.get("project"),
                    trigger=body.get("trigger", "unknown"),
                )
            except Exception as exc:
                self._json(500, {"error": f"capture could not be persisted: {exc}"})
                return
            CAPTURE_Q.put({"drain_spool": True})
            self._json(202, {"queued": True, "job_id": job["job_id"]})
```

Replace `_capture_worker` with:

```python
def _capture_worker() -> None:
    while True:
        job = CAPTURE_Q.get()
        try:
            if job.get("extract_only"):
                from .extract import run_extraction
                run_extraction(job["session_id"])
            elif job.get("drain_spool"):
                drain_spool()
        except Exception as exc:
            print(f"memoryd: capture worker failed: {exc}")
        finally:
            CAPTURE_Q.task_done()
```

Replace `_drain_spool_bg`'s integer handling with:

```python
        stats = drain_spool()
        if any(stats.values()):
            print("memoryd: spool " + ", ".join(
                f"{key}={value}" for key, value in stats.items()))
```

Keep the digest initialization and replace the old integer drain lines with:

```python
    report: list[str] = [f"# memoryd digest — {date.today().isoformat()}", ""]
    spool_stats = drain_spool()
    report.append("- spool: " + ", ".join(
        f"{key}={value}" for key, value in spool_stats.items()))
```

- [ ] **Step 6: Add an integration check for durable replay after source deletion**

Add this integration block in `scripts/smoke_test.py` after the existing archive check. It verifies that the HTTP endpoint owns a durable snapshot before it acknowledges the request:

```python
        durable_session = f"smoke-durable-{int(time.time())}"
        durable = td / "durable.jsonl"
        durable.write_text(json.dumps({
            "uuid": "durable-line",
            "type": "user",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "message": {"content": "survives source deletion"},
        }) + "\n", encoding="utf-8")
        code, response = http("/capture", {
            "transcript_path": str(durable),
            "session_id": durable_session,
            "project": "smoketest",
            "trigger": "stop",
        })
        check("capture acknowledges durable spool job",
              code == 202 and bool(response.get("job_id")), str(response))
        durable.unlink()
        time.sleep(1)
        with psycopg.connect(DSN) as conn:
            durable_rows = conn.execute(
                "SELECT count(*) FROM events WHERE session_id=%s",
                (durable_session,)).fetchone()[0]
        check("durable spool replays after source deletion",
              durable_rows >= 1, f"rows={durable_rows}")
```

- [ ] **Step 7: Run focused and integration tests**

Run:

```bash
python scripts/test_durable_capture.py
python scripts/smoke_test.py
python scripts/test_extract.py
```

Expected: durable test prints `10 passed, 0 failed`; smoke and extraction checks report zero failures.

- [ ] **Step 8: Commit the replay lifecycle**

```bash
git add memoryd/spool.py memoryd/ingest.py memoryd/server.py memoryd/microsleep.py scripts/test_durable_capture.py scripts/smoke_test.py
git commit -m "feat: add retryable capture replay lifecycle"
```

---

### Task 6: Integrity Inspection and Conservative Repair

**Files:**
- Create: `memoryd/doctor.py`
- Modify: `memoryd/cli.py:596-620`
- Modify: `scripts/test_durable_capture.py`

**Interfaces:**
- Produces: `Finding`, `inspect_spool`, `inspect_archive`, `repair_spool`, `repair_archive`, and `main(repair: bool = False) -> int`.
- CLI: `memoryd doctor` and `memoryd doctor --repair`.

- [ ] **Step 1: Add failing diagnostic tests**

Add to `scripts/test_durable_capture.py`:

```python
import hashlib

from memoryd.doctor import inspect_archive, inspect_spool, repair_archive, repair_spool


def test_doctor_detects_missing_sources_and_archive_objects() -> None:
    with tempfile.TemporaryDirectory() as td:
        home = Path(td)
        paths = ensure_layout(home / "spool")
        legacy = home / "spool" / "legacy.json"
        legacy.write_text(json.dumps({"transcript_path": str(home / "gone"),
                                      "session_id": "s"}), encoding="utf-8")
        archive = home / "archive"
        archive.mkdir()
        (archive / "manifest.jsonl").write_text(json.dumps({
            "sha256": "a" * 64, "fonds_path": "safe/path"}) + "\n")
        spool_codes = {item.code for item in inspect_spool(home / "spool")}
        archive_codes = {item.code for item in inspect_archive(archive)}
        assert "legacy_source_missing" in spool_codes
        assert "manifest_object_missing" in archive_codes

        broken = {
            "schema_version": 2, "job_id": "broken", "kind": "capture_snapshot",
            "session_id": "s", "trigger": "stop", "blob_sha256": "b" * 64,
            "blob_bytes": 1,
        }
        (paths["incoming"] / "broken.json").write_text(json.dumps(broken))
        assert "spool_blob_invalid" in {
            item.code for item in inspect_spool(home / "spool")}


def test_doctor_repair_preserves_legacy_job() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "spool"
        paths = ensure_layout(root)
        legacy = root / "legacy.json"
        original = {"transcript_path": str(Path(td) / "gone"), "session_id": "s"}
        legacy.write_text(json.dumps(original), encoding="utf-8")
        actions = repair_spool(root)
        preserved = paths["dead-letter"] / "legacy.json"
        assert json.loads(preserved.read_text()) == original
        assert any(action.code == "legacy_dead_lettered" for action in actions)


def test_doctor_reconstructs_manifest_from_job_evidence() -> None:
    with tempfile.TemporaryDirectory() as td:
        home = Path(td)
        spool = home / "spool"
        paths = ensure_layout(spool)
        data = b"archived"
        sha = hashlib.sha256(data).hexdigest()
        obj = home / "archive" / "objects" / "sha256" / sha[:2] / sha[2:4] / sha
        obj.parent.mkdir(parents=True)
        obj.write_bytes(data)
        job = {
            "schema_version": 2,
            "job_id": "job_evidence",
            "kind": "capture_snapshot",
            "created_at": "2026-07-10T08:00:00+00:00",
            "session_id": "safe-session",
            "project": "memoryd",
            "trigger": "stop",
            "blob_sha256": sha,
            "blob_bytes": len(data),
        }
        (paths["dead-letter"] / "job_evidence.json").write_text(json.dumps(job))
        actions = repair_archive(home / "archive", spool)
        assert any(action.code == "manifest_occurrence_reconstructed" for action in actions)
        entries = (home / "archive" / "manifest.jsonl").read_text().splitlines()
        assert json.loads(entries[0])["sha256"] == sha
```

Update the footer count to thirteen.

- [ ] **Step 2: Run tests and verify the missing module failure**

Run: `python scripts/test_durable_capture.py`

Expected: FAIL with `ModuleNotFoundError: No module named 'memoryd.doctor'`.

- [ ] **Step 3: Implement pure inspection functions**

Create `memoryd/doctor.py` with:

```python
from __future__ import annotations

import hashlib
import json
import time
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .core import append_manifest_occurrence, validate_fonds_path
from .spool import (
    PermanentSpoolError, dead_letter, ensure_layout, requeue_stale,
    upgrade_legacy_job, validate_blob,
)


@dataclass(frozen=True)
class Finding:
    code: str
    severity: str
    path: str
    detail: str
    repairable: bool = False


def inspect_spool(spool_root: Path) -> list[Finding]:
    paths = ensure_layout(spool_root)
    findings: list[Finding] = []
    manifests = [*spool_root.glob("*.json"), *paths["incoming"].glob("*.json"),
                 *paths["processing"].glob("*.json")]
    for manifest in manifests:
        try:
            job = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            findings.append(Finding("invalid_manifest", "error", str(manifest), str(exc), True))
            continue
        if job.get("schema_version") != 2:
            source = Path(job.get("transcript_path", "")).expanduser()
            if not source.is_file():
                findings.append(Finding("legacy_source_missing", "error",
                                        str(manifest), str(source), True))
        else:
            try:
                validate_blob(spool_root, job)
            except PermanentSpoolError as exc:
                findings.append(Finding("spool_blob_invalid", "error",
                                        str(manifest), str(exc), True))
        if manifest.parent == paths["processing"] and time.time() - manifest.stat().st_mtime > 900:
            findings.append(Finding("stale_processing_job", "error",
                                    str(manifest), "older than 15 minutes", True))
    dead = [p for p in paths["dead-letter"].glob("*.json")
            if not p.name.endswith(".reason.json")]
    if dead:
        findings.append(Finding("dead_letter_jobs", "error",
                                str(paths["dead-letter"]), str(len(dead))))
    return findings


def inspect_archive(archive_root: Path) -> list[Finding]:
    findings: list[Finding] = []
    object_root = archive_root / "objects" / "sha256"
    objects = {p.name: p for p in object_root.rglob("*") if p.is_file()}
    manifest = archive_root / "manifest.jsonl"
    mentioned: set[str] = set()
    if manifest.exists():
        for line_no, line in enumerate(manifest.read_text(encoding="utf-8").splitlines(), 1):
            try:
                entry = json.loads(line)
            except ValueError as exc:
                findings.append(Finding("invalid_manifest_line", "error",
                                        f"{manifest}:{line_no}", str(exc)))
                continue
            sha = str(entry.get("sha256", ""))
            mentioned.add(sha)
            try:
                validate_fonds_path(archive_root, str(entry.get("fonds_path", "")))
            except ValueError as exc:
                findings.append(Finding("unsafe_fonds_path", "error",
                                        f"{manifest}:{line_no}", str(exc)))
            if sha not in objects:
                findings.append(Finding("manifest_object_missing", "error",
                                        f"{manifest}:{line_no}", sha))
    for sha, path in objects.items():
        actual = _sha256_file(path)
        if actual != sha:
            findings.append(Finding("object_hash_mismatch", "error", str(path), actual))
        if sha not in mentioned:
            findings.append(Finding("orphan_object", "warning", str(path), sha))
    return findings


def repair_spool(spool_root: Path) -> list[Finding]:
    actions: list[Finding] = []
    requeued = requeue_stale(spool_root)
    if requeued:
        actions.append(Finding("stale_jobs_requeued", "info", str(spool_root), str(requeued)))
    paths = ensure_layout(spool_root)
    manifests = [*spool_root.glob("*.json"), *paths["incoming"].glob("*.json")]
    for manifest in manifests:
        try:
            job = json.loads(manifest.read_text(encoding="utf-8"))
        except ValueError as exc:
            dead_letter(spool_root, manifest, f"invalid manifest: {exc}")
            actions.append(Finding("invalid_job_dead_lettered", "info",
                                   str(manifest), str(exc)))
            continue
        if job.get("schema_version") == 2:
            try:
                validate_blob(spool_root, job)
            except PermanentSpoolError as exc:
                dead_letter(spool_root, manifest, str(exc))
                actions.append(Finding("corrupt_job_dead_lettered", "info",
                                       str(manifest), str(exc)))
            continue
        upgraded = upgrade_legacy_job(spool_root, manifest)
        actions.append(Finding(
            "legacy_upgraded" if upgraded else "legacy_dead_lettered",
            "info", str(manifest), str(upgraded or "preserved in dead-letter")))
    return actions


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def repair_archive(archive_root: Path, spool_root: Path) -> list[Finding]:
    actions: list[Finding] = []
    existing: set[str] = set()
    manifest = archive_root / "manifest.jsonl"
    if manifest.exists():
        for line in manifest.read_text(encoding="utf-8").splitlines():
            try:
                existing.add(str(json.loads(line).get("sha256", "")))
            except ValueError:
                continue
    paths = ensure_layout(spool_root)
    jobs = [*spool_root.glob("*.json")]
    for state in ("incoming", "processing", "dead-letter"):
        jobs.extend(p for p in paths[state].glob("*.json")
                    if not p.name.endswith(".reason.json"))
    for job_path in jobs:
        try:
            job = json.loads(job_path.read_text(encoding="utf-8"))
            sha = str(job.get("blob_sha256", ""))
            created = datetime.fromisoformat(str(job["created_at"]).replace("Z", "+00:00"))
        except (OSError, ValueError, KeyError):
            continue
        if not sha or sha in existing:
            continue
        obj = archive_root / "objects" / "sha256" / sha[:2] / sha[2:4] / sha
        if not obj.is_file() or _sha256_file(obj) != sha:
            continue
        fonds = f"claude-code/{created:%Y/%m/%d}/{job['session_id']}.jsonl"
        try:
            validate_fonds_path(archive_root, fonds)
        except (ValueError, KeyError):
            continue
        append_manifest_occurrence(archive_root, {
            "sha256": sha,
            "bytes": obj.stat().st_size,
            "mime": "application/x-jsonl",
            "first_seen": datetime.fromtimestamp(
                obj.stat().st_mtime, timezone.utc).isoformat(),
            "occurrence_at": created.isoformat(),
            "fonds_path": fonds,
            "ingest_job_id": job.get("job_id"),
        })
        existing.add(sha)
        actions.append(Finding("manifest_occurrence_reconstructed", "info",
                               str(obj), str(job_path)))
    return actions
```

Add `datetime` and `timezone` to the imports used by these functions.

- [ ] **Step 4: Add CLI orchestration**

Implement CLI orchestration with this function:

```python
def main(repair: bool = False) -> int:
    from .core import CFG, pool

    CFG.ensure_dirs()
    if repair:
        for action in [*repair_spool(CFG.spool),
                       *repair_archive(CFG.archive, CFG.spool)]:
            print(f"REPAIRED {action.code}: {action.path} ({action.detail})")
    paths = ensure_layout(CFG.spool)
    counts = {
        "incoming": len(list(CFG.spool.glob("*.json")))
                    + len(list(paths["incoming"].glob("*.json"))),
        "processing": len(list(paths["processing"].glob("*.json"))),
        "dead_letter": len([p for p in paths["dead-letter"].glob("*.json")
                            if not p.name.endswith(".reason.json")]),
    }
    print("memoryd doctor: " + ", ".join(
        f"{key}={value}" for key, value in counts.items()))
    findings = [*inspect_spool(CFG.spool), *inspect_archive(CFG.archive)]
    try:
        with pool().connection() as conn:
            conn.execute("SELECT 1")
    except Exception as exc:
        findings.append(Finding("database_unreachable", "error", "database", str(exc)))
    try:
        with urllib.request.urlopen(
                f"http://127.0.0.1:{CFG.port}/health", timeout=2) as response:
            if not json.loads(response.read()).get("ok"):
                raise RuntimeError("health response was not ok")
    except Exception as exc:
        findings.append(Finding("daemon_unreachable", "error", "daemon", str(exc)))
    for item in findings:
        print(f"{item.severity.upper()} {item.code}: {item.path} ({item.detail})")
    if not findings:
        print("memoryd doctor: no integrity defects found")
    return int(any(item.severity == "error" for item in findings))
```

Add to `memoryd.cli.main`:

```python
    elif cmd == "doctor":
        from .doctor import main as doctor_main
        repair = "--repair" in sys.argv[2:]
        sys.exit(doctor_main(repair=repair))
```

Add `memoryd doctor` and `memoryd doctor --repair` to the CLI module's usage text.

- [ ] **Step 5: Verify diagnostics against fixtures and the live home**

Run:

```bash
python scripts/test_durable_capture.py
python -m memoryd doctor
```

Expected: durable test prints `13 passed, 0 failed`. On the current home, doctor exits nonzero and reports the preserved legacy missing-source jobs and manifest entries whose objects are absent. Do not run `--repair` against the live home during this test.

- [ ] **Step 6: Commit diagnostics**

```bash
git add memoryd/doctor.py memoryd/cli.py scripts/test_durable_capture.py
git commit -m "feat: add memory integrity doctor"
```

---

### Task 7: Status, CI, Documentation, and Full Verification

**Files:**
- Modify: `memoryd/cli.py:519-526`
- Modify: `.github/workflows/tests.yml:45-69`
- Modify: `README.md:134-154`
- Modify: `docs/REFERENCE.md:54-84`
- Modify: `scripts/test_durable_capture.py`

**Interfaces:**
- Status output reports incoming, processing, and dead-letter counts.
- CI runs the durable DB-free suite before daemon startup and all DB-backed suites afterward.

- [ ] **Step 1: Add a failing status-format test**

Add a pure helper to the desired API in `scripts/test_durable_capture.py`:

```python
from memoryd.cli import _spool_counts


def test_status_counts_spool_states() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "spool"
        paths = ensure_layout(root)
        (paths["incoming"] / "one.json").write_text("{}")
        (paths["processing"] / "two.json").write_text("{}")
        (paths["dead-letter"] / "three.json").write_text("{}")
        assert _spool_counts(root) == {"incoming": 1, "processing": 1, "dead_letter": 1}
```

Update the footer count to fourteen.

- [ ] **Step 2: Run tests and verify the missing helper failure**

Run: `python scripts/test_durable_capture.py`

Expected: FAIL with `ImportError` for `_spool_counts`.

- [ ] **Step 3: Add state-aware status output**

Add to `memoryd/cli.py`:

```python
def _spool_counts(spool_root: Path) -> dict[str, int]:
    legacy = len(list(spool_root.glob("*.json")))
    return {
        "incoming": legacy + len(list((spool_root / "incoming").glob("*.json"))),
        "processing": len(list((spool_root / "processing").glob("*.json"))),
        "dead_letter": len([p for p in (spool_root / "dead-letter").glob("*.json")
                            if not p.name.endswith(".reason.json")]),
    }
```

Replace the flat spool count in `status()` with:

```python
    spool_counts = _spool_counts(home / "spool")
    if spool_counts["dead_letter"]:
        ok = False
    print("  spool      "
          f"incoming={spool_counts['incoming']} "
          f"processing={spool_counts['processing']} "
          f"dead-letter={spool_counts['dead_letter']}"
          + ("  <- run `memoryd doctor`" if spool_counts["dead_letter"] else ""))
```

- [ ] **Step 4: Update CI**

Add this step after package installation:

```yaml
      - name: Run DB-free durable-capture checks
        run: python scripts/test_durable_capture.py
```

Keep migration application before daemon startup. Update the suite label and comments to reflect the new checks; avoid a hard-coded total that will drift.

- [ ] **Step 5: Document operation and recovery**

Add these commands to README verification and daily-use sections:

```text
memoryd doctor
memoryd doctor --repair   # conservative: preserves all evidence
```

Document the four spool directories, version 2 manifest fields, dead-letter preservation, and the stable source ID rule in `docs/REFERENCE.md`. State that the hook snapshots bytes only after daemon delivery fails.

- [ ] **Step 6: Run the complete verification matrix**

With PostgreSQL, migrations, and the daemon running, execute:

```bash
python -m compileall -q memoryd hermes_plugin scripts
python scripts/test_durable_capture.py
python scripts/test_bitter_lesson.py
python scripts/smoke_test.py
python scripts/test_extract.py
python scripts/test_vector.py
python scripts/test_hermes.py
python -c "import os,subprocess,sys,tempfile; home=tempfile.mkdtemp(); env={**os.environ,'MEMORYD_HOME':home}; raise SystemExit(subprocess.call([sys.executable,'-m','memoryd','doctor'],env=env))"
```

Expected:

- compile exits 0;
- durable capture prints `14 passed, 0 failed`;
- bitter-lesson prints `37 passed, 0 failed`;
- every DB-backed script reports zero failures;
- doctor reports no defects in the isolated test home.

- [ ] **Step 7: Review the final diff against the approved design**

Run:

```bash
git diff --check
git status --short
git diff --stat HEAD~6
```

Confirm that changes remain limited to the files in this plan and that `graphify-out/` is neither staged nor committed.

- [ ] **Step 8: Commit status, CI, and documentation**

```bash
git add memoryd/cli.py .github/workflows/tests.yml README.md docs/REFERENCE.md scripts/test_durable_capture.py
git commit -m "chore: wire durable capture verification"
```

## Plan Self-Review Checklist

- Every approved design goal maps to at least one task.
- Task 1 preserves transcript bytes and deduplicates blobs.
- Task 2 wires the fail-open hook and visible warning.
- Task 3 implements stable event provenance and multi-event classification.
- Task 4 enforces archive path safety and occurrence tracking.
- Task 5 implements claim, retry, stale recovery, replay, and dead-letter preservation.
- Task 6 implements diagnostics and conservative repair.
- Task 7 covers status, CI, documentation, and the full regression matrix.
- Function names and return types remain consistent across tasks.
- Production code always follows a failing test.
- No task introduces a runtime dependency or unrelated refactor.
