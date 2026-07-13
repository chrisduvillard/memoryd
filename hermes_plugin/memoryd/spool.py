"""Crash-durable filesystem spool for the Hermes memoryd provider."""
from __future__ import annotations

import contextlib
import hashlib
import json
import math
import os
import re
import time
import uuid
from pathlib import Path
from typing import Callable, Iterator, NamedTuple, Optional

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
        _private_dir(self.root / "identity")

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

    def _identity_path(self, job_id: str) -> Path:
        key = hashlib.sha256(job_id.encode("utf-8")).hexdigest()
        return self.root / "identity" / f"{key}.json"

    def _reserve_identity_unlocked(self, job: dict, filename: str) -> None:
        reservation = self._identity_path(job["job_id"])
        if reservation.exists():
            raise JobCollision(f"durable job collision: {job['job_id']}")
        _atomic_json(reservation, {
            "schema_version": SCHEMA_VERSION,
            "job_id": job["job_id"],
            "filename": filename,
            "endpoint": job["endpoint"],
            "body_sha256": job["body_sha256"],
            "created_at": job["created_at"],
        }, replace=False)

    def _last_order_unlocked(self) -> int:
        sequence_path = self.root / "sequence.json"
        if not sequence_path.exists():
            return -1
        value = self._read(sequence_path)
        stored = value.get("last_order")
        if type(stored) is not int or stored < 0:
            raise ValueError("invalid spool sequence state")
        return stored

    def _advance_sequence_unlocked(self, order: int) -> None:
        if order > self._last_order_unlocked():
            _atomic_json(self.root / "sequence.json", {"last_order": order},
                         replace=True)

    def _next_order_unlocked(self, now: float) -> int:
        last_order = self._last_order_unlocked()
        sequence_path = self.root / "sequence.json"
        order = max(0, int(now * 1_000_000_000), last_order + 1)
        _atomic_json(sequence_path, {"last_order": order}, replace=True)
        return order

    def rebuild_identity_reservations(self) -> int:
        """Index legacy filenames once at startup without parsing job bodies."""
        created = 0
        max_order = -1
        with self._lock():
            for path in self._all_paths_unlocked():
                match = JOB_FILE_RE.fullmatch(path.name)
                if match is None:
                    continue
                max_order = max(max_order, int(match.group("order")))
                job_id = match.group("job_id")
                reservation = self._identity_path(job_id)
                if reservation.exists():
                    continue
                _atomic_json(reservation, {
                    "schema_version": SCHEMA_VERSION,
                    "job_id": job_id,
                    "filename": path.name,
                    "legacy": True,
                }, replace=False)
                created += 1
            if max_order >= 0:
                self._advance_sequence_unlocked(max_order)
        return created

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
            order = self._next_order_unlocked(now)
            filename = f"{order:020d}-{job_id}.json"
            self._reserve_identity_unlocked(job, filename)
            path = self._dir("incoming") / filename
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

    def release(self, path: Path, job: dict) -> None:
        """Return an active shutdown claim for immediate idempotent replay."""
        with self._lock():
            if not path.exists():
                raise FileNotFoundError(path)
            current = self._read(path)
            self._validate_job(current, path, "processing")
            updated = dict(current)
            updated["last_error"] = "shutdown handback; replay immediately"
            updated["next_attempt_at"] = max(self.clock(), updated["created_at"])
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
