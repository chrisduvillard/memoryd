"""Authoritative process and database ownership for offline-safe operations."""
from __future__ import annotations

import errno
import os
import stat
from contextlib import contextmanager
from pathlib import Path

DATABASE_LOCK_NAMESPACE = "memoryd:database-owner:"


class OwnershipError(RuntimeError):
    """A home or database is already owned, unsafe, or cannot be locked."""


def _canonical_home(home: Path | str) -> Path:
    return Path(home).expanduser().resolve(strict=False)


def _home_lock_path(home: Path | str) -> Path:
    canonical = _canonical_home(home)
    name = canonical.name or "root"
    return canonical.parent / f".{name}.memoryd-owner.lock"


def _validate_lock_descriptor(fd: int, path: Path) -> None:
    try:
        value = os.fstat(fd)
        path_value = path.stat(follow_symlinks=False)
    except OSError as exc:
        raise OwnershipError(f"cannot inspect memoryd ownership lock {path}") from exc
    if (path.is_symlink() or not stat.S_ISREG(value.st_mode) or
            not stat.S_ISREG(path_value.st_mode) or
            (value.st_dev, value.st_ino) !=
            (path_value.st_dev, path_value.st_ino)):
        raise OwnershipError(f"unsafe memoryd ownership lock {path}")
    if hasattr(os, "getuid") and value.st_uid != os.getuid():
        raise OwnershipError(f"unsafe foreign memoryd ownership lock {path}")
    if os.name != "nt" and stat.S_IMODE(value.st_mode) != 0o600:
        raise OwnershipError(f"unsafe mode on memoryd ownership lock {path}")


def _open_home_lock(home: Path | str) -> tuple[int, Path]:
    path = _home_lock_path(home)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise OwnershipError(
            f"cannot create memoryd ownership lock directory {path.parent}") from exc
    common = os.O_RDWR | getattr(os, "O_BINARY", 0)
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    created = False
    try:
        fd = os.open(path, common | nofollow | os.O_CREAT | os.O_EXCL, 0o600)
        created = True
    except FileExistsError:
        try:
            fd = os.open(path, common | nofollow)
        except OSError as exc:
            raise OwnershipError(f"unsafe memoryd ownership lock {path}") from exc
    except OSError as exc:
        raise OwnershipError(f"cannot create memoryd ownership lock {path}") from exc
    try:
        if created:
            if os.name != "nt":
                os.fchmod(fd, 0o600)
            os.write(fd, b"\0")
            os.fsync(fd)
        _validate_lock_descriptor(fd, path)
    except BaseException:
        os.close(fd)
        raise
    return fd, path


@contextmanager
def home_ownership(home: Path | str, *, purpose: str):
    """Hold one canonical home's nonblocking OS ownership lock."""
    fd, path = _open_home_lock(home)
    acquired = False
    primary: BaseException | None = None
    try:
        try:
            if os.name == "nt":
                import msvcrt
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
        except OSError as exc:
            busy = (os.name == "nt" or
                    exc.errno in {errno.EACCES, errno.EAGAIN})
            if busy:
                raise OwnershipError(
                    f"memoryd home is in use; cannot start {purpose}") from exc
            raise OwnershipError(
                f"cannot acquire memoryd ownership lock {path}") from exc
        yield
    except BaseException as exc:
        primary = exc
        raise
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
        except OSError as exc:
            cleanup = OwnershipError(
                f"cannot release memoryd ownership lock {path}")
            if primary is None:
                raise cleanup from exc
            primary.add_note(str(cleanup))
        finally:
            os.close(fd)


@contextmanager
def database_ownership(dsn: str, *, purpose: str):
    """Hold the target database's nonblocking session advisory lock."""
    connection = None
    acquired = False
    primary: BaseException | None = None
    try:
        try:
            import psycopg
            connection = psycopg.connect(
                dsn, autocommit=True, connect_timeout=5)
            row = connection.execute(
                "SELECT pg_try_advisory_lock(hashtextextended("
                "%s || current_database(), 0))",
                (DATABASE_LOCK_NAMESPACE,)).fetchone()
        except Exception as exc:  # noqa: BLE001
            raise OwnershipError(
                f"cannot establish exclusive database ownership for {purpose}") from exc
        acquired = bool(row and row[0] is True)
        if not acquired:
            raise OwnershipError(
                f"memoryd database is in use; cannot start {purpose}")
        yield
    except BaseException as exc:
        primary = exc
        raise
    finally:
        if connection is not None:
            try:
                connection.close()
            except Exception as exc:  # noqa: BLE001
                cleanup = OwnershipError(
                    f"cannot release database ownership for {purpose}")
                if primary is None:
                    raise cleanup from exc
                primary.add_note(str(cleanup))


@contextmanager
def offline_ownership(home: Path | str, dsn: str, *, purpose: str):
    """Acquire home then database ownership in the global lock order."""
    with home_ownership(home, purpose=purpose):
        with database_ownership(dsn, purpose=purpose):
            yield
