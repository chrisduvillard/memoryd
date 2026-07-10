from __future__ import annotations

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
_CLAIM_LOCK = threading.Lock()


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


def _blob_state_lock(spool_root: Path):
    from .core import _manifest_file_lock
    lock_anchor = ensure_layout(spool_root)["blobs"] / "state.guard"
    return _manifest_file_lock(lock_anchor)


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
        with _blob_state_lock(spool_root):
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
    with _CLAIM_LOCK:
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
            claim_lock = paths["processing"] / (
                ".claim-" + hashlib.sha256(source.name.encode()).hexdigest())
            try:
                claim_lock.mkdir()
            except FileExistsError:
                continue
            try:
                if target.exists():
                    continue
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
                    return target
            finally:
                claim_lock.rmdir()
    return None


def release_job(spool_root: Path, processing_path: Path, error: str,
                *, delay_s: int) -> Path:
    job = json.loads(processing_path.read_text(encoding="utf-8"))
    sha = job.get("blob_sha256")

    def release() -> Path:
        job["attempts"] = int(job.get("attempts", 0)) + 1
        job["last_error"] = error
        job["next_attempt_at"] = (
            datetime.now(timezone.utc) + timedelta(seconds=delay_s)).isoformat()
        _atomic_json(processing_path, job)
        target = ensure_layout(spool_root)["incoming"] / processing_path.name
        os.replace(processing_path, target)
        return target

    if sha:
        with _blob_state_lock(spool_root):
            return release()
    return release()


def dead_letter(spool_root: Path, job_path: Path, reason: str) -> Path:
    paths = ensure_layout(spool_root)
    target = paths["dead-letter"] / job_path.name
    if target.exists():
        target = target.with_name(
            f"{target.stem}-{secrets.token_hex(4)}{target.suffix}")
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
        target = paths["incoming"] / source.name

        def move_if_stale() -> bool:
            if source.stat().st_mtime >= cutoff:
                return False
            os.replace(source, target)
            return True

        try:
            try:
                job = json.loads(source.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                job = {}
            sha = job.get("blob_sha256") if isinstance(job, dict) else None
            if sha:
                with _blob_state_lock(spool_root):
                    moved += int(move_if_stale())
            else:
                moved += int(move_if_stale())
        except FileNotFoundError:
            continue
    for claim_lock in paths["processing"].glob(".claim-*"):
        try:
            if claim_lock.stat().st_mtime < cutoff:
                claim_lock.rmdir()
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
        with _blob_state_lock(spool_root):
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
                    continue
            blob = paths["blobs"] / sha
            blob.unlink(missing_ok=True)
            return True
    except OSError:
        return False
