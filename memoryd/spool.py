from __future__ import annotations

import contextlib
import errno
import hashlib
import json
import os
import secrets
import stat
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import BinaryIO

SCHEMA_VERSION = 2
BUFFER_BYTES = 1024 * 1024
DEAD_LETTER_REASON_FIELDS = frozenset({
    "dead_lettered_at", "reason", "manifest"})
_STATE_THREAD_LOCK = threading.RLock()


class SpoolError(RuntimeError):
    pass


class PermanentSpoolError(SpoolError):
    pass


class JobIdentityCollision(PermanentSpoolError):
    pass


def _require_nonempty_string(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PermanentSpoolError(f"invalid {field}: expected nonempty string")
    return value


def _require_nonnegative_int(value: object, field: str) -> int:
    if type(value) is not int or value < 0:
        raise PermanentSpoolError(f"invalid {field}: expected nonnegative integer")
    return value


def validate_job(value: object) -> dict:
    if not isinstance(value, dict):
        raise PermanentSpoolError("invalid job manifest: expected object")
    if (type(value.get("schema_version")) is not int or
            value["schema_version"] != SCHEMA_VERSION):
        raise PermanentSpoolError(
            f"invalid schema_version: expected integer {SCHEMA_VERSION}")
    for field in ("job_id", "kind", "created_at", "session_id"):
        if field not in value:
            raise PermanentSpoolError(f"missing manifest field: {field}")
        _require_nonempty_string(value[field], field)
    kind = value["kind"]
    if kind not in ("capture_snapshot", "extraction"):
        raise PermanentSpoolError(f"invalid kind: {kind!r}")
    _require_nonnegative_int(value.get("attempts", 0), "attempts")
    last_error = value.get("last_error")
    if last_error is not None and not isinstance(last_error, str):
        raise PermanentSpoolError("invalid last_error: expected string or null")
    next_attempt = value.get("next_attempt_at")
    if next_attempt is not None:
        _require_nonempty_string(next_attempt, "next_attempt_at")
    project = value.get("project")
    if project is not None:
        _require_nonempty_string(project, "project")

    if kind == "capture_snapshot":
        for field in ("trigger", "original_transcript_path", "blob_sha256"):
            if field not in value:
                raise PermanentSpoolError(f"missing manifest field: {field}")
            _require_nonempty_string(value[field], field)
        sha = value["blob_sha256"]
        if len(sha) != 64 or any(c not in "0123456789abcdef" for c in sha):
            raise PermanentSpoolError("invalid blob_sha256: expected lowercase SHA-256")
        if "blob_bytes" not in value:
            raise PermanentSpoolError("missing manifest field: blob_bytes")
        _require_nonnegative_int(value["blob_bytes"], "blob_bytes")
    request_endpoint = value.get("request_endpoint")
    request_digest = value.get("request_body_sha256")
    if (request_endpoint is None) != (request_digest is None):
        raise PermanentSpoolError(
            "request_endpoint and request_body_sha256 must appear together")
    if request_endpoint is not None:
        _require_nonempty_string(request_endpoint, "request_endpoint")
        _require_nonempty_string(request_digest, "request_body_sha256")
        if (len(request_digest) != 64 or
                any(c not in "0123456789abcdef" for c in request_digest)):
            raise PermanentSpoolError(
                "invalid request_body_sha256: expected lowercase SHA-256")
    return value


def validate_legacy_job(value: object) -> dict:
    if not isinstance(value, dict):
        raise PermanentSpoolError("invalid legacy manifest: expected object")
    if "transcript_path" not in value:
        raise PermanentSpoolError("invalid transcript_path: missing field")
    _require_nonempty_string(value["transcript_path"], "transcript_path")
    for field in ("session_id", "trigger"):
        if field in value:
            _require_nonempty_string(value[field], field)
    project = value.get("project")
    if project is not None:
        _require_nonempty_string(project, "project")
    return value


def ensure_layout(spool_root: Path) -> dict[str, Path]:
    paths = {name: spool_root / name for name in
             ("blobs", "incoming", "processing", "dead-letter",
              "request-identities")}
    _mkdir_durable(spool_root)
    _require_plain_directory(spool_root)
    for path in paths.values():
        _mkdir_durable(path)
        _require_plain_directory(path)
    return paths


def _collision_safe_target(directory: Path, name: str,
                           *, reserve_reason: bool = False) -> Path:
    target = directory / name
    reason_path = dead_letter_reason_path(target) if reserve_reason else None
    while (os.path.lexists(target) or
           (reason_path is not None and os.path.lexists(reason_path))):
        target = target.with_name(
            f"{target.stem}-{secrets.token_hex(4)}{target.suffix}")
        reason_path = dead_letter_reason_path(target) if reserve_reason else None
    return target


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


def _redirected(path: Path, path_stat: os.stat_result) -> bool:
    if stat.S_ISLNK(path_stat.st_mode):
        return True
    is_junction = getattr(path, "is_junction", None)
    if is_junction is not None:
        try:
            if is_junction():
                return True
        except OSError:
            return True
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return bool(getattr(path_stat, "st_file_attributes", 0) & reparse)


def _same_regular_identity(
        left: os.stat_result, right: os.stat_result) -> bool:
    return (
        stat.S_ISREG(left.st_mode) and stat.S_ISREG(right.st_mode) and
        (left.st_dev, left.st_ino, left.st_size, left.st_mtime_ns) ==
        (right.st_dev, right.st_ino, right.st_size, right.st_mtime_ns)
    )


def _require_plain_directory(path: Path) -> None:
    try:
        path_stat = path.stat(follow_symlinks=False)
    except OSError as exc:
        raise PermanentSpoolError(
            f"untrusted directory: {path}: {exc}") from exc
    if (_redirected(path, path_stat) or
            not stat.S_ISDIR(path_stat.st_mode)):
        raise PermanentSpoolError(f"untrusted directory: {path}")


def _require_canonical_archive_namespace(path: Path, sha: str) -> None:
    parent = path.parent
    shard_a = parent.parent
    sha_root = shard_a.parent
    objects = sha_root.parent
    archive = objects.parent
    shaped = (
        path.name == sha and parent.name == sha[2:4] and
        shard_a.name == sha[:2] and sha_root.name == "sha256" and
        objects.name == "objects"
    )
    directories = (
        (archive, objects, sha_root, shard_a, parent)
        if shaped else (parent,)
    )
    for directory in directories:
        _require_plain_directory(directory)


def _read_verified_file(path: Path, sha: str,
                        expected_bytes: int | None) -> bytes:
    try:
        before = path.stat(follow_symlinks=False)
        if (_redirected(path, before) or
                not stat.S_ISREG(before.st_mode)):
            raise PermanentSpoolError(f"redirected or non-regular file: {path}")
        if expected_bytes is not None and before.st_size != expected_bytes:
            raise PermanentSpoolError(f"file size mismatch: {path}")
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = os.open(path, flags)
        with os.fdopen(fd, "rb") as handle:
            opened = os.fstat(handle.fileno())
            if not _same_regular_identity(before, opened):
                raise PermanentSpoolError(f"file changed before open: {path}")
            data = handle.read()
            opened_after = os.fstat(handle.fileno())
            if not _same_regular_identity(opened, opened_after):
                raise PermanentSpoolError(f"file changed while reading: {path}")
        after = path.stat(follow_symlinks=False)
        if (_redirected(path, after) or
                not _same_regular_identity(opened_after, after)):
            raise PermanentSpoolError(f"file path changed while reading: {path}")
    except PermanentSpoolError:
        raise
    except OSError as exc:
        raise PermanentSpoolError(f"unreadable file: {path}: {exc}") from exc
    if hashlib.sha256(data).hexdigest() != sha:
        raise PermanentSpoolError(f"file checksum mismatch: {path}")
    return data


def _directory_fsync_unsupported(exc: OSError) -> bool:
    unsupported = {
        errno.EINVAL,
        getattr(errno, "ENOTSUP", errno.EINVAL),
        getattr(errno, "EOPNOTSUPP", errno.EINVAL),
    }
    if os.name == "nt":
        unsupported.update({errno.EACCES, errno.EPERM, errno.EBADF})
    return exc.errno in unsupported


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        if _directory_fsync_unsupported(exc):
            return
        raise
    try:
        try:
            os.fsync(fd)
        except OSError as exc:
            if not _directory_fsync_unsupported(exc):
                raise
    finally:
        os.close(fd)


def _mkdir_durable(path: Path) -> None:
    missing: list[Path] = []
    current = path
    while not os.path.lexists(current):
        missing.append(current)
        parent = current.parent
        if parent == current:
            break
        current = parent
    if not missing:
        _require_plain_directory(path)
        _fsync_directory(path.parent)
        return
    for directory in reversed(missing):
        try:
            directory.mkdir()
        except FileExistsError:
            path_stat = directory.stat(follow_symlinks=False)
            if (_redirected(directory, path_stat) or
                    not stat.S_ISDIR(path_stat.st_mode)):
                raise PermanentSpoolError(
                    f"untrusted spool directory: {directory}")
            _fsync_directory(directory.parent)
        else:
            _fsync_directory(directory.parent)


def _atomic_json(path: Path, value: dict) -> None:
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    try:
        with tmp.open("x", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        _fsync_directory(path.parent)
    finally:
        tmp.unlink(missing_ok=True)


def _fsync_file(path: Path) -> None:
    before = path.stat(follow_symlinks=False)
    if (_redirected(path, before) or not stat.S_ISREG(before.st_mode)):
        raise PermanentSpoolError(f"untrusted file: {path}")
    flags = os.O_RDWR | getattr(os, "O_BINARY", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags)
    try:
        opened = os.fstat(fd)
        if not _same_regular_identity(before, opened):
            raise PermanentSpoolError(f"file changed before fsync: {path}")
        os.fsync(fd)
    finally:
        os.close(fd)


def _sync_replaced_path(source: Path, target: Path) -> None:
    _fsync_directory(target.parent)
    if source.parent != target.parent:
        _fsync_directory(source.parent)


def _durable_replace(source: Path, target: Path) -> None:
    os.replace(source, target)
    _sync_replaced_path(source, target)


def _durable_unlink(path: Path) -> None:
    path.unlink()
    _fsync_directory(path.parent)


def _durable_touch(path: Path) -> None:
    os.utime(path, None)
    _fsync_file(path)


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
    if not isinstance(transcript_path, Path):
        raise PermanentSpoolError("invalid transcript_path: expected Path")
    _require_nonempty_string(session_id, "session_id")
    _require_nonempty_string(trigger, "trigger")
    if project is not None:
        _require_nonempty_string(project, "project")
    paths = ensure_layout(spool_root)
    source = transcript_path.expanduser()
    if not source.is_file():
        raise PermanentSpoolError(f"transcript not found: {source}")
    job_id = _job_id()
    tmp_blob = paths["blobs"] / f".{job_id}.tmp"
    remove_tmp = True
    try:
        with source.open("rb") as src, tmp_blob.open("xb") as dst:
            sha, size = _copy_and_hash(src, dst)
            dst.flush()
            os.fsync(dst.fileno())

        def preserve_temporary() -> Path:
            nonlocal remove_tmp
            evidence = paths["blobs"] / f".collision.{sha}.{job_id}"
            try:
                os.replace(tmp_blob, evidence)
            except OSError:
                # The fsynced temporary file remains durable evidence.
                remove_tmp = False
                return tmp_blob
            _fsync_directory(paths["blobs"])
            return evidence

        with _state_lock(spool_root):
            blob = paths["blobs"] / sha
            try:
                try:
                    os.link(tmp_blob, blob)
                except OSError as exc:
                    if exc.errno not in (
                            errno.EEXIST, errno.EACCES, errno.EPERM):
                        raise
                    _read_verified_file(blob, sha, size)
                _fsync_directory(paths["blobs"])
                _read_verified_file(blob, sha, size)
            except PermanentSpoolError as collision:
                preserved_at = preserve_temporary()
                raise PermanentSpoolError(
                    f"invalid spool blob collision for {sha}; "
                    f"capture bytes preserved at {preserved_at}") from collision
            except OSError:
                preserve_temporary()
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
            validate_job(job)
            _atomic_json(paths["incoming"] / f"{job_id}.json", job)
        return job
    finally:
        if remove_tmp:
            tmp_blob.unlink(missing_ok=True)


def enqueue_extraction(*, spool_root: Path, session_id: str,
                       request_id: str | None = None,
                       request_endpoint: str | None = None,
                       request_body_sha256: str | None = None) -> dict:
    _require_nonempty_string(session_id, "session_id")
    paths = ensure_layout(spool_root)
    if request_id is not None:
        _require_nonempty_string(request_id, "request_id")
        _require_nonempty_string(request_endpoint, "request_endpoint")
        _require_nonempty_string(request_body_sha256, "request_body_sha256")
        if (len(request_body_sha256) != 64 or
                any(c not in "0123456789abcdef"
                    for c in request_body_sha256)):
            raise PermanentSpoolError(
                "invalid request_body_sha256: expected lowercase SHA-256")
    elif request_endpoint is not None or request_body_sha256 is not None:
        raise PermanentSpoolError(
            "request metadata requires request_id")
    job_id = request_id or _job_id()
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
    if request_id is not None:
        job["request_endpoint"] = request_endpoint
        job["request_body_sha256"] = request_body_sha256
    validate_job(job)
    with _state_lock(spool_root):
        if request_id is not None:
            existing = _find_request_identity_unlocked(
                spool_root, paths, request_id)
            if existing is not None:
                endpoint, digest, manifest = existing
                if (manifest.get("kind") == "extraction" and
                        endpoint == request_endpoint and
                        digest == request_body_sha256):
                    return {**manifest, "duplicate": True}
                raise JobIdentityCollision("request_id collision")
            filename = hashlib.sha256(request_id.encode()).hexdigest() + ".json"
            target = paths["incoming"] / filename
            if os.path.lexists(target):
                raise JobIdentityCollision("request_id collision")
        else:
            target = paths["incoming"] / f"{job_id}.json"
        _atomic_json(target, job)
    return {**job, "duplicate": False}


def _find_request_identity_unlocked(
        spool_root: Path, paths: dict[str, Path],
        request_id: str) -> tuple[str, str, dict] | None:
    candidates = [*spool_root.glob("*.json")]
    for state in (
            "incoming", "processing", "dead-letter",
            "request-identities"):
        candidates.extend(paths[state].glob("*.json"))
    found = None
    for candidate in candidates:
        try:
            manifest = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if (not isinstance(manifest, dict) or
                manifest.get("job_id") != request_id):
            continue
        endpoint = manifest.get("request_endpoint")
        digest = manifest.get("request_body_sha256")
        if not isinstance(endpoint, str) or not isinstance(digest, str):
            raise JobIdentityCollision("request_id collision")
        identity = (endpoint, digest, manifest)
        if found is not None and found[:2] != identity[:2]:
            raise JobIdentityCollision("request_id collision")
        found = identity
    return found


def find_request_identity(
        spool_root: Path, request_id: str) -> tuple[str, str] | None:
    """Return the durable extraction request identity across every job state."""
    _require_nonempty_string(request_id, "request_id")
    with _state_lock(spool_root):
        paths = ensure_layout(spool_root)
        found = _find_request_identity_unlocked(
            spool_root, paths, request_id)
        return None if found is None else found[:2]


def load_job(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise PermanentSpoolError(f"invalid job manifest: {exc}") from exc
    return validate_job(value)


def validate_blob(spool_root: Path, job: dict) -> Path:
    read_validated_blob(spool_root, job)
    return spool_root / "blobs" / job["blob_sha256"]


def read_validated_blob(spool_root: Path, job: dict) -> bytes:
    job = validate_job(job)
    if job["kind"] != "capture_snapshot":
        raise PermanentSpoolError("invalid kind: blob validation requires capture")
    sha = job["blob_sha256"]
    _require_plain_directory(spool_root)
    blob_root = spool_root / "blobs"
    _require_plain_directory(blob_root)
    try:
        return _read_verified_file(blob_root / sha, sha, job["blob_bytes"])
    except PermanentSpoolError as exc:
        raise PermanentSpoolError(f"invalid spool blob {sha}: {exc}") from exc


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
            target = _collision_safe_target(paths["processing"], source.name)
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
                _sync_replaced_path(source, target)
                _durable_touch(target)
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
        target = _collision_safe_target(
            ensure_layout(spool_root)["incoming"], processing_path.name)
        _durable_replace(processing_path, target)
        return target


def dead_letter_reason_path(manifest_path: Path) -> Path:
    reason_path = manifest_path.with_suffix(".reason.json")
    if reason_path == manifest_path:
        reason_path = manifest_path.with_name(
            manifest_path.name + ".reason.json")
    return reason_path


def is_dead_letter_sidecar(path: Path) -> bool:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if (not isinstance(value, dict) or
            set(value) != DEAD_LETTER_REASON_FIELDS):
        return False
    manifest_name = value.get("manifest")
    reason = value.get("reason")
    dead_lettered_at = value.get("dead_lettered_at")
    if (not isinstance(manifest_name, str) or not manifest_name or
            Path(manifest_name).name != manifest_name or
            not isinstance(reason, str) or
            not isinstance(dead_lettered_at, str) or not dead_lettered_at):
        return False
    manifest_path = path.parent / manifest_name
    return (manifest_path != path and manifest_path.is_file() and
            dead_letter_reason_path(manifest_path) == path)


def dead_letter(spool_root: Path, job_path: Path, reason: str) -> Path:
    with _state_lock(spool_root):
        paths = ensure_layout(spool_root)
        target = _collision_safe_target(
            paths["dead-letter"], job_path.name, reserve_reason=True)
        reason_path = dead_letter_reason_path(target)
        _atomic_json(reason_path, {
            "dead_lettered_at": datetime.now(timezone.utc).isoformat(),
            "reason": reason,
            "manifest": target.name,
        })
        _durable_replace(job_path, target)
        return target


def complete_job(job_path: Path) -> None:
    with _state_lock(job_path.parent.parent):
        job = json.loads(job_path.read_text(encoding="utf-8"))
        if (isinstance(job, dict) and
                job.get("request_endpoint") is not None and
                job.get("request_body_sha256") is not None):
            completed = ensure_layout(job_path.parent.parent)[
                "request-identities"] / job_path.name
            _durable_replace(job_path, completed)
        else:
            _durable_unlink(job_path)


def requeue_stale(spool_root: Path, *, stale_after_s: int = 900) -> int:
    with _state_lock(spool_root):
        paths = ensure_layout(spool_root)
        cutoff = time.time() - stale_after_s
        moved = 0
        for source in paths["processing"].glob("*.json"):
            try:
                modified = source.stat().st_mtime
            except FileNotFoundError:
                continue
            if modified >= cutoff:
                continue
            target = _collision_safe_target(paths["incoming"], source.name)
            try:
                os.replace(source, target)
            except FileNotFoundError:
                continue
            _sync_replaced_path(source, target)
            moved += 1
        return moved


def upgrade_legacy_job(spool_root: Path, legacy_path: Path) -> Path | None:
    try:
        value = json.loads(legacy_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise PermanentSpoolError(f"invalid legacy manifest: {exc}") from exc
    job = validate_legacy_job(value)
    source = Path(job["transcript_path"]).expanduser()
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


def _candidate_blob_sha(value: object) -> str | None:
    if not isinstance(value, dict):
        raise PermanentSpoolError("invalid candidate manifest: expected object")
    if "schema_version" in value:
        job = validate_job(value)
        return (job["blob_sha256"]
                if job["kind"] == "capture_snapshot" else None)
    if value.get("extract_only") is True:
        _require_nonempty_string(value.get("session_id"), "session_id")
        _require_nonnegative_int(value.get("attempts", 0), "attempts")
        return None
    if "transcript_path" in value:
        job = validate_legacy_job(value)
        legacy_sha = job.get("blob_sha256")
        if legacy_sha is None:
            return None
        _require_nonempty_string(legacy_sha, "blob_sha256")
        if (len(legacy_sha) != 64 or
                any(c not in "0123456789abcdef" for c in legacy_sha)):
            raise PermanentSpoolError("invalid blob_sha256 in legacy manifest")
        return legacy_sha
    raise PermanentSpoolError("unrecognized candidate manifest")


def gc_blob_if_unreferenced(spool_root: Path, sha: str,
                            canonical_object: Path) -> bool:
    expected_layout = [
        spool_root,
        *(spool_root / name for name in
          ("blobs", "incoming", "processing", "dead-letter")),
    ]
    try:
        _require_canonical_archive_namespace(canonical_object, sha)
        _read_verified_file(canonical_object, sha, None)
        for directory in expected_layout:
            _require_plain_directory(directory)
    except PermanentSpoolError:
        return False

    try:
        with _state_lock(spool_root):
            paths = ensure_layout(spool_root)
            for directory in (spool_root, *paths.values()):
                _require_plain_directory(directory)
            manifests = [*spool_root.glob("*.json")]
            for state in ("incoming", "processing", "dead-letter"):
                manifests.extend(paths[state].glob("*.json"))
            for manifest in manifests:
                if (manifest.parent == paths["dead-letter"] and
                        is_dead_letter_sidecar(manifest)):
                    continue
                try:
                    value = json.loads(manifest.read_text())
                    if _candidate_blob_sha(value) == sha:
                        return False
                except (OSError, ValueError, PermanentSpoolError):
                    return False
            blob = paths["blobs"] / sha
            _require_plain_directory(paths["blobs"])
            try:
                _read_verified_file(blob, sha, None)
            except PermanentSpoolError:
                return False
            try:
                _require_canonical_archive_namespace(canonical_object, sha)
                _read_verified_file(canonical_object, sha, None)
            except PermanentSpoolError:
                return False
            before_delete = blob.stat(follow_symlinks=False)
            if (_redirected(blob, before_delete) or
                    not stat.S_ISREG(before_delete.st_mode)):
                return False
            _durable_unlink(blob)
            return True
    except (OSError, PermanentSpoolError):
        return False
