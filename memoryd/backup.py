"""Offline, verified backups for memoryd's database and durable files."""
from __future__ import annotations

import argparse
import errno
import hashlib
import json
import os
import re
import secrets
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

from . import __version__

SCHEMA_VERSION = 1
POSIX_MODE_ENFORCED = os.name != "nt"
WINDOWS_RESTORE_REQUIRES_ABSENT_HOME = os.name == "nt"
SNAPSHOT_RE = re.compile(r"^\d{8}T\d{6}Z-v1$")
MIGRATION_RE = re.compile(r"^\d{3}_[A-Za-z0-9][A-Za-z0-9_-]*\.sql$")
SNAPSHOT_FILES = {
    "database.dump", "memory.tar.gz", "config.sanitized.json",
    "manifest.json",
}
PAYLOAD_FILES = SNAPSHOT_FILES - {"manifest.json"}
MANIFEST_FIELDS = {
    "schema_version", "created_at", "memoryd_version", "db_migrations",
    "required_secret_env_names", "files",
}
KNOWN_SECRET_ENV_NAMES = {
    "ANTHROPIC_API_KEY", "OPENROUTER_API_KEY", "OPENAI_API_KEY",
    "VOYAGE_API_KEY",
}
SECRET_KEY_RE = re.compile(
    r"(?:api[_-]?key|password|passwd|secret|token|credential|dsn)$", re.I)
SECRET_ENV_RE = re.compile(
    r"(?:^|_)(?:API_KEY|PASSWORD|PASSWD|SECRET|TOKEN|CREDENTIALS?)(?:$|_)",
    re.I)
SERVICE_ROOT_NAME = ".pg-service"
SERVICE_LOCK_NAME = "state.lock"
SERVICE_OPERATION_RE = re.compile(r"^op-[0-9a-f]{32}$")
SERVICE_CLEANUP_RETRY_S = 1.0
SERVICE_CLEANUP_RETRY_INTERVAL_S = 0.01
SERVICE_LOCK_TIMEOUT_S = 660.0


class BackupError(RuntimeError):
    """An operator-actionable backup or restore refusal."""


@dataclass(frozen=True)
class Verification:
    ok: bool
    reason: str = "ok"


@dataclass(frozen=True)
class BackupListing:
    timestamp: str
    path: Path
    ok: bool
    reason: str


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _default_output() -> Path:
    return Path("~/memory/backups").expanduser()


def _default_home() -> Path:
    return Path(os.environ.get("MEMORYD_HOME", "~/memory")).expanduser()


def _chmod(path: Path, mode: int) -> None:
    try:
        os.chmod(path, mode, follow_symlinks=False)
    except OSError as exc:
        if POSIX_MODE_ENFORCED:
            raise BackupError(
                f"cannot enforce owner-only permissions on {path}: {exc}") from exc


def _require_mode(path: Path, expected: int) -> None:
    if not POSIX_MODE_ENFORCED:
        return
    actual = stat.S_IMODE(path.stat(follow_symlinks=False).st_mode)
    if actual != expected:
        raise BackupError(
            f"unsafe mode for {path}: expected {expected:04o}, got {actual:04o}")


def _ensure_owner_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    _chmod(path, 0o700)


def _atomic_rename(source: Path, destination: Path) -> None:
    """Publish atomically, tolerating short-lived Windows scanner handles."""
    deadline = time.monotonic() + 5
    destination_existed = os.path.lexists(destination)
    while True:
        try:
            os.replace(source, destination)
            return
        except PermissionError:
            if destination_existed or time.monotonic() >= deadline:
                raise
            time.sleep(0.005)


def _daemon_health() -> dict | None:
    from .cli import _health
    return _health()


def _doctor_findings(home: Path) -> list[Any]:
    from .doctor import inspect_archive, inspect_spool
    return [*inspect_spool(home / "spool"),
            *inspect_archive(home / "archive")]


def _finding_value(finding: Any, name: str, default: str = "") -> str:
    if isinstance(finding, dict):
        return str(finding.get(name, default))
    return str(getattr(finding, name, default))


def _read_config(home: Path) -> dict[str, Any]:
    path = home / "config.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise BackupError(f"cannot read memoryd config {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise BackupError(f"memoryd config is not an object: {path}")
    return value


def _is_secret_key(key: str) -> bool:
    return bool(SECRET_KEY_RE.search(key))


def _is_secret_env(key: str) -> bool:
    return bool(SECRET_ENV_RE.search(key))


def _sanitize_config(value: dict[str, Any]) -> tuple[dict[str, Any], list[str], set[str]]:
    required: set[str] = set()
    removed_values: set[str] = set()

    def clean(item: Any, *, parent: str = "") -> Any:
        if isinstance(item, dict):
            result: dict[str, Any] = {}
            for raw_key, child in item.items():
                key = str(raw_key)
                secret = _is_secret_key(key) or (parent == "env" and _is_secret_env(key))
                if secret:
                    if parent == "env":
                        required.add(key)
                    if isinstance(child, str) and child:
                        removed_values.add(child)
                    continue
                result[key] = clean(child, parent=key)
            return result
        if isinstance(item, list):
            return [clean(child, parent=parent) for child in item]
        return item

    return clean(value), sorted(required), removed_values


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _file_entry(path: Path) -> dict[str, Any]:
    return {"sha256": _sha256(path), "bytes": path.stat().st_size}


def _safe_source_tree(root: Path) -> set[str]:
    try:
        root_stat = root.stat(follow_symlinks=False)
    except FileNotFoundError:
        return set()
    if stat.S_ISLNK(root_stat.st_mode) or not stat.S_ISDIR(root_stat.st_mode):
        raise BackupError(f"backup source root must be a real directory: {root}")
    pending = [root]
    links: set[str] = set()
    while pending:
        directory = pending.pop()
        with os.scandir(directory) as entries:
            for entry in entries:
                path = Path(entry.path)
                if entry.is_symlink():
                    links.add(path.relative_to(root).as_posix())
                elif entry.is_dir(follow_symlinks=False):
                    pending.append(path)
                elif not entry.is_file(follow_symlinks=False):
                    raise BackupError(
                        f"refusing special file in backup source: {path}")
    return links


def _tar_filter(member: tarfile.TarInfo) -> tarfile.TarInfo:
    member.uid = member.gid = 0
    member.uname = member.gname = ""
    member.mode = 0o700 if member.isdir() else 0o600
    return member


def _create_memory_tar(home: Path, destination: Path) -> None:
    skipped_links: set[str] = set()
    for name in ("archive", "spool"):
        skipped_links.update(
            f"{name}/{relative}"
            for relative in _safe_source_tree(home / name))

    def safe_filter(member: tarfile.TarInfo) -> tarfile.TarInfo | None:
        if member.name in skipped_links:
            return None
        return _tar_filter(member)

    with tarfile.open(destination, "w:gz", dereference=False) as archive:
        for name in ("archive", "spool"):
            source = home / name
            if source.is_dir():
                archive.add(source, arcname=name, recursive=True,
                            filter=safe_filter)
            else:
                member = tarfile.TarInfo(name)
                member.type = tarfile.DIRTYPE
                archive.addfile(_tar_filter(member))


def _safe_conninfo(dsn: str) -> tuple[dict[str, str], tuple[str, ...]]:
    try:
        from psycopg.conninfo import conninfo_to_dict
        values = conninfo_to_dict(dsn)
    except Exception as exc:  # noqa: BLE001
        raise BackupError("invalid PostgreSQL DSN") from exc
    redactions = tuple(
        values[name] for name in ("password", "sslpassword")
        if values.get(name))
    return values, redactions


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        fd = os.open(path, flags)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError as exc:
        raise BackupError(f"cannot fsync service directory {path}: {exc}") from exc


def _require_service_artifact(
        path: Path, *, directory: bool, mode: int) -> None:
    try:
        value = path.stat(follow_symlinks=False)
    except OSError as exc:
        raise BackupError(f"cannot inspect service artifact {path}: {exc}") from exc
    expected = stat.S_ISDIR if directory else stat.S_ISREG
    if path.is_symlink() or not expected(value.st_mode):
        raise BackupError(f"unsafe service artifact: {path}")
    if (hasattr(os, "getuid") and value.st_uid != os.getuid()):
        raise BackupError(f"service artifact is not owned by this user: {path}")
    if POSIX_MODE_ENFORCED and stat.S_IMODE(value.st_mode) != mode:
        raise BackupError(
            f"unsafe mode for service artifact {path}: expected {mode:04o}")


def _remove_service_artifact(path: Path, *, directory: bool) -> None:
    deadline = time.monotonic() + SERVICE_CLEANUP_RETRY_S
    while True:
        try:
            if directory:
                path.rmdir()
            else:
                path.unlink()
            _fsync_directory(path.parent)
            return
        except FileNotFoundError:
            return
        except OSError as exc:
            if time.monotonic() >= deadline:
                raise BackupError(
                    f"cannot clean service artifact {path}: {exc}") from exc
            time.sleep(SERVICE_CLEANUP_RETRY_INTERVAL_S)


def _cleanup_service_operation(operation: Path) -> None:
    _require_service_artifact(operation, directory=True, mode=0o700)
    try:
        children = list(operation.iterdir())
    except OSError as exc:
        raise BackupError(
            f"cannot inspect service operation {operation}: {exc}") from exc
    if children:
        if len(children) != 1 or children[0].name != "pg_service.conf":
            raise BackupError(
                f"unrecognized service artifact in {operation}")
        service_file = children[0]
        _require_service_artifact(service_file, directory=False, mode=0o600)
        _remove_service_artifact(service_file, directory=False)
    _remove_service_artifact(operation, directory=True)


def _service_root() -> Path:
    home = _default_home()
    try:
        home.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise BackupError(f"cannot create memory home {home}: {exc}") from exc
    root = home / SERVICE_ROOT_NAME
    created = False
    try:
        root.mkdir(mode=0o700)
        created = True
    except FileExistsError:
        pass
    except OSError as exc:
        raise BackupError(f"cannot create service root {root}: {exc}") from exc
    if created:
        _chmod(root, 0o700)
        _fsync_directory(home)
    _require_service_artifact(root, directory=True, mode=0o700)
    _ensure_service_lock(root)
    return root


def _ensure_service_lock(root: Path) -> Path:
    lock_file = root / SERVICE_LOCK_NAME
    flags = (os.O_CREAT | os.O_EXCL | os.O_RDWR |
             getattr(os, "O_BINARY", 0))
    created = False
    try:
        fd = os.open(lock_file, flags, 0o600)
        created = True
    except FileExistsError:
        _require_service_artifact(lock_file, directory=False, mode=0o600)
        flags = os.O_RDWR | getattr(os, "O_BINARY", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(lock_file, flags)
        except OSError as exc:
            raise BackupError(f"cannot open service lock {lock_file}: {exc}") from exc
    except OSError as exc:
        raise BackupError(f"cannot create service lock {lock_file}: {exc}") from exc
    try:
        if os.fstat(fd).st_size == 0:
            os.write(fd, b"\0")
        os.fsync(fd)
    finally:
        os.close(fd)
    _chmod(lock_file, 0o600)
    _require_service_artifact(lock_file, directory=False, mode=0o600)
    if created:
        _fsync_directory(root)
    return lock_file


@contextmanager
def _service_state_lock(root: Path):
    lock_file = _ensure_service_lock(root)
    flags = os.O_RDWR | getattr(os, "O_BINARY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(lock_file, flags)
    except OSError as exc:
        raise BackupError(f"cannot open service lock {lock_file}: {exc}") from exc
    acquired = False
    deadline = time.monotonic() + SERVICE_LOCK_TIMEOUT_S
    try:
        if os.name == "nt":
            import msvcrt
            while True:
                os.lseek(fd, 0, os.SEEK_SET)
                try:
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                    acquired = True
                    break
                except OSError as exc:
                    if time.monotonic() >= deadline:
                        raise BackupError(
                            f"timed out acquiring service lock {lock_file}") from exc
                    time.sleep(SERVICE_CLEANUP_RETRY_INTERVAL_S)
        else:
            import fcntl
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    acquired = True
                    break
                except OSError as exc:
                    if exc.errno not in {errno.EACCES, errno.EAGAIN}:
                        raise BackupError(
                            f"cannot acquire service lock {lock_file}: {exc}") from exc
                    if time.monotonic() >= deadline:
                        raise BackupError(
                            f"timed out acquiring service lock {lock_file}") from exc
                    time.sleep(SERVICE_CLEANUP_RETRY_INTERVAL_S)
        yield
    finally:
        try:
            if acquired:
                if os.name == "nt":
                    import msvcrt
                    os.lseek(fd, 0, os.SEEK_SET)
                    msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def _cleanup_stale_service_operations(root: Path) -> None:
    try:
        children = list(root.iterdir())
    except OSError as exc:
        raise BackupError(f"cannot inspect service root {root}: {exc}") from exc
    for operation in children:
        if operation.name == SERVICE_LOCK_NAME:
            _require_service_artifact(operation, directory=False, mode=0o600)
            continue
        if not SERVICE_OPERATION_RE.fullmatch(operation.name):
            raise BackupError(f"unrecognized service artifact: {operation}")
        _cleanup_service_operation(operation)


@contextmanager
def _service_operation(values: dict[str, str], root: Path):
    operation: Path | None = None
    primary: BaseException | None = None
    try:
        for _attempt in range(3):
            candidate = root / f"op-{secrets.token_hex(16)}"
            try:
                candidate.mkdir(mode=0o700)
            except FileExistsError:
                continue
            operation = candidate
            break
        if operation is None:
            raise BackupError("cannot allocate a unique service operation")
        _chmod(operation, 0o700)
        _fsync_directory(root)
        service_file = operation / "pg_service.conf"
        flags = (os.O_CREAT | os.O_EXCL | os.O_WRONLY |
                 getattr(os, "O_BINARY", 0))
        fd = os.open(service_file, flags, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write("[memoryd]\n")
            for name, value in sorted(values.items()):
                handle.write(f"{name}={value}\n")
            handle.flush()
            os.fsync(handle.fileno())
        _chmod(service_file, 0o600)
        _fsync_directory(operation)
        env = os.environ.copy()
        for name in ("PGPASSWORD", "PGSSLPASSWORD", "PGSERVICE",
                     "PGSERVICEFILE"):
            env.pop(name, None)
        env["PGSERVICEFILE"] = str(service_file)
        yield "service=memoryd", env
    except BaseException as exc:
        primary = exc
        raise
    finally:
        if operation is not None:
            try:
                _cleanup_service_operation(operation)
            except BackupError as cleanup_exc:
                if primary is None:
                    raise
                primary.add_note(f"service cleanup failed: {cleanup_exc}")


@contextmanager
def _libpq_service(values: dict[str, str]):
    for value in values.values():
        if "\n" in value or "\r" in value:
            raise BackupError(
                "PostgreSQL connection values cannot contain newlines")
    root = _service_root()
    with _service_state_lock(root):
        _cleanup_stale_service_operations(root)
        with _service_operation(values, root) as connection:
            yield connection


def _run_tool(
        command: list[str], *, env: dict[str, str],
        redactions: tuple[str, ...]) -> None:
    try:
        result = subprocess.run(
            command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, env=env, shell=False, timeout=600)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise BackupError(f"database tool failed: {exc}") from exc
    if result.returncode:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        for secret in redactions:
            detail = detail.replace(secret, "***")
        raise BackupError(
            f"database tool exited {result.returncode}: {detail or 'no diagnostic'}")


def _docker_available() -> bool:
    from .cli import CONTAINER, _docker
    return _docker("inspect", CONTAINER)[0] == 0


def _docker_tool(arguments: list[str]) -> None:
    from .cli import CONTAINER, _docker
    code, detail = _docker("exec", CONTAINER, *arguments)
    if code:
        raise BackupError(f"database tool in Docker failed: {detail}")


def _container_connection(values: dict[str, str]) -> tuple[str, str] | None:
    from .cli import _container_port
    host = values.get("host", "")
    if host not in {"127.0.0.1", "localhost", "::1"}:
        return None
    if values.get("port", "5432") != (_container_port() or ""):
        return None
    return values.get("user", "postgres"), values.get("dbname", "memoryd")


def _remote_dump_path(operation: str) -> str:
    return f"/tmp/memoryd-{operation}-{secrets.token_hex(16)}.dump"


def _dump_database(dsn: str, destination: Path) -> None:
    values, redactions = _safe_conninfo(dsn)
    tool = shutil.which("pg_dump")
    if tool:
        with _libpq_service(values) as (safe_dsn, env):
            _run_tool([tool, "--format=custom", "--file", str(destination),
                       "--dbname", safe_dsn], env=env,
                      redactions=redactions)
        return
    connection = _container_connection(values)
    if not connection or not _docker_available():
        raise BackupError(
            "pg_dump is unavailable; install PostgreSQL client tools or use "
            "the installer-managed memoryd-pgvector container")
    from .cli import CONTAINER, _docker
    user, database = connection
    remote = _remote_dump_path("backup")
    try:
        _docker_tool(["pg_dump", "-U", user, "-d", database,
                      "--format=custom", "--file", remote])
        code, detail = _docker("cp", f"{CONTAINER}:{remote}", str(destination))
        if code:
            raise BackupError(f"copying database dump from Docker failed: {detail}")
    finally:
        _docker("exec", CONTAINER, "rm", "-f", remote)


def _restore_database(dump: Path, dsn: str) -> None:
    values, redactions = _safe_conninfo(dsn)
    tool = shutil.which("pg_restore")
    if tool:
        with _libpq_service(values) as (safe_dsn, env):
            _run_tool([tool, "--exit-on-error", "--single-transaction",
                       "--no-owner", "--no-privileges", "--dbname", safe_dsn,
                       str(dump)], env=env, redactions=redactions)
        return
    connection = _container_connection(values)
    if not connection or not _docker_available():
        raise BackupError(
            "pg_restore is unavailable; install PostgreSQL client tools or use "
            "the installer-managed memoryd-pgvector container")
    from .cli import CONTAINER, _docker
    user, database = connection
    remote = _remote_dump_path("restore")
    try:
        code, detail = _docker("cp", str(dump), f"{CONTAINER}:{remote}")
        if code:
            raise BackupError(
                f"copying database dump into Docker failed: {detail}")
        _docker_tool(["pg_restore", "--exit-on-error", "--single-transaction",
                      "--no-owner", "--no-privileges", "-U", user, "-d",
                      database, remote])
    finally:
        _docker("exec", CONTAINER, "rm", "-f", remote)


def _valid_migration_names(value: object) -> bool:
    return (
        isinstance(value, list) and bool(value) and
        all(isinstance(name, str) and MIGRATION_RE.fullmatch(name)
            for name in value) and
        value == sorted(set(value))
    )


def _database_migrations(dsn: str) -> list[str]:
    try:
        import psycopg
        with psycopg.connect(dsn, connect_timeout=5) as connection:
            rows = connection.execute(
                "SELECT filename FROM schema_migrations ORDER BY filename"
            ).fetchall()
    except Exception as exc:  # noqa: BLE001
        raise BackupError(
            f"cannot read source schema_migrations ledger: {exc}") from exc
    try:
        names = [row["filename"] if isinstance(row, dict) else row[0]
                 for row in rows]
    except (KeyError, IndexError, TypeError) as exc:
        raise BackupError("invalid source schema_migrations ledger rows") from exc
    if not _valid_migration_names(names):
        raise BackupError("source schema_migrations ledger is empty or invalid")
    return names


def _snapshot_name(created: datetime) -> str:
    return created.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ-v1")


def create_backup(*, output: Path | str | None = None,
                  retain: int = 14, home: Path | str | None = None) -> Path:
    """Create, verify, atomically publish, then safely retain snapshots."""
    if retain < 1:
        raise BackupError("--retain must be at least 1")
    if _daemon_health() is not None:
        raise BackupError("stop the memoryd daemon before creating a backup")
    config_home = Path(home) if home is not None else _default_home()
    config = _read_config(config_home)
    configured_home = config.get("home")
    if (not os.environ.get("MEMORYD_HOME") and
            isinstance(configured_home, str) and configured_home):
        source_home = Path(configured_home).expanduser()
    else:
        source_home = config_home
    findings = [finding for finding in _doctor_findings(source_home)
                if _finding_value(finding, "severity") == "error"]
    if findings:
        codes = ", ".join(_finding_value(item, "code", "integrity_error")
                          for item in findings)
        raise BackupError(f"doctor found integrity errors: {codes}")
    dsn = os.environ.get("MEMORYD_DSN") or config.get("dsn")
    if not isinstance(dsn, str) or not dsn:
        raise BackupError("no PostgreSQL DSN configured")
    sanitized, required, removed_values = _sanitize_config(config)
    environment_secrets = {
        name: os.environ[name] for name in KNOWN_SECRET_ENV_NAMES
        if os.environ.get(name)
    }
    required = sorted(set(required) | set(environment_secrets))
    removed_values.update(environment_secrets.values())
    migrations = _database_migrations(dsn)

    root = Path(output) if output is not None else _default_output()
    if os.path.lexists(root) and root.is_symlink():
        raise BackupError(f"backup output must not be a symlink: {root}")
    _ensure_owner_dir(root)
    created = _utc_now()
    final = root / _snapshot_name(created)
    if os.path.lexists(final):
        raise BackupError(f"snapshot already exists: {final}")
    staging: Path | None = None
    try:
        staging = Path(tempfile.mkdtemp(prefix=".memoryd-backup-", dir=root))
        _chmod(staging, 0o700)
        dump = staging / "database.dump"
        memory_tar = staging / "memory.tar.gz"
        sanitized_path = staging / "config.sanitized.json"
        _dump_database(dsn, dump)
        _create_memory_tar(source_home, memory_tar)
        sanitized_path.write_text(
            json.dumps(sanitized, indent=2, sort_keys=True), encoding="utf-8")
        for path in (dump, memory_tar, sanitized_path):
            _chmod(path, 0o600)
        manifest = {
            "schema_version": SCHEMA_VERSION,
            "created_at": created.astimezone(timezone.utc).isoformat().replace(
                "+00:00", "Z"),
            "memoryd_version": __version__,
            "db_migrations": migrations,
            "required_secret_env_names": required,
            "files": {name: _file_entry(staging / name)
                      for name in sorted(PAYLOAD_FILES)},
        }
        manifest_text = json.dumps(manifest, indent=2, sort_keys=True)
        config_text = sanitized_path.read_text(encoding="utf-8")
        if any(secret and secret in manifest_text + config_text
               for secret in removed_values):
            raise BackupError("sanitized backup metadata still contains a secret")
        manifest_path = staging / "manifest.json"
        manifest_path.write_text(manifest_text, encoding="utf-8")
        _chmod(manifest_path, 0o600)
        result = verify_snapshot(staging, require_generated_name=False)
        if not result.ok:
            raise BackupError(f"created snapshot failed verification: {result.reason}")
        _atomic_rename(staging, final)
        staging = None
        _apply_retention(root, retain, verified={final: result})
        return final
    except BackupError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise BackupError(f"backup creation failed: {exc}") from exc
    finally:
        if staging is not None:
            shutil.rmtree(staging, ignore_errors=True)


def _load_manifest(snapshot: Path) -> dict[str, Any]:
    try:
        value = json.loads((snapshot / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise BackupError(f"manifest is unreadable: {exc}") from exc
    if not isinstance(value, dict):
        raise BackupError("manifest is not an object")
    return value


def _validate_config_secrets(value: Any, *, parent: str = "") -> None:
    if isinstance(value, dict):
        for raw_key, child in value.items():
            key = str(raw_key)
            if _is_secret_key(key) or (parent == "env" and _is_secret_env(key)):
                raise BackupError(f"sanitized config contains secret field: {key}")
            _validate_config_secrets(child, parent=key)
    elif isinstance(value, list):
        for child in value:
            _validate_config_secrets(child, parent=parent)
    elif isinstance(value, str):
        if re.search(r"://[^\s:/@]+:[^\s@]+@", value):
            raise BackupError("sanitized config contains a password-bearing DSN")


def _validated_tar_members(
        archive: tarfile.TarFile, *, read_payloads: bool,
) -> list[tuple[tarfile.TarInfo, tuple[str, ...]]]:
    validated: list[tuple[tarfile.TarInfo, tuple[str, ...]]] = []
    seen: set[tuple[str, ...]] = set()
    roots: set[str] = set()
    for member in archive:
        name = member.name
        pure = PurePosixPath(name)
        parts = pure.parts
        if (not name or not parts or "\\" in name or pure.is_absolute() or
                ".." in parts or parts[0] not in {"archive", "spool"}):
            raise BackupError(f"unsafe tar member path: {name!r}")
        normalized = tuple(parts)
        if normalized in seen:
            raise BackupError(
                f"duplicate normalized tar destination: {'/'.join(normalized)}")
        seen.add(normalized)
        if not (member.isdir() or member.isreg()):
            raise BackupError(f"unsafe tar member type: {name}")
        if len(normalized) == 1 and member.isdir():
            roots.add(normalized[0])
        if member.isreg() and read_payloads:
            payload = archive.extractfile(member)
            if payload is None:
                raise BackupError(f"unreadable tar member: {name}")
            with payload:
                while payload.read(1024 * 1024):
                    pass
        validated.append((member, normalized))
    missing = {"archive", "spool"} - roots
    if missing:
        raise BackupError(
            f"memory tar is missing required roots: {sorted(missing)!r}")
    return validated


def _validate_tar(path: Path) -> None:
    try:
        with tarfile.open(path, "r:gz") as archive:
            _validated_tar_members(archive, read_payloads=True)
    except BackupError:
        raise
    except (OSError, tarfile.TarError) as exc:
        raise BackupError(f"memory tar is unreadable: {exc}") from exc


def verify_snapshot(snapshot: Path | str, *,
                    require_generated_name: bool = True) -> Verification:
    path = Path(snapshot)
    try:
        if require_generated_name and not SNAPSHOT_RE.fullmatch(path.name):
            raise BackupError("snapshot name is not a generated v1 name")
        if path.is_symlink() or not path.is_dir():
            raise BackupError("snapshot must be a real directory")
        _require_mode(path, 0o700)
        actual = {child.name for child in path.iterdir()}
        if actual != SNAPSHOT_FILES:
            raise BackupError(
                f"snapshot file allowlist mismatch: {sorted(actual)!r}")
        for child in path.iterdir():
            mode = child.stat(follow_symlinks=False).st_mode
            if child.is_symlink() or not stat.S_ISREG(mode):
                raise BackupError(f"snapshot entry is not a regular file: {child.name}")
            _require_mode(child, 0o600)
        manifest = _load_manifest(path)
        if set(manifest) != MANIFEST_FIELDS:
            raise BackupError("manifest field allowlist mismatch")
        if (type(manifest.get("schema_version")) is not int or
                manifest["schema_version"] != SCHEMA_VERSION):
            raise BackupError("unsupported manifest schema_version")
        created_at = manifest.get("created_at")
        if not isinstance(created_at, str) or not created_at.endswith("Z"):
            raise BackupError("invalid manifest created_at")
        try:
            created = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        except ValueError as exc:
            raise BackupError("invalid manifest created_at") from exc
        if created.utcoffset() != timezone.utc.utcoffset(created):
            raise BackupError("manifest created_at is not UTC")
        if require_generated_name and _snapshot_name(created) != path.name:
            raise BackupError("snapshot name does not match manifest created_at")
        version = manifest.get("memoryd_version")
        if not isinstance(version, str) or not version:
            raise BackupError("invalid manifest memoryd_version")
        files = manifest.get("files")
        if not isinstance(files, dict) or set(files) != PAYLOAD_FILES:
            raise BackupError("manifest file allowlist mismatch")
        for name in sorted(PAYLOAD_FILES):
            entry = files.get(name)
            if (not isinstance(entry, dict) or set(entry) != {"sha256", "bytes"} or
                    not isinstance(entry.get("sha256"), str) or
                    not re.fullmatch(r"[0-9a-f]{64}", entry["sha256"]) or
                    type(entry.get("bytes")) is not int or entry["bytes"] < 0):
                raise BackupError(f"invalid manifest entry for {name}")
            target = path / name
            if target.stat().st_size != entry["bytes"]:
                raise BackupError(f"size mismatch for {name}")
            if _sha256(target) != entry["sha256"]:
                raise BackupError(f"checksum mismatch for {name}")
        required = manifest.get("required_secret_env_names")
        if (not isinstance(required, list) or
                any(not isinstance(name, str) or not _is_secret_env(name)
                    for name in required) or
                required != sorted(set(required))):
            raise BackupError("invalid required_secret_env_names")
        migrations = manifest.get("db_migrations")
        if not _valid_migration_names(migrations):
            raise BackupError("invalid db_migrations")
        try:
            config = json.loads(
                (path / "config.sanitized.json").read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError) as exc:
            raise BackupError(f"sanitized config is unreadable: {exc}") from exc
        if not isinstance(config, dict):
            raise BackupError("sanitized config is not an object")
        _validate_config_secrets(config)
        serialized = json.dumps({"manifest": manifest, "config": config})
        for key, value in os.environ.items():
            if (_is_secret_env(key) and len(value) >= 8 and
                    value in serialized):
                raise BackupError(f"snapshot metadata contains secret value from {key}")
        with (path / "database.dump").open("rb") as handle:
            if handle.read(5) != b"PGDMP":
                raise BackupError("database.dump is not PostgreSQL custom format")
        _validate_tar(path / "memory.tar.gz")
        return Verification(True)
    except BackupError as exc:
        return Verification(False, str(exc))
    except OSError as exc:
        return Verification(False, f"snapshot is unreadable: {exc}")


def list_backups(output: Path | str | None = None) -> list[BackupListing]:
    root = Path(output) if output is not None else _default_output()
    try:
        candidates = sorted(
            (path for path in root.iterdir() if SNAPSHOT_RE.fullmatch(path.name)),
            key=lambda path: path.name)
    except FileNotFoundError:
        return []
    except OSError as exc:
        raise BackupError(f"cannot list backup output {root}: {exc}") from exc
    rows: list[BackupListing] = []
    for path in candidates:
        result = verify_snapshot(path)
        rows.append(BackupListing(path.name.removesuffix("-v1"), path,
                                  result.ok, result.reason))
    return rows


def _apply_retention(
        output: Path, retain: int, *,
        verified: dict[Path, Verification] | None = None) -> None:
    cached = verified or {}
    try:
        candidates = sorted(
            (path for path in output.iterdir()
             if SNAPSHOT_RE.fullmatch(path.name)),
            key=lambda path: path.name)
    except OSError as exc:
        raise BackupError(f"cannot apply retention in {output}: {exc}") from exc
    valid: list[Path] = []
    for path in candidates:
        if path.is_symlink() or not path.is_dir():
            continue
        result = cached.get(path)
        if result is None:
            result = verify_snapshot(path)
        if result.ok:
            valid.append(path)
    for old in valid[:-retain]:
        mode = old.stat(follow_symlinks=False).st_mode
        if stat.S_ISDIR(mode) and not old.is_symlink():
            shutil.rmtree(old)


def _target_db_has_tables(dsn: str) -> bool:
    import psycopg
    with psycopg.connect(dsn, connect_timeout=5) as connection:
        row = connection.execute(
            "SELECT EXISTS (SELECT 1 FROM information_schema.tables "
            "WHERE table_type='BASE TABLE' AND table_schema NOT IN "
            "('pg_catalog', 'information_schema'))").fetchone()
    return bool(row[0])


def _ensure_private_directories(root: Path, target: Path) -> None:
    current = root
    for part in target.relative_to(root).parts:
        current /= part
        current.mkdir(exist_ok=True)
        _chmod(current, 0o700)


def _extract_memory_tar(path: Path, destination: Path) -> None:
    try:
        with tarfile.open(path, "r:gz") as archive:
            members = _validated_tar_members(archive, read_payloads=False)
            for member, normalized in members:
                target = destination.joinpath(*normalized)
                if member.isdir():
                    _ensure_private_directories(destination, target)
                    continue
                _ensure_private_directories(destination, target.parent)
                payload = archive.extractfile(member)
                if payload is None:
                    raise BackupError(
                        f"unreadable tar member: {member.name}")
                with payload, target.open("xb") as handle:
                    shutil.copyfileobj(payload, handle)
                _chmod(target, 0o600)
    except BackupError:
        raise
    except (OSError, tarfile.TarError) as exc:
        raise BackupError(f"memory tar extraction failed: {exc}") from exc


def restore_backup(snapshot: Path | str, *, target_dsn: str,
                   target_home: Path | str) -> Path:
    source = Path(snapshot)
    result = verify_snapshot(source)
    if not result.ok:
        raise BackupError(f"snapshot verification failed: {result.reason}")
    if _daemon_health() is not None:
        raise BackupError("stop the memoryd daemon before restoring a backup")
    home = Path(target_home)
    if os.path.lexists(home):
        if home.is_symlink():
            raise BackupError(f"target home must not be a symlink: {home}")
        if not home.is_dir():
            raise BackupError(f"target home is not a directory: {home}")
        with os.scandir(home) as entries:
            if next(entries, None) is not None:
                raise BackupError(f"target home is not empty: {home}")
        if WINDOWS_RESTORE_REQUIRES_ABSENT_HOME:
            raise BackupError(
                "Windows restore requires an absent target home; remove the "
                "empty directory and retry")
    try:
        if _target_db_has_tables(target_dsn):
            raise BackupError("target database already has user tables")
    except BackupError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise BackupError(f"cannot validate target database: {exc}") from exc

    home.parent.mkdir(parents=True, exist_ok=True)
    staging: Path | None = None
    database_risk = False
    try:
        staging = Path(tempfile.mkdtemp(
            prefix=f".{home.name}.restore-", dir=home.parent))
        _chmod(staging, 0o700)
        _extract_memory_tar(source / "memory.tar.gz", staging)
        config = json.loads(
            (source / "config.sanitized.json").read_text(encoding="utf-8"))
        config["dsn"] = target_dsn
        config["home"] = str(home)
        config_path = staging / "config.json"
        config_path.write_text(
            json.dumps(config, indent=2, sort_keys=True), encoding="utf-8")
        _chmod(config_path, 0o600)
        _require_mode(staging, 0o700)
        _require_mode(config_path, 0o600)
        database_risk = True
        _restore_database(source / "database.dump", target_dsn)
        _atomic_rename(staging, home)
        staging = None
        return home
    except Exception as exc:  # noqa: BLE001
        if isinstance(exc, BackupError):
            detail = str(exc)
        else:
            detail = str(exc)
        suffix = ("; partial empty-target DB risk: pg_restore may have "
                  "created objects; inspect or recreate that target database"
                  if database_risk else "")
        raise BackupError(f"restore failed: {detail}{suffix}") from exc
    finally:
        if staging is not None:
            shutil.rmtree(staging, ignore_errors=True)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="memoryd backup")
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("create")
    create.add_argument("--output", type=Path)
    create.add_argument("--retain", type=int, default=14)
    listing = commands.add_parser("list")
    listing.add_argument("--output", type=Path)
    verify = commands.add_parser("verify")
    verify.add_argument("snapshot", type=Path)
    restore = commands.add_parser("restore")
    restore.add_argument("snapshot", type=Path)
    restore.add_argument("--dsn", required=True)
    restore.add_argument("--home", required=True, type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "create":
            snapshot = create_backup(output=args.output, retain=args.retain)
            print(f"created {snapshot}")
            return 0
        if args.command == "list":
            for row in list_backups(args.output):
                status = "ok" if row.ok else f"CORRUPT: {row.reason}"
                print(f"{row.timestamp}  {row.path}  {status}")
            return 0
        if args.command == "verify":
            result = verify_snapshot(args.snapshot)
            print(f"{args.snapshot}: {'ok' if result.ok else result.reason}")
            return 0 if result.ok else 1
        restore_backup(args.snapshot, target_dsn=args.dsn,
                       target_home=args.home)
        print(f"restored {args.snapshot} -> {args.home}")
        return 0
    except BackupError as exc:
        print(f"memoryd backup: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
