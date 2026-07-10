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
