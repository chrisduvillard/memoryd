"""Crash-durable Hermes memory provider for the memoryd daemon.

Primary-context mutations are synchronously published to a profile-scoped
disk spool before a hook returns.  A single background worker drains that
spool; recall remains fail-open and non-primary contexts remain read-only.
"""
from __future__ import annotations

import contextlib
import hashlib
import http.client
import json
import logging
import math
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, NamedTuple, Optional

from agent.memory_provider import MemoryProvider

DEFAULT_URL = "http://127.0.0.1:7437"
AGENT_NAME = "hermes"
SCHEMA_VERSION = 1
STALE_PROCESSING_SECONDS = 900
MAX_BACKOFF_SECONDS = 300
MUTATION_ENDPOINTS = frozenset({"/capture-events", "/extract", "/miss"})
JOB_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
JOB_FILE_RE = re.compile(
    r"(?P<order>[0-9]{20})-(?P<job_id>[A-Za-z0-9][A-Za-z0-9._-]{0,127})\.json\Z")
JOB_BASE_KEYS = frozenset({
    "schema_version", "job_id", "endpoint", "body", "body_sha256",
    "created_at", "attempts", "next_attempt_at", "last_error",
})
JOB_CLAIM_KEYS = frozenset({"claimed_at"})
JOB_DEAD_KEYS = frozenset({"dead_letter_reason", "dead_lettered_at"})
logger = logging.getLogger(__name__)


class JobCollision(RuntimeError):
    """A job id already has durable evidence and must not be overwritten."""


class HttpResult(NamedTuple):
    kind: str  # success | retry | permanent
    payload: Optional[dict]
    error: str


def _retryable_status(status: int) -> bool:
    return status in (408, 429) or 500 <= status <= 599


def _canonical_json(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")


def _fsync_dir(path: Path) -> None:
    """Make a directory-entry change durable where the OS supports it."""
    if os.name == "nt":
        return
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _private_dir(path: Path) -> None:
    missing = []
    cursor = path
    while not cursor.exists():
        missing.append(cursor)
        parent = cursor.parent
        if parent == cursor:
            break
        cursor = parent
    for directory in reversed(missing):
        try:
            directory.mkdir(mode=0o700)
        except FileExistsError:
            pass
        if os.name != "nt":
            os.chmod(directory, 0o700)
        # Commit every new directory entry, including spool/memoryd itself.
        _fsync_dir(directory.parent)
    if not missing:
        # Retry the parent sync even if an earlier attempt created the
        # directory and then failed before its directory entry was durable.
        _fsync_dir(path.parent)
    if os.name != "nt":
        os.chmod(path, 0o700)


def _replace(source: Path, destination: Path) -> None:
    """Retry short-lived Windows sharing violations without weakening atomicity."""
    for attempt in range(5):
        try:
            if os.name == "nt":
                import ctypes
                flags = 0x1 | 0x8  # REPLACE_EXISTING | WRITE_THROUGH
                if not ctypes.windll.kernel32.MoveFileExW(
                        str(source), str(destination), flags):
                    raise ctypes.WinError()
            else:
                os.replace(source, destination)
            return
        except PermissionError:
            if os.name != "nt" or attempt == 4:
                raise
            time.sleep(0.01 * (attempt + 1))


def _atomic_json(path: Path, value: dict, *, replace: bool) -> None:
    """fsync a private temp file, publish it, then fsync its parent."""
    _private_dir(path.parent)
    tmp = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    fd = os.open(tmp, flags, 0o600)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(_canonical_json(value) + b"\n")
            stream.flush()
            os.fsync(stream.fileno())
        if not replace and path.exists():
            raise JobCollision(f"durable job collision: {path.name}")
        _replace(tmp, path)
        if os.name != "nt":
            os.chmod(path, 0o600)
        _fsync_dir(path.parent)
    finally:
        with contextlib.suppress(FileNotFoundError):
            tmp.unlink()


class DurableSpool:
    """Profile-scoped mutation spool under ``HERMES_HOME/spool/memoryd``."""

    _STATE_DIR = {"incoming": "incoming", "processing": "processing",
                  "dead_letter": "dead-letter"}

    def __init__(self, hermes_home: Path, *,
                 clock: Callable[[], float] = time.time) -> None:
        self.home = Path(hermes_home)
        self.root = self.home / "spool" / "memoryd"
        self.clock = clock

    def _ensure(self) -> None:
        _private_dir(self.root.parent)
        _private_dir(self.root)
        for dirname in self._STATE_DIR.values():
            _private_dir(self.root / dirname)

    def _dir(self, state: str) -> Path:
        return self.root / self._STATE_DIR[state]

    @contextlib.contextmanager
    def _lock(self) -> Iterator[None]:
        """Take a cross-process advisory lock around claims and moves."""
        self._ensure()
        lock_path = self.root / "spool.lock"
        lock_fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        stream = os.fdopen(lock_fd, "a+b")
        try:
            if os.name == "nt":
                import msvcrt
                stream.seek(0, os.SEEK_END)
                if stream.tell() == 0:
                    stream.write(b"0")
                    stream.flush()
                    os.fsync(stream.fileno())
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_LOCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            if os.name != "nt":
                os.chmod(lock_path, 0o600)
            yield
        finally:
            if os.name == "nt":
                import msvcrt
                stream.seek(0)
                with contextlib.suppress(OSError):
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
            stream.close()

    def _all_paths_unlocked(self) -> Iterator[Path]:
        for dirname in self._STATE_DIR.values():
            yield from (self.root / dirname).glob("*.json")

    @staticmethod
    def _read(path: Path) -> dict:
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError(f"invalid spool job: {path}")
        return value

    def _validate_job(self, job: dict, path: Path, state: str) -> None:
        if state not in self._STATE_DIR or path.parent != self._dir(state):
            raise ValueError("invalid job state directory")
        keys = frozenset(job)
        missing = JOB_BASE_KEYS - keys
        if missing:
            raise ValueError(f"invalid job missing keys: {sorted(missing)}")
        allowed = JOB_BASE_KEYS
        if state == "processing":
            allowed |= JOB_CLAIM_KEYS | JOB_DEAD_KEYS
        elif state == "dead_letter":
            allowed |= JOB_DEAD_KEYS
        unknown = keys - allowed
        if unknown:
            raise ValueError(f"invalid job unknown keys: {sorted(unknown)}")
        schema = job.get("schema_version")
        if type(schema) is not int or schema != SCHEMA_VERSION:
            raise ValueError("invalid job schema_version")
        job_id = job.get("job_id")
        endpoint = job.get("endpoint")
        body = job.get("body")
        if not isinstance(job_id, str) or JOB_ID_RE.fullmatch(job_id) is None:
            raise ValueError("invalid job job_id")
        filename = JOB_FILE_RE.fullmatch(path.name)
        if filename is None or filename.group("job_id") != job_id:
            raise ValueError("invalid job filename binding")
        if not isinstance(endpoint, str) or endpoint not in MUTATION_ENDPOINTS:
            raise ValueError("invalid job endpoint")
        if not isinstance(body, dict):
            raise ValueError("invalid job body")
        if body.get("request_id") != job_id:
            raise ValueError("invalid job request_id")
        expected = hashlib.sha256(_canonical_json(body)).hexdigest()
        if job.get("body_sha256") != expected:
            raise ValueError("invalid job body_sha256")
        if (isinstance(job.get("attempts"), bool) or
                not isinstance(job.get("attempts"), int) or job["attempts"] < 0):
            raise ValueError("invalid job attempts")
        for field in ("created_at", "next_attempt_at"):
            value = job.get(field)
            if (isinstance(value, bool) or not isinstance(value, (int, float)) or
                    not math.isfinite(value) or value < 0):
                raise ValueError(f"invalid job {field}")
        if job["next_attempt_at"] < job["created_at"]:
            raise ValueError("invalid job next_attempt_at before created_at")
        if "claimed_at" in job:
            claimed = job["claimed_at"]
            if (isinstance(claimed, bool) or
                    not isinstance(claimed, (int, float)) or
                    not math.isfinite(claimed) or claimed < job["created_at"]):
                raise ValueError("invalid job claimed_at")
        if job.get("last_error") is not None and not isinstance(job["last_error"], str):
            raise ValueError("invalid job last_error")

        has_reason = "dead_letter_reason" in job
        reason = job.get("dead_letter_reason")
        has_dead_at = "dead_lettered_at" in job
        dead_at = job.get("dead_lettered_at")
        if has_reason and (not isinstance(reason, str) or not reason.strip()):
            raise ValueError("invalid job dead_letter_reason")
        if has_dead_at and (
                isinstance(dead_at, bool) or not isinstance(dead_at, (int, float)) or
                not math.isfinite(dead_at) or dead_at < job["created_at"]):
            raise ValueError("invalid job dead_lettered_at")
        if state == "incoming":
            if keys != JOB_BASE_KEYS:
                raise ValueError("invalid job incoming state")
        elif state == "processing":
            if has_reason != has_dead_at:
                raise ValueError("invalid job processing dead-letter state")
            if has_reason:
                if keys != JOB_BASE_KEYS | JOB_DEAD_KEYS:
                    raise ValueError("invalid job processing dead-letter transition")
            elif keys not in (JOB_BASE_KEYS, JOB_BASE_KEYS | JOB_CLAIM_KEYS):
                raise ValueError("invalid job processing state")
        elif state == "dead_letter":
            if keys != JOB_BASE_KEYS | JOB_DEAD_KEYS:
                raise ValueError("invalid job dead-letter state")
        else:
            raise ValueError(f"invalid spool state: {state}")

    def _quarantine_unlocked(self, source: Path, error: Exception,
                             job: dict | None = None) -> None:
        reason = f"invalid job: {error}"
        destination_name = source.name
        if isinstance(job, dict):
            job_id = job.get("job_id")
            if isinstance(job_id, str) and JOB_ID_RE.fullmatch(job_id):
                match = JOB_FILE_RE.fullmatch(source.name)
                order = (match.group("order") if match else
                         f"{max(0, int(self.clock() * 1_000_000_000)):020d}")
                destination_name = f"{order}-{job_id}.json"
        destination = self._dir("dead_letter") / destination_name
        if isinstance(job, dict):
            evidence = dict(job)
            evidence["last_error"] = reason
            evidence["dead_letter_reason"] = reason
            evidence["dead_lettered_at"] = self.clock()
            evidence.pop("claimed_at", None)
            _atomic_json(source, evidence, replace=True)
            self._move_unlocked(source, destination)
        else:
            # Preserve unreadable bytes verbatim and publish reason separately.
            self._move_unlocked(source, destination)
            _atomic_json(destination.with_suffix(destination.suffix + ".reason"),
                         {"reason": reason, "quarantined_at": self.clock()},
                         replace=False)
        self.set_fault(f"quarantined {reason}")

    def persist(self, endpoint: str, body: dict, *, job_id: str | None = None) -> str:
        if endpoint not in MUTATION_ENDPOINTS:
            raise ValueError(f"mutation endpoint not allowed: {endpoint}")
        if not isinstance(body, dict):
            raise TypeError("mutation body must be an object")
        job_id = job_id or uuid.uuid4().hex
        if not isinstance(job_id, str) or JOB_ID_RE.fullmatch(job_id) is None:
            raise ValueError("invalid job_id")
        now = self.clock()
        canonical_body = dict(body)
        canonical_body["request_id"] = job_id
        job = {
            "schema_version": SCHEMA_VERSION,
            "job_id": job_id,
            "endpoint": endpoint,
            "body": canonical_body,
            "body_sha256": hashlib.sha256(_canonical_json(canonical_body)).hexdigest(),
            "created_at": now,
            "attempts": 0,
            "next_attempt_at": now,
            "last_error": None,
        }
        with self._lock():
            existing_paths = list(self._all_paths_unlocked())
            for existing in existing_paths:
                filename = JOB_FILE_RE.fullmatch(existing.name)
                if filename and filename.group("job_id") == job_id:
                    raise JobCollision(f"durable job collision: {job_id}")
                try:
                    if self._read(existing).get("job_id") == job_id:
                        raise JobCollision(f"durable job collision: {job_id}")
                except json.JSONDecodeError:
                    # Unreadable evidence has a distinct filename and remains
                    # preserved; it must not block unrelated future jobs.
                    continue
            timestamp = max(0, int(now * 1_000_000_000))
            for existing in existing_paths:
                with contextlib.suppress(ValueError):
                    timestamp = max(timestamp, int(existing.name.split("-", 1)[0]) + 1)
            path = self._dir("incoming") / f"{timestamp:020d}-{job_id}.json"
            _atomic_json(path, job, replace=False)
        return job_id

    def list_jobs(self, state: str) -> list[tuple[Path, dict]]:
        self._ensure()
        result = []
        for path in sorted(self._dir(state).glob("*.json"), key=lambda item: item.name):
            result.append((path, self._read(path)))
        return result

    def counts(self) -> dict[str, int]:
        self._ensure()
        return {state: sum(1 for _ in self._dir(state).glob("*.json"))
                for state in self._STATE_DIR}

    def audit_dead_letters(self) -> list[str]:
        """Read-only validation of terminal evidence; never deletes or rewrites it."""
        directory = self._dir("dead_letter")
        if not directory.exists():
            return []
        findings = []
        for path in sorted(directory.glob("*.json"), key=lambda item: item.name):
            try:
                job = self._read(path)
                self._validate_job(job, path, "dead_letter")
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                findings.append(f"{path.name}: {exc}")
        return findings

    def _move_unlocked(self, source: Path, destination: Path) -> Path:
        if destination.exists():
            raise JobCollision(f"move collision: {destination.name}")
        _replace(source, destination)
        _fsync_dir(source.parent)
        if destination.parent != source.parent:
            _fsync_dir(destination.parent)
        return destination

    def _recover_stale_unlocked(self) -> int:
        recovered = 0
        now = self.clock()
        for source in sorted(self._dir("processing").glob("*.json")):
            job = None
            try:
                job = self._read(source)
                self._validate_job(job, source, "processing")
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                self._quarantine_unlocked(source, exc, job)
                recovered += 1
                continue
            claimed = float(job.get("claimed_at") or source.stat().st_mtime)
            if now - claimed < STALE_PROCESSING_SECONDS:
                continue
            destination_state = (
                "dead_letter" if "dead_letter_reason" in job else "incoming")
            if destination_state == "incoming" and "claimed_at" in job:
                job = dict(job)
                job.pop("claimed_at", None)
                _atomic_json(source, job, replace=True)
            destination = self._dir(destination_state) / source.name
            self._move_unlocked(source, destination)
            recovered += 1
        return recovered

    def recover_stale(self) -> int:
        with self._lock():
            return self._recover_stale_unlocked()

    def claim_oldest(self) -> tuple[Path, dict] | None:
        with self._lock():
            self._recover_stale_unlocked()
            # Globally serialize delivery: another provider may own this job.
            if next(self._dir("processing").glob("*.json"), None) is not None:
                return None
            candidates = sorted(self._dir("incoming").glob("*.json"),
                                key=lambda item: item.name)
            if not candidates:
                return None
            source = candidates[0]
            job = None
            try:
                job = self._read(source)
                self._validate_job(job, source, "incoming")
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                self._quarantine_unlocked(source, exc, job)
                return None
            if float(job.get("next_attempt_at", 0)) > self.clock():
                return None
            destination = self._dir("processing") / source.name
            self._move_unlocked(source, destination)
            job["claimed_at"] = self.clock()
            _atomic_json(destination, job, replace=True)
            return destination, job

    def retry(self, path: Path, job: dict, error: str) -> None:
        with self._lock():
            if not path.exists():
                raise FileNotFoundError(path)
            current = self._read(path)
            self._validate_job(current, path, "processing")
            updated = dict(current)
            attempts = int(updated.get("attempts", 0)) + 1
            updated["attempts"] = attempts
            updated["last_error"] = error
            updated["next_attempt_at"] = (
                self.clock() + min(2 ** (attempts - 1), MAX_BACKOFF_SECONDS))
            updated.pop("claimed_at", None)
            _atomic_json(path, updated, replace=True)
            self._move_unlocked(path, self._dir("incoming") / path.name)

    def dead_letter(self, path: Path, job: dict, reason: str) -> None:
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("dead-letter reason must be a nonempty string")
        with self._lock():
            if not path.exists():
                raise FileNotFoundError(path)
            current = self._read(path)
            self._validate_job(current, path, "processing")
            updated = dict(current)
            updated["attempts"] = int(updated.get("attempts", 0)) + 1
            updated["last_error"] = reason
            updated["dead_letter_reason"] = reason
            updated["dead_lettered_at"] = self.clock()
            updated.pop("claimed_at", None)
            _atomic_json(path, updated, replace=True)
            self._move_unlocked(path, self._dir("dead_letter") / path.name)

    def complete(self, path: Path) -> None:
        with self._lock():
            current = self._read(path)
            self._validate_job(current, path, "processing")
            path.unlink()
            _fsync_dir(path.parent)

    def set_fault(self, message: str) -> None:
        self._ensure()
        _atomic_json(self.root / "state.json",
                     {"durability_fault": message, "updated_at": self.clock()},
                     replace=True)

    def fault(self) -> str | None:
        state = self.root / "state.json"
        if not state.exists():
            return None
        try:
            value = self._read(state)
            fault = value.get("durability_fault")
            return str(fault) if fault else None
        except (OSError, ValueError, json.JSONDecodeError):
            return "unreadable spool state"


class MemorydProvider(MemoryProvider):
    def __init__(self) -> None:
        self._url = DEFAULT_URL
        self._project: Optional[str] = None
        self._session_id = ""
        self._platform = "cli"
        self._primary = True
        self._prefetch_cache: Dict[str, str] = {}
        self._spool_store: Optional[DurableSpool] = None
        self._worker: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._warned_down = False
        self._lifecycle_mutation_lock = threading.RLock()
        self._shutdown_requested = threading.Event()
        self._durability_fault: Optional[str] = None
        self._durability_notice_pending = False

    @property
    def name(self) -> str:
        return "memoryd"

    @property
    def durability_fault(self) -> Optional[str]:
        return self._durability_fault

    def is_available(self) -> bool:
        return True

    def initialize(self, session_id: str, **kwargs) -> None:
        self._session_id = session_id
        self._platform = kwargs.get("platform", "cli")
        self._primary = kwargs.get("agent_context", "primary") == "primary"
        home_arg = kwargs.get("hermes_home")
        cfg = {}
        if home_arg:
            config_path = Path(home_arg) / "memoryd.json"
            if config_path.exists():
                try:
                    cfg = json.loads(config_path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    cfg = {}
        self._url = (cfg.get("url") or DEFAULT_URL).rstrip("/")
        self._project = cfg.get("project") or f"hermes-{self._platform}"
        if not self._primary:
            return
        if not home_arg:
            self._record_durability_fault("initialize missing required hermes_home")
            return
        try:
            self._spool_store = DurableSpool(Path(home_arg))
            self._spool_store._ensure()
            self._spool_store.recover_stale()
            stored_fault = self._spool_store.fault()
            if stored_fault:
                self._durability_fault = stored_fault
                self._durability_notice_pending = True
            if self._spool_store.counts()["dead_letter"]:
                self._durability_notice_pending = True
            invalid_dead = self._spool_store.audit_dead_letters()
            if invalid_dead:
                self._durability_fault = (
                    f"invalid dead-letter evidence ({len(invalid_dead)} files)")
                self._durability_notice_pending = True
            self._start_worker()
            self._persist_mutation(
                "/capture-events",
                {"agent": AGENT_NAME, "session_id": self._session_id,
                 "project": self._project,
                 "events": [{"kind": "session_start",
                             "payload": {"platform": self._platform}}]})
        except (OSError, ValueError, JobCollision) as exc:
            self._record_durability_fault(f"spool initialization failed: {exc}")

    def _start_worker(self) -> None:
        if self._worker is None:
            self._worker = threading.Thread(
                target=self._drain, name="memoryd-durable-spool", daemon=True)
            self._worker.start()

    def shutdown(self) -> None:
        if not self._primary:
            return
        self._shutdown_requested.set()
        with self._lifecycle_mutation_lock:
            self._stop.set()
        self._wake.set()
        if self._worker is None:
            return
        self._worker.join(timeout=6.0)
        if self._worker.is_alive():
            raise RuntimeError("memoryd worker did not stop after bounded HTTP timeout")

    def _consume_durability_notice(self) -> str:
        if not self._durability_notice_pending:
            return ""
        self._durability_notice_pending = False
        detail = self._durability_fault or "permanent dead-letter present"
        return f"[memoryd: capture durability fault — {detail}]"

    def system_prompt_block(self) -> str:
        notice = self._consume_durability_notice()
        base = (
            "External long-term memory (memoryd) is active. Recalled context "
            "is injected each turn under '## Memory'; entries cite mem_ ids "
            "and certainty lanes. Treat 'Unconfirmed candidates' as unverified. "
            "Use the memoryd_search tool for anything not already recalled; "
            "use memoryd_report_miss when the user says you forgot something.")
        return f"{base}\n{notice}" if notice else base

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        notice = self._consume_durability_notice()
        if notice:
            return notice
        sid = session_id or self._session_id
        cached = self._prefetch_cache.pop(sid, None)
        if cached is not None:
            return cached
        packet = self._recall(query, sid, timeout=1.5)
        if packet is None:
            if not self._warned_down:
                self._warned_down = True
                return "[memoryd: unavailable — proceeding without external recall]"
            return ""
        return packet

    def queue_prefetch(self, query: str, *, session_id: str = "") -> None:
        sid = session_id or self._session_id

        def background() -> None:
            packet = self._recall(query, sid, timeout=5.0)
            if packet is not None:
                self._prefetch_cache[sid] = packet
                self._warned_down = False

        threading.Thread(target=background, daemon=True).start()

    def _capture(self, events: List[dict], session_id: str = "") -> Optional[str]:
        if not self._primary or not events:
            return None
        return self._persist_mutation(
            "/capture-events",
            {"agent": AGENT_NAME, "session_id": session_id or self._session_id,
             "project": self._project, "events": events})

    def sync_turn(self, user_content: str, assistant_content: str, *,
                  session_id: str = "", messages: Optional[List[Dict[str, Any]]] = None,
                  ) -> None:
        if not self._primary:
            return
        events = []
        if user_content:
            events.append({"kind": "user_message", "payload": {"text": user_content}})
        if assistant_content:
            events.append({"kind": "agent_response", "payload": {"text": assistant_content}})
        if messages:
            tools = [message for message in messages[-10:]
                     if message.get("role") == "tool" or
                     (message.get("role") == "assistant" and message.get("tool_calls"))]
            if tools:
                events.append({"kind": "tool_call", "payload": {
                    "summary": f"{len(tools)} tool interactions this turn"}})
        self._capture(events, session_id=session_id)

    def on_pre_compress(self, messages: List[Dict[str, Any]]) -> str:
        if self._primary and messages:
            text = "\n".join(
                f"{message.get('role', '?')}: {self._text_of(message)}"
                for message in messages)[:200000]
            self._capture([{"kind": "external_note", "payload": {
                "text": text, "note": "pre_compress_snapshot"}}])
        return ""

    def on_memory_write(self, action: str, target: str, content: str,
                        metadata: Optional[Dict[str, Any]] = None) -> None:
        if self._primary:
            self._capture([{"kind": "external_note", "payload": {
                "text": content, "note": "builtin_memory_write", "action": action,
                "target": target, "meta": metadata or {}}}])

    def on_delegation(self, task: str, result: str, *,
                      child_session_id: str = "", **kwargs) -> None:
        if self._primary:
            self._capture([{"kind": "delegation", "payload": {
                "task": task[:4000], "result": result[:4000],
                "child_session_id": child_session_id}}])

    def _persist_boundary(self, session_id: str, payload: dict) -> None:
        # Synchronous calls preserve capture-before-extract publication order.
        capture_id = self._capture(
            [{"kind": "session_end", "payload": payload}], session_id)
        if capture_id is not None:
            self._persist_mutation("/extract", {"session_id": session_id})

    def on_session_switch(self, new_session_id: str, *, parent_session_id: str = "",
                          reset: bool = False, rewound: bool = False, **kwargs) -> None:
        old = self._session_id
        self._session_id = new_session_id
        self._prefetch_cache.pop(old, None)
        if reset and old and self._primary:
            self._persist_boundary(old, {"reason": "reset"})

    def on_session_end(self, messages: List[Dict[str, Any]]) -> None:
        if self._primary:
            self._persist_boundary(self._session_id, {"turns": len(messages)})

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        return [
            {"name": "memoryd_search",
             "description": "Search memoryd for relevant long-term memory.",
             "parameters": {"type": "object", "properties": {
                 "query": {"type": "string"}}, "required": ["query"]}},
            {"name": "memoryd_report_miss",
             "description": "Durably queue a report that memoryd missed context.",
             "parameters": {"type": "object", "properties": {
                 "detail": {"type": "string"}}, "required": ["detail"]}},
        ]

    def handle_tool_call(self, tool_name: str, args: Dict[str, Any], **kwargs) -> str:
        if tool_name == "memoryd_search":
            packet = self._recall(args.get("query", ""), self._session_id, timeout=5.0)
            if packet is None:
                return json.dumps({"ok": False, "error": "memoryd unreachable"})
            return json.dumps({"ok": True,
                               "memory": packet or "(nothing relevant found)"})
        if tool_name == "memoryd_report_miss":
            if not self._primary:
                return json.dumps({"ok": False, "queued": False,
                                   "error": "read-only agent context"})
            job_id = self._persist_mutation(
                "/miss", {"session_id": self._session_id,
                          "signal": "user_said_forgot",
                          "detail": {"note": args.get("detail", "")}})
            return json.dumps({"ok": job_id is not None, "queued": job_id is not None,
                               "request_id": job_id})
        raise NotImplementedError(tool_name)

    def get_config_schema(self) -> List[Dict[str, Any]]:
        return [
            {"key": "url", "description": "memoryd daemon URL",
             "default": DEFAULT_URL, "required": False},
            {"key": "project", "description":
                "Fixed project label for this profile (default: hermes-<platform>)",
             "required": False},
        ]

    def save_config(self, values: Dict[str, Any], hermes_home: str) -> None:
        path = Path(hermes_home) / "memoryd.json"
        _private_dir(path.parent)
        _atomic_json(path, {key: value for key, value in values.items() if value},
                     replace=True)

    def backup_paths(self) -> List[str]:
        # The durable spool is inside active HERMES_HOME and is already included
        # by Hermes backup; only paths outside HERMES_HOME belong in this list.
        return []

    @staticmethod
    def _text_of(message: Dict[str, Any]) -> str:
        content = message.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return " ".join(block.get("text", "") for block in content
                            if isinstance(block, dict) and block.get("type") == "text")
        return ""

    def _record_durability_fault(self, message: str) -> None:
        self._durability_fault = message
        self._durability_notice_pending = True
        warning = f"memoryd capture durability fault: {message}"
        logger.warning(warning)
        print(warning, file=sys.stderr)
        if self._spool_store is not None:
            with contextlib.suppress(OSError, JobCollision):
                self._spool_store.set_fault(message)

    def _persist_mutation(self, endpoint: str, body: dict) -> Optional[str]:
        if not self._primary:
            return None
        with self._lifecycle_mutation_lock:
            if self._shutdown_requested.is_set() or self._stop.is_set():
                self._durability_fault = "provider is shut down; mutation was not queued"
                self._durability_notice_pending = True
                logger.warning("memoryd mutation rejected after shutdown: %s", endpoint)
                return None
            if self._spool_store is None:
                self._record_durability_fault("durable spool is unavailable")
                return None
            try:
                job_id = self._spool_store.persist(endpoint, body)
            except (OSError, ValueError, TypeError, JobCollision) as exc:
                self._record_durability_fault(f"could not persist {endpoint}: {exc}")
                return None
            self._wake.set()
            return job_id

    def _recall(self, prompt: str, session_id: str, timeout: float) -> Optional[str]:
        result = self._request_json(
            "/recall", {"prompt": prompt, "session_id": session_id,
                        "project": self._project, "agent": AGENT_NAME}, timeout)
        if result.kind != "success" or result.payload is None:
            return None
        return result.payload.get("markdown", "")

    def _request_json(self, path: str, body: dict, timeout: float) -> HttpResult:
        try:
            request = urllib.request.Request(
                f"{self._url}{path}", data=_canonical_json(body),
                headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(request, timeout=timeout) as response:
                status = getattr(response, "status", response.getcode())
                raw = response.read()
            if not 200 <= status <= 299:
                kind = "retry" if _retryable_status(status) else "permanent"
                return HttpResult(kind, None, f"HTTP {status}")
            if not raw:
                return HttpResult("retry", None, "invalid JSON response: empty body")
            try:
                payload = json.loads(raw)
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                return HttpResult("retry", None, f"invalid JSON response: {exc}")
            if not isinstance(payload, dict):
                return HttpResult("retry", None, "invalid JSON response: expected object")
            return HttpResult("success", payload, "")
        except urllib.error.HTTPError as exc:
            try:
                detail = exc.read().decode("utf-8", "replace")[:2000]
            except Exception:  # noqa: BLE001
                detail = str(exc)
            kind = "retry" if _retryable_status(exc.code) else "permanent"
            return HttpResult(kind, None, f"HTTP {exc.code}: {detail}")
        except (urllib.error.URLError, TimeoutError, OSError, ValueError,
                http.client.HTTPException) as exc:
            kind = "permanent" if isinstance(exc, ValueError) else "retry"
            return HttpResult(kind, None, f"network/configuration failure: {exc}")

    def _send_mutation(self, job: dict) -> HttpResult:
        return self._request_json(job["endpoint"], job["body"], timeout=5.0)

    def _process_one(self) -> bool:
        if self._spool_store is None:
            return False
        try:
            with self._lifecycle_mutation_lock:
                if self._shutdown_requested.is_set() or self._stop.is_set():
                    return False
                claimed = self._spool_store.claim_oldest()
                if claimed is None:
                    fault = self._spool_store.fault()
                    if fault and fault != self._durability_fault:
                        self._durability_fault = fault
                        self._durability_notice_pending = True
                    return False
            path, job = claimed
            with self._lifecycle_mutation_lock:
                if self._shutdown_requested.is_set() or self._stop.is_set():
                    return True
            result = self._send_mutation(job)
            with self._lifecycle_mutation_lock:
                if self._shutdown_requested.is_set() or self._stop.is_set():
                    # Shutdown owns a bounded handoff. Leave claimed evidence for
                    # stale recovery rather than mutating it after shutdown returns.
                    return True
                if result.kind == "success":
                    self._spool_store.complete(path)
                elif result.kind == "permanent":
                    self._spool_store.dead_letter(path, job, result.error)
                    self._durability_notice_pending = True
                    logger.warning("memoryd mutation dead-lettered: %s", result.error)
                    print(f"memoryd mutation dead-lettered: {result.error}", file=sys.stderr)
                else:
                    self._spool_store.retry(path, job, result.error)
            return True
        except (OSError, ValueError, json.JSONDecodeError, JobCollision) as exc:
            self._record_worker_fault(f"spool worker failure: {exc}")
            return False

    def _record_worker_fault(self, message: str) -> None:
        with self._lifecycle_mutation_lock:
            if self._shutdown_requested.is_set() or self._stop.is_set():
                return
            self._record_durability_fault(message)

    def _drain(self) -> None:
        while not self._stop.is_set():
            try:
                if self._process_one():
                    continue
            except Exception as exc:  # noqa: BLE001 - worker must never die silently
                self._record_worker_fault(f"unexpected spool worker failure: {exc}")
            self._wake.wait(0.25)
            self._wake.clear()


def register(ctx) -> None:
    """Entry point called by Hermes plugin discovery."""
    ctx.register_memory_provider(MemorydProvider())
