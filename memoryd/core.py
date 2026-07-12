"""memoryd core: config, ids, content-addressed archive, ledger writer.

Slice architecture v1 — M1/M2. Raw archival is unconditional and never
blocks on anything downstream (spec §4.3).
"""
from __future__ import annotations

import contextlib
import errno
import hashlib
import json
import os
import secrets
import shutil
import stat
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath, PureWindowsPath

import psycopg
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

# ----------------------------------------------------------------- config

def _file_cfg() -> dict:
    """~/memory/config.json, written by `memoryd install`.

    Precedence everywhere is env > config.json > default — scheduled tasks
    (schtasks/systemd/launchd) inherit no shell exports, so the file is what
    makes autostarted daemons find the right DB. The file's *location* honors
    MEMORYD_HOME env only; a `home` key inside it relocates data, not the file.
    """
    p = Path(os.environ.get("MEMORYD_HOME", "~/memory")).expanduser() / "config.json"
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


_FILE_CFG = _file_cfg()
# persisted env (e.g. ANTHROPIC_API_KEY) for scheduled runs; real env wins
for _k, _v in (_FILE_CFG.get("env") or {}).items():
    os.environ.setdefault(_k, str(_v))


def _get(env: str, key: str, default: str) -> str:
    return os.environ.get(env) or str(_FILE_CFG.get(key) or "") or default


@dataclass
class Config:
    dsn: str = _get("MEMORYD_DSN", "dsn", "postgresql://memoryd@localhost/memoryd")
    home: Path = field(default_factory=lambda: Path(
        os.environ.get("MEMORYD_HOME") or _FILE_CFG.get("home") or "~/memory").expanduser())
    port: int = int(_get("MEMORYD_PORT", "port", "7437"))
    packet_token_budget: int = int(_get("MEMORYD_PACKET_TOKENS", "packet_tokens", "1500"))
    model_profile: str = _get("MEMORYD_MODEL_PROFILE", "model_profile", "")
    extractor_contract: str = _get("MEMORYD_EXTRACTOR_CONTRACT", "extractor_contract", "builtin_v1")
    semantic_policy: str = _get("MEMORYD_SEMANTIC_POLICY", "semantic_policy", "conservative_v1")
    recall_policy: str = _get("MEMORYD_RECALL_POLICY", "recall_policy", "heuristic_v1")
    packet_compiler: str = _get("MEMORYD_PACKET_COMPILER", "packet_compiler", "lane_v1")
    eval_profile: str = _get("MEMORYD_EVAL_PROFILE", "eval_profile", "default_v1")
    # per-agent memory visas (spec §6, governance). Override with
    # MEMORYD_VISAS='{"hermes": ["work_private","public"], ...}' or a
    # "visas" object in config.json.
    default_scopes: tuple[str, ...] = ("work_private", "project_shared", "public")

    def visa(self, agent: str) -> list[str]:
        visas = None
        raw = os.environ.get("MEMORYD_VISAS", "")
        if raw:
            try:
                visas = json.loads(raw)
            except json.JSONDecodeError:
                visas = None
        if visas is None:
            visas = _FILE_CFG.get("visas")
        if isinstance(visas, dict):
            if agent in visas:
                return list(visas[agent])
            if "*" in visas:
                return list(visas["*"])
        return list(self.default_scopes)

    @property
    def archive(self) -> Path:
        return self.home / "archive"

    @property
    def spool(self) -> Path:
        return self.home / "spool"

    def ensure_dirs(self) -> None:
        _mkdir_durable(self.archive / "objects" / "sha256")
        _mkdir_durable(self.archive / "fonds")
        _mkdir_durable(self.spool)
        _mkdir_durable(self.home / "digest")


CFG = Config()
POOL: ConnectionPool | None = None
_POOL_LOCK = threading.Lock()


class ArchiveOccurrenceCollision(ValueError):
    pass


def pool() -> ConnectionPool:
    global POOL
    if POOL is None:
        with _POOL_LOCK:  # HTTP handler threads race the capture worker on first use
            if POOL is None:
                from psycopg.rows import tuple_row

                def _reset(conn: psycopg.Connection) -> None:
                    # handlers may set dict_row for their checkout; never let that
                    # leak to the next borrower of the pooled connection
                    conn.row_factory = tuple_row

                # timeout=5: while the DB is down (e.g. Docker still booting),
                # fail requests fast instead of parking threads for 30s — the
                # recall hook gave up at 1.5s anyway.
                POOL = ConnectionPool(CFG.dsn, min_size=1, max_size=8, open=True,
                                      timeout=5, reset=_reset)
                # close at exit: Python 3.14 raises PythonFinalizationError
                # when joining the pool's worker threads at shutdown
                import atexit
                atexit.register(POOL.close)
    return POOL

# ----------------------------------------------------------------- ids

_ULID_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"  # Crockford base32


def ulid() -> str:
    """Compact dependency-free ULID: 48-bit ms timestamp + 80-bit randomness."""
    ts = int(time.time() * 1000)
    rand = secrets.randbits(80)
    n = (ts << 80) | rand
    chars = []
    for _ in range(26):
        chars.append(_ULID_ALPHABET[n & 0x1F])
        n >>= 5
    return "".join(reversed(chars))


def new_id(prefix: str) -> str:
    return f"{prefix}_{ulid()}"


def barcode(ts: datetime, session_id: str, kind: str, content_hash: str) -> str:
    """Episodic barcode: 'this exact episode', distinct from semantic hash (spec A3)."""
    return f"{ts.strftime('%Y%m%dT%H%M%S')}|{session_id[:8]}|{kind}|{content_hash[:8]}"

# ----------------------------------------------------------------- archive (Fonds Keeper)

def validate_fonds_path(archive_root: Path, fonds_path: str) -> Path:
    if not fonds_path or PureWindowsPath(fonds_path).drive:
        raise ValueError(f"unsafe fonds path: {fonds_path!r}")
    normalized = fonds_path.replace("\\", "/")
    rel = PurePosixPath(normalized)
    parts = normalized.split("/")
    if rel.is_absolute() or any(part in ("", ".", "..") for part in parts):
        raise ValueError(f"unsafe fonds path: {fonds_path!r}")
    trusted_root = archive_root.resolve()
    fonds_root = trusted_root / "fonds"
    if os.path.lexists(fonds_root):
        root_stat = fonds_root.stat(follow_symlinks=False)
        if (not stat.S_ISDIR(root_stat.st_mode) or
                fonds_root.resolve() != fonds_root):
            raise ValueError(f"unsafe fonds root: {fonds_root}")
    target = fonds_root / Path(*parts)
    if not target.parent.resolve().is_relative_to(fonds_root):
        raise ValueError(f"fonds path escapes archive: {fonds_path!r}")
    return target


@contextlib.contextmanager
def _manifest_file_lock(manifest: Path):
    lock = manifest.with_suffix(".lock")
    deadline = time.monotonic() + 5
    fd = None
    last_error = None
    while fd is None:
        candidate = None
        try:
            candidate = os.open(lock, os.O_CREAT | os.O_RDWR, 0o600)
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
            raise TimeoutError(f"manifest lock timeout: {lock}") from last_error
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


def _redirected_directory(path: Path, path_stat: os.stat_result) -> bool:
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


def _require_plain_directory(path: Path) -> None:
    path_stat = path.stat(follow_symlinks=False)
    if (_redirected_directory(path, path_stat) or
            not stat.S_ISDIR(path_stat.st_mode)):
        raise ValueError(f"unsafe archive directory: {path}")


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
            _require_plain_directory(directory)
            _fsync_directory(directory.parent)
        else:
            _fsync_directory(directory.parent)


def _archive_object_namespace(
        archive_root: Path, sha: str, *, create: bool) -> tuple[Path, Path]:
    if create:
        _mkdir_durable(archive_root)
    _require_plain_directory(archive_root)
    current = archive_root
    for component in ("objects", "sha256", sha[:2], sha[2:4]):
        child = current / component
        if create and not os.path.lexists(child):
            try:
                child.mkdir()
            except FileExistsError:
                pass
        _require_plain_directory(child)
        if create:
            _fsync_directory(current)
        current = child
    return current, current / sha


def _same_archive_identity(
        left: os.stat_result, right: os.stat_result) -> bool:
    return (
        stat.S_ISREG(left.st_mode) and stat.S_ISREG(right.st_mode) and
        (left.st_dev, left.st_ino, left.st_size, left.st_mtime_ns) ==
        (right.st_dev, right.st_ino, right.st_size, right.st_mtime_ns)
    )


def _open_verified_archive_object(
        obj_path: Path, sha: str,
        expected_bytes: int) -> tuple[object, os.stat_result]:
    path_stat = obj_path.stat(follow_symlinks=False)
    if (not stat.S_ISREG(path_stat.st_mode) or
            path_stat.st_size != expected_bytes):
        raise ValueError(f"archive object integrity mismatch: {obj_path}")

    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(obj_path, flags)
    handle = os.fdopen(fd, "rb")
    try:
        opened_stat = os.fstat(handle.fileno())
        if not _same_archive_identity(path_stat, opened_stat):
            raise ValueError(f"archive object integrity mismatch: {obj_path}")
        actual_sha = hashlib.file_digest(handle, "sha256").hexdigest()
        opened_after = os.fstat(handle.fileno())
        if (actual_sha != sha or
                not _same_archive_identity(opened_stat, opened_after)):
            raise ValueError(f"archive object integrity mismatch: {obj_path}")
        path_after = obj_path.stat(follow_symlinks=False)
        if not _same_archive_identity(opened_after, path_after):
            raise ValueError(f"archive object integrity mismatch: {obj_path}")
        return handle, opened_after
    except Exception:
        handle.close()
        raise


def _archive_object_still_bound(
        handle: object, obj_path: Path, verified: os.stat_result,
        archive_root: Path, sha: str) -> bool:
    try:
        obj_dir, expected_path = _archive_object_namespace(
            archive_root, sha, create=False)
        if expected_path != obj_path or obj_dir != obj_path.parent:
            return False
        opened = os.fstat(handle.fileno())
        current = obj_path.stat(follow_symlinks=False)
    except (OSError, ValueError):
        return False
    return (
        _same_archive_identity(verified, opened) and
        _same_archive_identity(verified, current)
    )


def _safe_fonds_links_supported() -> bool:
    required = (os.open, os.mkdir, os.stat, os.symlink)
    return bool(
        os.name == "posix" and
        hasattr(os, "O_DIRECTORY") and
        hasattr(os, "O_NOFOLLOW") and
        all(operation in os.supports_dir_fd for operation in required) and
        os.stat in os.supports_follow_symlinks
    )


def _create_fonds_link(archive_root: Path, obj_path: Path,
                       link: Path, fonds_path: str) -> None:
    if not _safe_fonds_links_supported():
        return

    parts = fonds_path.replace("\\", "/").split("/")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    descriptors = []
    try:
        current = os.open(archive_root, flags)
        descriptors.append(current)
        current = os.open("fonds", flags, dir_fd=current)
        descriptors.append(current)
        for part in parts[:-1]:
            try:
                os.mkdir(part, dir_fd=current)
            except FileExistsError:
                pass
            current = os.open(part, flags, dir_fd=current)
            descriptors.append(current)
        try:
            os.stat(parts[-1], dir_fd=current, follow_symlinks=False)
        except FileNotFoundError:
            os.symlink(os.path.relpath(obj_path, link.parent), parts[-1],
                       dir_fd=current)
            try:
                os.fsync(current)
            except OSError:
                pass
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def append_manifest_occurrence(
        archive_root: Path, occurrence: dict,
        *, pre_append: Callable[[], bool] | None = None,
        post_append: Callable[[], bool] | None = None,
        skip_if: Callable[[], bool] | None = None) -> bool:
    manifest = archive_root / "manifest.jsonl"
    line = (json.dumps(occurrence, sort_keys=True, default=str) + "\n").encode()
    with _manifest_file_lock(manifest):
        if skip_if is not None and skip_if():
            return False
        if pre_append is not None and not pre_append():
            raise ValueError("manifest append precondition failed")
        with manifest.open("a+b") as handle:
            handle.seek(0, os.SEEK_END)
            original_size = handle.tell()
            if original_size:
                handle.seek(-1, os.SEEK_END)
                if handle.read(1) != b"\n":
                    handle.seek(0, os.SEEK_END)
                    handle.write(b"\n")
            handle.seek(0, os.SEEK_END)
            handle.write(line)
            handle.flush()
            os.fsync(handle.fileno())
            try:
                valid = post_append is None or post_append()
            except Exception:
                handle.truncate(original_size)
                handle.flush()
                os.fsync(handle.fileno())
                raise
            if not valid:
                handle.truncate(original_size)
                handle.flush()
                os.fsync(handle.fileno())
                raise ValueError("manifest append postcondition failed")
        _fsync_directory(manifest.parent)
    return True


def _manifest_occurrence(
        archive_root: Path, ingest_job_id: str) -> dict | None:
    manifest = archive_root / "manifest.jsonl"
    try:
        lines = manifest.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return None
    for line in lines:
        try:
            existing = json.loads(line)
        except (TypeError, json.JSONDecodeError):
            continue
        if (isinstance(existing, dict) and
                existing.get("ingest_job_id") == ingest_job_id):
            return existing
    return None


def _manifest_has_occurrence(archive_root: Path, occurrence: dict) -> bool:
    existing = _manifest_occurrence(
        archive_root, occurrence["ingest_job_id"])
    if existing is None:
        return False
    if existing.get("sha256") != occurrence["sha256"]:
        raise ArchiveOccurrenceCollision(
            "archive occurrence identity collision")
    return True


def archive_bytes(data: bytes, mime: str, fonds_path: str,
                  ingest_job_id: str | None = None) -> str:
    """Store blob content-addressed; append occurrence; symlink into fonds.

    Returns sha256. Identical bytes share one immutable object while every
    archive call records its own occurrence.
    """
    sha = hashlib.sha256(data).hexdigest()
    if ingest_job_id is not None:
        existing = _manifest_occurrence(CFG.archive, ingest_job_id)
        if existing is not None and existing.get("sha256") != sha:
            raise ArchiveOccurrenceCollision(
                "archive occurrence identity collision")
    obj_dir, obj_path = _archive_object_namespace(
        CFG.archive, sha, create=True)
    trusted_archive_root = CFG.archive.resolve()
    link = validate_fonds_path(trusted_archive_root, fonds_path)
    tmp = obj_dir / f".{sha}.{os.getpid()}.{secrets.token_hex(8)}.tmp"
    with tmp.open("xb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    keep_temp = False
    canonical_observed = False
    try:
        try:
            os.link(tmp, obj_path)
            canonical_observed = True
        except FileExistsError:
            canonical_observed = True
        except PermissionError:
            deadline = time.monotonic() + 5
            while not os.path.lexists(obj_path):
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.01)
            canonical_observed = True
        _fsync_directory(obj_dir)

        try:
            obj_handle, obj_stat = _open_verified_archive_object(
                obj_path, sha, len(data))
        except Exception:
            keep_temp = canonical_observed
            raise
        try:
            try:
                seen = datetime.fromtimestamp(
                    obj_stat.st_mtime, timezone.utc).isoformat()
                occurrence = {
                    "sha256": sha,
                    "bytes": len(data),
                    "mime": mime,
                    "first_seen": seen,
                    "occurrence_at": datetime.now(timezone.utc).isoformat(),
                    "fonds_path": fonds_path.replace("\\", "/"),
                    "ingest_job_id": ingest_job_id,
                }
                occurrence_published = append_manifest_occurrence(
                    CFG.archive, occurrence,
                    skip_if=(
                        (lambda: _manifest_has_occurrence(
                            CFG.archive, occurrence))
                        if ingest_job_id is not None else None),
                    pre_append=lambda: _archive_object_still_bound(
                        obj_handle, obj_path, obj_stat, CFG.archive, sha),
                    post_append=lambda: _archive_object_still_bound(
                        obj_handle, obj_path, obj_stat, CFG.archive, sha))
            except Exception as exc:
                binding_failure = (
                    isinstance(exc, ValueError) and
                    str(exc) in {
                        "manifest append precondition failed",
                        "manifest append postcondition failed",
                    }
                )
                if (binding_failure or
                        not _archive_object_still_bound(
                            obj_handle, obj_path, obj_stat, CFG.archive, sha)):
                    keep_temp = True
                raise
        finally:
            obj_handle.close()

        # Fonds is a best-effort derived view, created only after the durable
        # occurrence binds the verified object to its canonical pathname.
        if occurrence_published:
            try:
                _create_fonds_link(
                    trusted_archive_root, obj_path, link, fonds_path)
            except OSError:
                pass

        return sha
    finally:
        if not keep_temp:
            tmp.unlink(missing_ok=True)
            _fsync_directory(obj_dir)


def archive_file(path: Path, fonds_path: str,
                 mime: str = "application/octet-stream",
                 ingest_job_id: str | None = None) -> str:
    return archive_bytes(path.read_bytes(), mime, fonds_path,
                         ingest_job_id=ingest_job_id)


def read_blob(sha: str) -> bytes:
    return (CFG.archive / "objects" / "sha256" / sha[:2] / sha[2:4] / sha).read_bytes()

# ----------------------------------------------------------------- ledger

def append_event(
    conn: psycopg.Connection,
    *,
    kind: str,
    session_id: str,
    ts: datetime | None = None,
    agent: str = "claude-code",
    project: str | None = None,
    raw_sha256: str | None = None,
    payload: dict | None = None,
    meta: bool = False,
    source_adapter: str | None = None,
    source_event_id: str | None = None,
    source_seq: int | None = None,
    ingest_job_id: str | None = None,
) -> str | None:
    ts = ts or datetime.now(timezone.utc)
    payload = payload or {}
    content_hash = hashlib.sha256(
        json.dumps(payload, sort_keys=True, default=str).encode()
    ).hexdigest()
    eid = new_id("evt")
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
