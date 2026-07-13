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
from .ownership import OwnershipError, offline_ownership

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
        try:
            values = _legacy_servicefile_conninfo(dsn)
        except Exception:  # noqa: BLE001
            raise BackupError("invalid PostgreSQL DSN") from exc
    redactions = tuple(
        values[name] for name in ("password", "sslpassword")
        if values.get(name))
    return values, redactions


def _legacy_servicefile_conninfo(dsn: str) -> dict[str, str]:
    """Parse the PostgreSQL 19 servicefile option with an older libpq."""
    from psycopg.conninfo import conninfo_to_dict, make_conninfo

    if dsn.startswith(("postgresql://", "postgres://")):
        from urllib.parse import unquote, urlsplit
        parts = urlsplit(dsn)
        kept: list[str] = []
        servicefile: str | None = None
        for parameter in parts.query.split("&"):
            raw_name, separator, raw_value = parameter.partition("=")
            if unquote(raw_name) == "servicefile":
                if not separator or re.search(r"%(?![0-9A-Fa-f]{2})", raw_value):
                    raise ValueError("invalid servicefile URI parameter")
                servicefile = unquote(raw_value)
            else:
                kept.append(parameter)
        if servicefile is None:
            raise ValueError("no legacy servicefile option")
        query_at = dsn.find("?")
        clean = dsn[:query_at] + (f"?{'&'.join(kept)}" if kept else "")
        values = conninfo_to_dict(clean)
        values["servicefile"] = servicefile
        return values

    whitespace = " \t\r\n\v\f"
    index = 0
    parsed: dict[str, str] = {}
    while index < len(dsn):
        while index < len(dsn) and dsn[index] in whitespace:
            index += 1
        if index == len(dsn):
            break
        start = index
        while (index < len(dsn) and dsn[index] not in whitespace and
               dsn[index] != "="):
            index += 1
        name = dsn[start:index]
        while index < len(dsn) and dsn[index] in whitespace:
            index += 1
        if not name or index == len(dsn) or dsn[index] != "=":
            raise ValueError("invalid conninfo parameter")
        index += 1
        while index < len(dsn) and dsn[index] in whitespace:
            index += 1
        value: list[str] = []
        quoted = index < len(dsn) and dsn[index] == "'"
        if quoted:
            index += 1
        while index < len(dsn):
            character = dsn[index]
            if character == "\\":
                index += 1
                if index == len(dsn):
                    raise ValueError("unterminated conninfo escape")
                value.append(dsn[index])
                index += 1
            elif quoted and character == "'":
                index += 1
                break
            elif not quoted and character in whitespace:
                break
            else:
                value.append(character)
                index += 1
        if quoted and (index == 0 or dsn[index - 1] != "'"):
            raise ValueError("unterminated conninfo quote")
        if index < len(dsn) and dsn[index] not in whitespace:
            raise ValueError("characters after conninfo value")
        parsed[name] = "".join(value)
    if "servicefile" not in parsed:
        raise ValueError("no legacy servicefile option")
    servicefile = parsed.pop("servicefile")
    values = conninfo_to_dict(make_conninfo(**parsed))
    values["servicefile"] = servicefile
    return values


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
        raise BackupError(f"cannot fsync directory {path}: {exc}") from exc


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
        allowed = {"pg_service.conf", "pgpass.conf"}
        if ({child.name for child in children} - allowed or
                len({child.name for child in children}) != len(children)):
            raise BackupError(
                f"unrecognized service artifact in {operation}")
        for artifact in children:
            _require_service_artifact(
                artifact, directory=False, mode=0o600)
            _remove_service_artifact(artifact, directory=False)
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


def _resolve_service_values(values: dict[str, str]) -> dict[str, str]:
    """Apply libpq's local service-file lookup and override semantics."""
    if "service" not in values:
        return values.copy()
    service = values["service"]
    if "servicefile" in values:
        servicefile = values["servicefile"]
    else:
        servicefile = os.environ.get("PGSERVICEFILE")
    if servicefile is not None:
        candidates = [(Path(servicefile), True)]
    else:
        if os.name == "nt":
            appdata = os.environ.get("APPDATA")
            user_file = (Path(appdata) / "postgresql" / ".pg_service.conf"
                         if appdata else None)
        else:
            home = os.environ.get("HOME")
            user_file = Path(home) / ".pg_service.conf" if home else None
        candidates = [(user_file, False)] if user_file else []
    service_values: dict[str, str] | None = None
    for path, required in candidates:
        if not path.is_file():
            if required:
                raise BackupError("cannot resolve PostgreSQL connection service")
            continue
        service_values = _parse_service_file(path, service)
        if service_values is not None:
            break
    if service_values is None:
        system = os.environ.get("PGSYSCONFDIR")
        system_file = (Path(system) / "pg_service.conf" if system else
                       _compiled_system_service_file())
        if system_file is not None and system_file.is_file():
            service_values = _parse_service_file(system_file, service)
    if service_values is None:
        raise BackupError("cannot resolve PostgreSQL connection service")
    service_values.update({
        name: value for name, value in values.items()
        if name not in {"service", "servicefile"}
    })
    return service_values


def _compiled_system_service_file() -> Path | None:
    """Locate libpq's compiled system service directory without connecting."""
    pg_config = shutil.which("pg_config")
    if not pg_config:
        return None
    inherited = {
        name: value for name, value in os.environ.items()
        if name.upper() in {
            "PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "COMSPEC",
            "TEMP", "TMP",
        }
    }
    try:
        result = subprocess.run(
            [pg_config, "--sysconfdir"], stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=inherited,
            shell=False, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode:
        return None
    try:
        lines = result.stdout.decode().splitlines()
    except UnicodeError:
        return None
    if len(lines) != 1 or not lines[0].strip():
        return None
    return Path(lines[0].strip()) / "pg_service.conf"


def _parse_service_file(path: Path, service: str) -> dict[str, str] | None:
    """Parse one service exactly as libpq's local INI parser does."""
    try:
        from psycopg import pq
        keywords = {
            option.keyword.decode() for option in pq.Conninfo.get_defaults()
        }
        content = path.read_bytes()
        parts = content.split(b"\n")
        lines = [
            part + (b"\n" if index < len(parts) - 1 else b"")
            for index, part in enumerate(parts)
        ]
    except (OSError, UnicodeError, ValueError) as exc:
        raise BackupError("cannot resolve PostgreSQL connection service") from exc
    found = False
    result: dict[str, str] = {}
    whitespace = b" \t\r\n\v\f"
    for raw_line in lines:
        if len(raw_line) >= 1023 or b"\0" in raw_line:
            raise BackupError("cannot resolve PostgreSQL connection service")
        line = raw_line.rstrip(whitespace).lstrip(whitespace)
        if not line or line.startswith(b"#"):
            continue
        if line.startswith(b"["):
            if found:
                return result
            try:
                found = line.startswith(f"[{service}]".encode())
            except UnicodeEncodeError as exc:
                raise BackupError(
                    "cannot resolve PostgreSQL connection service") from exc
            continue
        if not found:
            continue
        try:
            key_bytes, value_bytes = line.split(b"=", 1)
            key = key_bytes.decode()
            value = value_bytes.decode()
        except (ValueError, UnicodeError) as exc:
            raise BackupError(
                "cannot resolve PostgreSQL connection service") from exc
        if key in {"service", "servicefile"} or key not in keywords:
            raise BackupError("cannot resolve PostgreSQL connection service")
        result.setdefault(key, value)
    return result if found else None


def _write_private_text(path: Path, text: str) -> None:
    flags = (os.O_CREAT | os.O_EXCL | os.O_WRONLY |
             getattr(os, "O_BINARY", 0))
    fd = os.open(path, flags, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    _chmod(path, 0o600)


def _pgpass_password(value: str) -> str:
    return value.replace("\\", "\\\\").replace(":", "\\:")


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
        service_values = values.copy()
        if "password" in service_values:
            password = service_values.pop("password")
            pass_file = operation / "pgpass.conf"
            _write_private_text(
                pass_file, f"*:*:*:*:{_pgpass_password(password)}\n")
            service_values["passfile"] = str(pass_file)
        service_text = "[memoryd]\n" + "".join(
            f"{name}={value}\n"
            for name, value in sorted(service_values.items()))
        _write_private_text(service_file, service_text)
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
        resolved = _resolve_service_values(values)
        for name, value in resolved.items():
            # libpq strips trailing isspace() bytes from service-file lines
            # and the format has no quoting or escaping for them. Password is
            # the sole exception because its lossless transport is pgpass.
            service_whitespace = " \t\v\f"
            if (name != "password" and value and
                    value[-1] in service_whitespace):
                raise BackupError(
                    "PostgreSQL connection parameter "
                    f"{name} has boundary whitespace that cannot be "
                    "transported safely to local PostgreSQL tools")
        with _service_operation(resolved, root) as connection:
            discovered = tuple(
                resolved[name] for name in ("password", "sslpassword")
                if resolved.get(name))
            yield (*connection, discovered)


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
        with _libpq_service(values) as (safe_dsn, env, discovered):
            all_redactions = tuple(sorted(
                set((*redactions, *discovered)), key=len, reverse=True))
            _run_tool([tool, "--format=custom", "--file", str(destination),
                       "--dbname", safe_dsn], env=env,
                      redactions=all_redactions)
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
        with _libpq_service(values) as (safe_dsn, env, discovered):
            all_redactions = tuple(sorted(
                set((*redactions, *discovered)), key=len, reverse=True))
            _run_tool([tool, "--exit-on-error", "--single-transaction",
                       "--no-owner", "--no-privileges", "--dbname", safe_dsn,
                       str(dump)], env=env,
                      redactions=all_redactions)
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
    config_home = Path(home) if home is not None else _default_home()
    config = _read_config(config_home)
    configured_home = config.get("home")
    if (not os.environ.get("MEMORYD_HOME") and
            isinstance(configured_home, str) and configured_home):
        source_home = Path(configured_home).expanduser()
    else:
        source_home = config_home
    dsn = os.environ.get("MEMORYD_DSN") or config.get("dsn")
    if not isinstance(dsn, str) or not dsn:
        raise BackupError("no PostgreSQL DSN configured")
    try:
        with offline_ownership(source_home, dsn, purpose="backup"):
            return _create_backup_owned(
                output=output, retain=retain, config=config,
                source_home=source_home, dsn=dsn)
    except OwnershipError as exc:
        raise BackupError(str(exc)) from exc


def _create_backup_owned(
        *, output: Path | str | None, retain: int, config: dict[str, Any],
        source_home: Path, dsn: str) -> Path:
    findings = [finding for finding in _doctor_findings(source_home)
                if _finding_value(finding, "severity") == "error"]
    if findings:
        codes = ", ".join(_finding_value(item, "code", "integrity_error")
                          for item in findings)
        raise BackupError(f"doctor found integrity errors: {codes}")
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


def _target_db_user_object_kind(dsn: str) -> str | None:
    # PostgreSQL 16 spreads dumpable objects across many catalogs.  A fresh
    # database is allowed to contain only system schemas, the public schema,
    # built-in languages, and the plpgsql extension.  Shared catalogs are
    # always filtered to the current database so state belonging to another
    # database in the cluster cannot create a false positive.
    query = """
        WITH current_db AS (
            SELECT oid FROM pg_catalog.pg_database
            WHERE datname = pg_catalog.current_database()
        ), user_namespaces AS (
            SELECT oid, nspname FROM pg_catalog.pg_namespace
            WHERE nspname NOT IN ('pg_catalog', 'information_schema')
              AND nspname !~ '^pg_(toast($|_)|temp_)'
        ), user_objects(kind) AS (
            SELECT 'schema' FROM user_namespaces WHERE nspname <> 'public'
            UNION ALL
            SELECT 'relation' FROM pg_catalog.pg_class
             WHERE relnamespace IN (SELECT oid FROM user_namespaces)
            UNION ALL
            SELECT 'function' FROM pg_catalog.pg_proc
             WHERE pronamespace IN (SELECT oid FROM user_namespaces)
            UNION ALL
            SELECT 'type' FROM pg_catalog.pg_type
             WHERE typnamespace IN (SELECT oid FROM user_namespaces)
            UNION ALL
            SELECT 'collation' FROM pg_catalog.pg_collation
             WHERE collnamespace IN (SELECT oid FROM user_namespaces)
            UNION ALL
            SELECT 'conversion' FROM pg_catalog.pg_conversion
             WHERE connamespace IN (SELECT oid FROM user_namespaces)
            UNION ALL
            SELECT 'operator' FROM pg_catalog.pg_operator
             WHERE oprnamespace IN (SELECT oid FROM user_namespaces)
            UNION ALL
            SELECT 'operator class' FROM pg_catalog.pg_opclass
             WHERE opcnamespace IN (SELECT oid FROM user_namespaces)
            UNION ALL
            SELECT 'operator family' FROM pg_catalog.pg_opfamily
             WHERE opfnamespace IN (SELECT oid FROM user_namespaces)
            UNION ALL
            SELECT 'text search configuration' FROM pg_catalog.pg_ts_config
             WHERE cfgnamespace IN (SELECT oid FROM user_namespaces)
            UNION ALL
            SELECT 'text search dictionary' FROM pg_catalog.pg_ts_dict
             WHERE dictnamespace IN (SELECT oid FROM user_namespaces)
            UNION ALL
            SELECT 'text search parser' FROM pg_catalog.pg_ts_parser
             WHERE prsnamespace IN (SELECT oid FROM user_namespaces)
            UNION ALL
            SELECT 'text search template' FROM pg_catalog.pg_ts_template
             WHERE tmplnamespace IN (SELECT oid FROM user_namespaces)
            UNION ALL
            SELECT 'extended statistics' FROM pg_catalog.pg_statistic_ext
             WHERE stxnamespace IN (SELECT oid FROM user_namespaces)
            UNION ALL
            SELECT 'extension' FROM pg_catalog.pg_extension
             WHERE extname <> 'plpgsql'
            UNION ALL
            SELECT 'language' FROM pg_catalog.pg_language
             WHERE lanname NOT IN ('internal', 'c', 'sql', 'plpgsql')
            UNION ALL
            SELECT 'event trigger' FROM pg_catalog.pg_event_trigger
            UNION ALL
            SELECT 'foreign-data wrapper'
              FROM pg_catalog.pg_foreign_data_wrapper
            UNION ALL
            SELECT 'foreign server' FROM pg_catalog.pg_foreign_server
            UNION ALL
            SELECT 'user mapping' FROM pg_catalog.pg_user_mapping
            UNION ALL
            SELECT 'publication' FROM pg_catalog.pg_publication
            UNION ALL
            SELECT 'subscription' FROM pg_catalog.pg_subscription
             WHERE subdbid IN (SELECT oid FROM current_db)
            UNION ALL
            SELECT 'large object' FROM pg_catalog.pg_largeobject_metadata
            UNION ALL
            SELECT 'default privileges' FROM pg_catalog.pg_default_acl
            UNION ALL
            SELECT 'database role setting' FROM pg_catalog.pg_db_role_setting
             WHERE setdatabase IN (SELECT oid FROM current_db)
            UNION ALL
            SELECT 'transform' FROM pg_catalog.pg_transform
            UNION ALL
            SELECT 'security label' FROM pg_catalog.pg_seclabel
            UNION ALL
            SELECT 'database privileges' FROM pg_catalog.pg_database
             WHERE oid IN (SELECT oid FROM current_db) AND datacl IS NOT NULL
            UNION ALL
            SELECT 'database comment' FROM pg_catalog.pg_shdescription
             WHERE classoid = 'pg_catalog.pg_database'::pg_catalog.regclass
               AND objoid IN (SELECT oid FROM current_db)
            UNION ALL
            SELECT 'database security label' FROM pg_catalog.pg_shseclabel
             WHERE classoid = 'pg_catalog.pg_database'::pg_catalog.regclass
               AND objoid IN (SELECT oid FROM current_db)
        )
        SELECT kind FROM user_objects LIMIT 1
        """
    import psycopg
    with psycopg.connect(dsn, connect_timeout=5) as connection:
        row = connection.execute(query).fetchone()
    return str(row[0]) if row is not None else None


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


def _snapshot_file_state(path: Path) -> tuple[int, int, int, int, int]:
    try:
        value = path.stat(follow_symlinks=False)
    except OSError as exc:
        raise BackupError(f"cannot inspect source snapshot entry {path.name}") from exc
    if path.is_symlink() or not stat.S_ISREG(value.st_mode):
        raise BackupError(
            f"source snapshot entry is not a regular file: {path.name}")
    _require_mode(path, 0o600)
    return (value.st_dev, value.st_ino, value.st_size,
            value.st_mtime_ns, value.st_ctime_ns)


def _copy_snapshot_file(source: Path, destination: Path) -> None:
    read_flags = (os.O_RDONLY | getattr(os, "O_BINARY", 0) |
                  getattr(os, "O_NOFOLLOW", 0))
    write_flags = (os.O_WRONLY | os.O_CREAT | os.O_EXCL |
                   getattr(os, "O_BINARY", 0))
    try:
        source_fd = os.open(source, read_flags)
    except OSError as exc:
        raise BackupError(
            f"cannot safely open source snapshot entry {source.name}") from exc
    destination_fd: int | None = None
    try:
        opened = os.fstat(source_fd)
        named = source.stat(follow_symlinks=False)
        if (source.is_symlink() or not stat.S_ISREG(opened.st_mode) or
                not stat.S_ISREG(named.st_mode) or
                (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino)):
            raise BackupError(
                f"source snapshot entry changed during copy: {source.name}")
        destination_fd = os.open(destination, write_flags, 0o600)
        while True:
            chunk = os.read(source_fd, 1024 * 1024)
            if not chunk:
                break
            offset = 0
            while offset < len(chunk):
                offset += os.write(destination_fd, chunk[offset:])
        os.fsync(destination_fd)
        if POSIX_MODE_ENFORCED:
            os.fchmod(destination_fd, 0o600)
    except BackupError:
        raise
    except OSError as exc:
        raise BackupError(
            f"cannot copy source snapshot entry {source.name}: {exc}") from exc
    finally:
        if destination_fd is not None:
            os.close(destination_fd)
        os.close(source_fd)


def _sync_private_snapshot_file(path: Path) -> None:
    _chmod(path, 0o600)
    value = path.stat(follow_symlinks=False)
    if path.is_symlink() or not stat.S_ISREG(value.st_mode):
        raise BackupError(f"unsafe private snapshot entry: {path.name}")
    _require_mode(path, 0o600)
    flags = os.O_RDWR | getattr(os, "O_BINARY", 0)
    fd = os.open(path, flags)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _cleanup_staging(path: Path, primary: BaseException | None) -> None:
    if not os.path.lexists(path):
        return
    try:
        shutil.rmtree(path)
    except BaseException as exc:
        cleanup = BackupError(f"cannot remove private staging directory {path}")
        if primary is None:
            raise cleanup from exc
        primary.add_note(str(cleanup))


@contextmanager
def _private_snapshot_copy(source: Path, parent: Path, *, target_name: str):
    if not SNAPSHOT_RE.fullmatch(source.name):
        raise BackupError("snapshot name is not a generated v1 name")
    try:
        source_value = source.stat(follow_symlinks=False)
    except OSError as exc:
        raise BackupError(f"snapshot is unreadable: {exc}") from exc
    if source.is_symlink() or not stat.S_ISDIR(source_value.st_mode):
        raise BackupError("snapshot must be a real directory")
    _require_mode(source, 0o700)
    source_identity = (source_value.st_dev, source_value.st_ino)
    try:
        entries = {entry.name for entry in source.iterdir()}
    except OSError as exc:
        raise BackupError(f"snapshot is unreadable: {exc}") from exc
    if entries != SNAPSHOT_FILES:
        raise BackupError(
            f"snapshot file allowlist mismatch: {sorted(entries)!r}")

    staging = Path(tempfile.mkdtemp(
        prefix=f".{target_name}.snapshot-", dir=parent))
    primary: BaseException | None = None
    try:
        _chmod(staging, 0o700)
        for name in sorted(SNAPSHOT_FILES):
            source_file = source / name
            before = _snapshot_file_state(source_file)
            destination = staging / name
            _copy_snapshot_file(source_file, destination)
            _sync_private_snapshot_file(destination)
            after = _snapshot_file_state(source_file)
            if after != before:
                raise BackupError(
                    f"source snapshot entry changed during copy: {name}")
        current = source.stat(follow_symlinks=False)
        current_entries = {entry.name for entry in source.iterdir()}
        if ((current.st_dev, current.st_ino) != source_identity or
                source.is_symlink() or not stat.S_ISDIR(current.st_mode) or
                current_entries != SNAPSHOT_FILES):
            raise BackupError("source snapshot changed during copy")
        _fsync_directory(staging)
        result = verify_snapshot(staging, require_generated_name=False)
        if not result.ok:
            raise BackupError(
                f"private snapshot verification failed: {result.reason}")
        yield staging
    except BaseException as exc:
        primary = exc
        raise
    finally:
        _cleanup_staging(staging, primary)


def restore_backup(snapshot: Path | str, *, target_dsn: str,
                   target_home: Path | str) -> Path:
    source = Path(snapshot)
    home = Path(target_home)
    try:
        with offline_ownership(home, target_dsn, purpose="restore"):
            return _restore_backup_owned(
                source, target_dsn=target_dsn, home=home)
    except OwnershipError as exc:
        raise BackupError(str(exc)) from exc


def _restore_backup_owned(
        source: Path, *, target_dsn: str, home: Path) -> Path:
    home.parent.mkdir(parents=True, exist_ok=True)
    with _private_snapshot_copy(
            source, home.parent, target_name=home.name) as private_source:
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
            object_kind = _target_db_user_object_kind(target_dsn)
            if object_kind is not None:
                raise BackupError(
                    "target database already has user objects "
                    f"(found {object_kind})")
        except BackupError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise BackupError(f"cannot validate target database: {exc}") from exc

        staging = Path(tempfile.mkdtemp(
            prefix=f".{home.name}.restore-", dir=home.parent))
        database_risk = False
        primary: BaseException | None = None
        try:
            _chmod(staging, 0o700)
            _extract_memory_tar(private_source / "memory.tar.gz", staging)
            config = json.loads(
                (private_source / "config.sanitized.json").read_text(
                    encoding="utf-8"))
            config["dsn"] = target_dsn
            config["home"] = str(home)
            config_path = staging / "config.json"
            config_path.write_text(
                json.dumps(config, indent=2, sort_keys=True), encoding="utf-8")
            _chmod(config_path, 0o600)
            _require_mode(staging, 0o700)
            _require_mode(config_path, 0o600)
            database_risk = True
            _restore_database(private_source / "database.dump", target_dsn)
            _atomic_rename(staging, home)
            return home
        except Exception as exc:  # noqa: BLE001
            detail = str(exc)
            suffix = ("; partial empty-target DB risk: pg_restore may have "
                      "created objects; inspect or recreate that target database"
                      if database_risk else "")
            error = BackupError(f"restore failed: {detail}{suffix}")
            primary = error
            raise error from exc
        except BaseException as exc:
            primary = exc
            raise
        finally:
            _cleanup_staging(staging, primary)


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
