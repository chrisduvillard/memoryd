from __future__ import annotations

import contextlib
import errno
import hashlib
import json
import os
import secrets
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import BinaryIO

SCHEMA_VERSION = 2
BUFFER_BYTES = 1024 * 1024
_STATE_THREAD_LOCK = threading.RLock()


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


@contextlib.contextmanager
def _state_lock(spool_root: Path):
    ensure_layout(spool_root)
    lock_path = spool_root / "state.lock"
    deadline = time.monotonic() + 5
    fd = None
    last_error = None
    with _STATE_THREAD_LOCK:
        while fd is None:
            candidate = None
            try:
                candidate = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
                if os.name == "nt":
                    import msvcrt
                    if os.fstat(candidate).st_size == 0:
                        os.write(candidate, b"\0")
                        os.fsync(candidate)
                    os.lseek(candidate, 0, os.SEEK_SET)
                    msvcrt.locking(candidate, msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(candidate, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except PermissionError as error:
                last_error = error
            except OSError as error:
                if error.errno not in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                    if candidate is not None:
                        os.close(candidate)
                    raise
                last_error = error
            else:
                fd = candidate
                break
            if candidate is not None:
                os.close(candidate)
            if time.monotonic() >= deadline:
                raise TimeoutError(f"state lock timeout: {lock_path}") from last_error
            time.sleep(0.01)
        try:
            yield
        finally:
            try:
                if os.name == "nt":
                    import msvcrt
                    os.lseek(fd, 0, os.SEEK_SET)
                    msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)


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
        with _state_lock(spool_root):
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


def enqueue_extraction(*, spool_root: Path, session_id: str) -> dict:
    paths = ensure_layout(spool_root)
    job_id = _job_id()
    job = {
        "schema_version": SCHEMA_VERSION,
        "job_id": job_id,
        "kind": "extraction",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "session_id": session_id,
        "attempts": 0,
        "last_error": None,
        "next_attempt_at": None,
    }
    with _state_lock(spool_root):
        _atomic_json(paths["incoming"] / f"{job_id}.json", job)
    return job


def load_job(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise PermanentSpoolError(f"invalid job manifest: {exc}") from exc
    if not isinstance(value, dict):
        raise PermanentSpoolError("invalid job manifest: expected object")
    if value.get("schema_version") != SCHEMA_VERSION:
        raise PermanentSpoolError("unsupported job schema")
    required = {"job_id", "kind", "session_id"}
    missing = required - value.keys()
    if missing:
        raise PermanentSpoolError(f"missing manifest fields: {sorted(missing)}")
    if not str(value["session_id"]):
        raise PermanentSpoolError("invalid job identity")
    if value["kind"] == "capture_snapshot":
        capture_required = {"trigger", "blob_sha256", "blob_bytes"}
        missing = capture_required - value.keys()
        if missing:
            raise PermanentSpoolError(
                f"missing manifest fields: {sorted(missing)}")
        if not isinstance(value["blob_bytes"], int) or value["blob_bytes"] < 0:
            raise PermanentSpoolError("invalid blob byte count")
    elif value["kind"] != "extraction":
        raise PermanentSpoolError("unsupported job kind")
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


def _scheduled(job: object) -> bool:
    if not isinstance(job, dict):
        return False
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
    with _state_lock(spool_root):
        paths = ensure_layout(spool_root)
        sources = sorted([*spool_root.glob("*.json"),
                          *paths["incoming"].glob("*.json")])
        for source in sources:
            try:
                job = json.loads(source.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                job = {}
            if not ignore_schedule and _scheduled(job):
                continue
            target = paths["processing"] / source.name
            if target.exists():
                continue
            os.utime(source, None)
            deadline = time.monotonic() + 5
            while True:
                try:
                    os.replace(source, target)
                except FileNotFoundError:
                    break
                except PermissionError as exc:
                    if not source.exists() or target.exists():
                        break
                    if time.monotonic() >= deadline:
                        raise exc
                    time.sleep(0.005)
                    continue
                os.utime(target, None)
                return target
    return None


def release_job(spool_root: Path, processing_path: Path, error: str,
                *, delay_s: int) -> Path:
    with _state_lock(spool_root):
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
    with _state_lock(spool_root):
        paths = ensure_layout(spool_root)
        target = paths["dead-letter"] / job_path.name
        reason_path = target.with_suffix(".reason.json")
        while target.exists() or reason_path.exists():
            target = target.with_name(
                f"{target.stem}-{secrets.token_hex(4)}{target.suffix}")
            reason_path = target.with_suffix(".reason.json")
        _atomic_json(reason_path, {
            "dead_lettered_at": datetime.now(timezone.utc).isoformat(),
            "reason": reason,
            "manifest": target.name,
        })
        os.replace(job_path, target)
        return target


def complete_job(job_path: Path) -> None:
    with _state_lock(job_path.parent.parent):
        job_path.unlink()


def requeue_stale(spool_root: Path, *, stale_after_s: int = 900) -> int:
    with _state_lock(spool_root):
        paths = ensure_layout(spool_root)
        cutoff = time.time() - stale_after_s
        moved = 0
        for source in paths["processing"].glob("*.json"):
            target = paths["incoming"] / source.name
            try:
                if source.stat().st_mtime >= cutoff:
                    continue
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
    digest = hashlib.sha256()
    try:
        with canonical_object.open("rb") as handle:
            while chunk := handle.read(BUFFER_BYTES):
                digest.update(chunk)
    except OSError:
        return False
    if digest.hexdigest() != sha:
        return False

    try:
        with _state_lock(spool_root):
            paths = ensure_layout(spool_root)
            manifests = [*spool_root.glob("*.json")]
            for state in ("incoming", "processing", "dead-letter"):
                manifests.extend(
                    path for path in paths[state].glob("*.json")
                    if not path.name.endswith(".reason.json"))
            for manifest in manifests:
                try:
                    value = json.loads(manifest.read_text())
                    if (isinstance(value, dict) and
                            value.get("blob_sha256") == sha):
                        return False
                except (OSError, ValueError):
                    return False
                if not isinstance(value, dict):
                    return False
            blob = paths["blobs"] / sha
            blob.unlink(missing_ok=True)
            return True
    except OSError:
        return False
