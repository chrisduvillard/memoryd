from __future__ import annotations

import os
import stat
import subprocess
import sys
import types
from contextlib import contextmanager
from pathlib import Path

import pytest


def _fake_psycopg(monkeypatch, connect) -> None:
    monkeypatch.setitem(
        sys.modules, "psycopg", types.SimpleNamespace(connect=connect))


def test_home_lock_is_sibling_owner_only_and_preserves_absent_home(tmp_path):
    from memoryd import ownership

    home = tmp_path / "missing-home"

    with ownership.home_ownership(home, purpose="restore"):
        lock = ownership._home_lock_path(home)
        assert lock.parent == home.parent
        assert lock.is_file()
        assert not home.exists()
        if os.name != "nt":
            assert stat.S_IMODE(lock.stat().st_mode) == 0o600

    assert not home.exists()


def test_home_lock_canonicalizes_aliases_and_releases_after_error(tmp_path):
    from memoryd import ownership

    parent = tmp_path / "parent"
    parent.mkdir()
    home = parent / "home"
    alias = parent / "child" / ".." / "home"

    with pytest.raises(RuntimeError, match="injected"):
        with ownership.home_ownership(home, purpose="server"):
            with pytest.raises(ownership.OwnershipError, match="in use"):
                with ownership.home_ownership(alias, purpose="backup"):
                    pytest.fail("contending alias acquired the same home")
            raise RuntimeError("injected")

    with ownership.home_ownership(alias, purpose="backup"):
        pass


def test_home_lock_refuses_unsafe_existing_artifact(tmp_path):
    from memoryd import ownership

    home = tmp_path / "home"
    lock = ownership._home_lock_path(home)
    lock.mkdir()

    with pytest.raises(ownership.OwnershipError, match="unsafe.*lock"):
        with ownership.home_ownership(home, purpose="backup"):
            pytest.fail("unsafe artifact was opened")


def test_home_lock_refuses_symlink_artifact_without_touching_target(tmp_path):
    from memoryd import ownership

    home = tmp_path / "memory"
    lock = ownership._home_lock_path(home)
    target = tmp_path / "foreign-lock"
    target.write_bytes(b"foreign")
    try:
        lock.symlink_to(target)
    except OSError as exc:
        pytest.skip(f"symlinks unavailable: {exc}")

    with pytest.raises(ownership.OwnershipError, match="unsafe.*lock"):
        with ownership.home_ownership(home, purpose="restore"):
            pytest.fail("symlink artifact was opened")

    assert lock.is_symlink()
    assert target.read_bytes() == b"foreign"


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode enforcement")
def test_home_lock_refuses_group_readable_artifact(tmp_path):
    from memoryd import ownership

    home = tmp_path / "home"
    lock = ownership._home_lock_path(home)
    lock.write_bytes(b"\0")
    lock.chmod(0o644)

    with pytest.raises(ownership.OwnershipError, match="unsafe.*lock"):
        with ownership.home_ownership(home, purpose="backup"):
            pytest.fail("unsafe artifact was opened")


def test_home_lock_is_released_when_owner_process_crashes(tmp_path):
    from memoryd import ownership

    home = tmp_path / "home"
    script = (
        "import os,sys\n"
        "from pathlib import Path\n"
        "from memoryd.ownership import home_ownership\n"
        "with home_ownership(Path(sys.argv[1]), purpose='server'):\n"
        " print('locked', flush=True)\n"
        " os._exit(0)\n")
    child = subprocess.run(
        [sys.executable, "-c", script, str(home)], capture_output=True,
        text=True, cwd=Path(__file__).resolve().parents[1], timeout=10)

    assert child.returncode == 0, child.stderr
    assert child.stdout.strip() == "locked"
    with ownership.home_ownership(home, purpose="backup"):
        pass


def test_database_lock_is_session_scoped_nonblocking_and_secret_safe(
        monkeypatch):
    from memoryd import ownership

    calls: list[tuple[str, tuple]] = []

    class Cursor:
        def fetchone(self):
            return (True,)

    class Connection:
        closed = False

        def execute(self, sql, params):
            calls.append((sql, params))
            return Cursor()

        def close(self):
            self.closed = True

    connection = Connection()
    _fake_psycopg(monkeypatch, lambda *_args, **_kwargs: connection)

    with ownership.database_ownership(
            "postgresql://operator:secret@localhost/memoryd",
            purpose="backup"):
        assert not connection.closed

    assert connection.closed
    assert len(calls) == 1
    assert "pg_try_advisory_lock" in calls[0][0]
    assert "current_database()" in calls[0][0]
    assert calls[0][1] == (ownership.DATABASE_LOCK_NAMESPACE,)


def test_database_lock_rejects_same_database_but_allows_unrelated_database(
        monkeypatch):
    from memoryd import ownership

    held: set[str] = set()

    class Cursor:
        def __init__(self, acquired):
            self.acquired = acquired

        def fetchone(self):
            return (self.acquired,)

    class Connection:
        def __init__(self, database):
            self.database = database
            self.acquired = False

        def execute(self, _sql, _params):
            self.acquired = self.database not in held
            if self.acquired:
                held.add(self.database)
            return Cursor(self.acquired)

        def close(self):
            if self.acquired:
                held.remove(self.database)

    def connect(dsn, **_kwargs):
        return Connection(dsn.rsplit("/", 1)[-1])

    _fake_psycopg(monkeypatch, connect)

    with ownership.database_ownership("postgresql:///one", purpose="server"):
        with ownership.database_ownership(
                "postgresql:///two", purpose="backup"):
            pass
        with pytest.raises(ownership.OwnershipError, match="in use"):
            with ownership.database_ownership(
                    "postgresql:///one", purpose="restore"):
                pytest.fail("same database lock was acquired twice")
    assert held == set()


def test_database_lock_failure_closes_session_and_redacts_dsn(monkeypatch):
    from memoryd import ownership

    class Connection:
        closed = False

        def execute(self, _sql, _params):
            raise RuntimeError("diagnostic includes service-secret")

        def close(self):
            self.closed = True

    connection = Connection()
    _fake_psycopg(monkeypatch, lambda *_args, **_kwargs: connection)

    with pytest.raises(ownership.OwnershipError) as exc:
        with ownership.database_ownership(
                "postgresql://operator:service-secret@localhost/memoryd",
                purpose="restore"):
            pytest.fail("failed database lock yielded")

    assert connection.closed
    assert "service-secret" not in str(exc.value)


def test_server_holds_home_and_database_ownership_before_workers_and_bind(
        monkeypatch):
    from memoryd import server

    events: list[str] = []

    @contextmanager
    def own(home, dsn, *, purpose):
        assert home == server.CFG.home
        assert dsn == server.CFG.dsn
        assert purpose == "server"
        events.append("ownership-enter")
        try:
            yield
        finally:
            events.append("ownership-exit")

    class Thread:
        def __init__(self, *, target, **_kwargs):
            assert events[0] == "ownership-enter"
            self.name = target.__name__

        def start(self):
            events.append(f"start:{self.name}")

        def join(self):
            events.append(f"join:{self.name}")

    class Queue:
        def join(self):
            events.append("queue-join")

        def put(self, _value):
            events.append("queue-stop")

    class HTTPServer:
        def __init__(self, *_args):
            assert events[0] == "ownership-enter"
            events.append("bind")

        def serve_forever(self):
            events.append("serve")
            raise RuntimeError("injected serve failure")

        def server_close(self):
            events.append("server-close")

    monkeypatch.setattr(server, "offline_ownership", own, raising=False)
    monkeypatch.setattr(
        server, "_secure_server_home",
        lambda: events.append("secure-home"), raising=False)
    monkeypatch.setattr(server.CFG, "ensure_dirs", lambda: events.append("dirs"))
    monkeypatch.setattr(server.threading, "Thread", Thread)
    monkeypatch.setattr(server, "ThreadingHTTPServer", HTTPServer)
    monkeypatch.setattr(server, "CAPTURE_Q", Queue())

    with pytest.raises(RuntimeError, match="serve failure"):
        server.main()

    assert events[0] == "ownership-enter"
    assert events[-1] == "ownership-exit"
    assert events.index("ownership-enter") < events.index("secure-home")
    assert events.index("secure-home") < events.index("dirs")
    assert events.index("ownership-enter") < events.index("dirs")
    assert events.index("ownership-enter") < events.index("start:_capture_worker")
    assert events.index("ownership-enter") < events.index("bind")
    assert events.index("serve") < events.index("server-close")
    assert events.index("server-close") < events.index("queue-join")
    assert events.index("queue-stop") < events.index("join:_capture_worker")
    assert events.index("join:_drain_spool_bg") < events.index("ownership-exit")


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode enforcement")
def test_server_secures_home_under_permissive_umask(monkeypatch, tmp_path):
    from memoryd import server

    home = tmp_path / "memory"
    monkeypatch.setattr(server.CFG, "home", home)
    previous = os.umask(0)
    try:
        server._secure_server_home()
    finally:
        os.umask(previous)

    assert stat.S_IMODE(home.stat().st_mode) == 0o700
