from __future__ import annotations

import os
import queue
import socket
import stat
import subprocess
import sys
import threading
import time
import types
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler
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


@pytest.mark.skipif(os.name == "nt", reason="POSIX directory trust")
def test_home_lock_refuses_writable_nonsticky_parent(tmp_path):
    from memoryd import ownership

    parent = tmp_path / "unsafe-parent"
    parent.mkdir()
    parent.chmod(0o777)
    home = parent / "home"

    with pytest.raises(ownership.OwnershipError, match="unsafe.*parent"):
        with ownership.home_ownership(home, purpose="server"):
            pytest.fail("lock acquired through unsafe parent")


@pytest.mark.skipif(os.name == "nt", reason="POSIX flock replacement race")
def test_home_lock_revalidates_identity_after_flock_replacement(
        monkeypatch, tmp_path):
    import fcntl
    from memoryd import ownership

    home = tmp_path / "home"
    path = ownership._home_lock_path(home)
    displaced = path.with_suffix(".displaced")
    real_flock = fcntl.flock
    replacement_fd: int | None = None
    replacement_acquired = False
    replaced = False

    def replace_after_flock(fd, operation):
        nonlocal replacement_fd, replacement_acquired, replaced
        real_flock(fd, operation)
        if operation == fcntl.LOCK_EX | fcntl.LOCK_NB and not replaced:
            replaced = True
            os.replace(path, displaced)
            replacement_fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_EXCL,
                                     0o600)
            os.write(replacement_fd, b"\0")
            real_flock(
                replacement_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            replacement_acquired = True

    monkeypatch.setattr(fcntl, "flock", replace_after_flock)
    try:
        with pytest.raises(ownership.OwnershipError, match="unsafe.*lock"):
            with ownership.home_ownership(home, purpose="backup"):
                pytest.fail("replaced lock yielded concurrent ownership")
        assert replacement_acquired
    finally:
        if replacement_fd is not None:
            real_flock(replacement_fd, fcntl.LOCK_UN)
            os.close(replacement_fd)
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


def test_server_ownership_waits_for_real_inflight_handler(
        monkeypatch, tmp_path):
    from memoryd import server

    handler_started = threading.Event()
    release_handler = threading.Event()
    ownership_exited = threading.Event()
    main_errors: list[BaseException] = []
    servers = []

    class BlockingHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            handler_started.set()
            self.server.shutdown()
            assert release_handler.wait(timeout=5)
            self.send_response(200)
            self.end_headers()

        def log_message(self, *_args):
            pass

    @contextmanager
    def own(_home, _dsn, *, purpose):
        assert purpose == "server"
        try:
            yield
        finally:
            ownership_exited.set()

    real_http_server = server.ThreadingHTTPServer

    class TrackingHTTPServer(real_http_server):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            servers.append(self)

    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()

    monkeypatch.setattr(server, "offline_ownership", own)
    monkeypatch.setattr(server, "_secure_server_home", lambda: None)
    monkeypatch.setattr(server.CFG, "ensure_dirs", lambda: None)
    monkeypatch.setattr(server.CFG, "port", port)
    monkeypatch.setattr(server, "Handler", BlockingHandler)
    monkeypatch.setattr(server, "ThreadingHTTPServer", TrackingHTTPServer)
    monkeypatch.setattr(server, "_drain_spool_bg", lambda: None)
    monkeypatch.setattr(server, "CAPTURE_Q", queue.Queue())

    def run_server():
        try:
            server.main()
        except BaseException as exc:  # noqa: BLE001
            main_errors.append(exc)

    daemon = threading.Thread(target=run_server, daemon=True)
    daemon.start()

    client_errors: list[BaseException] = []

    def request():
        try:
            deadline = time.monotonic() + 5
            while True:
                try:
                    client = socket.create_connection(
                        ("127.0.0.1", port), timeout=1)
                    break
                except ConnectionRefusedError:
                    if time.monotonic() >= deadline:
                        raise
                    time.sleep(0.01)
            with client:
                client.settimeout(5)
                client.sendall(b"GET / HTTP/1.0\r\nHost: localhost\r\n\r\n")
                while client.recv(4096):
                    pass
        except BaseException as exc:  # noqa: BLE001
            client_errors.append(exc)

    client = threading.Thread(target=request)
    client.start()
    try:
        for _ in range(100):
            if handler_started.wait(timeout=0.05):
                break
            if not daemon.is_alive():
                break
        assert handler_started.is_set(), main_errors
        assert not ownership_exited.wait(timeout=1.5)
    finally:
        release_handler.set()
        if daemon.is_alive() and servers:
            servers[0].shutdown()
        client.join(timeout=5)
        daemon.join(timeout=5)

    assert not daemon.is_alive()
    assert ownership_exited.is_set()
    assert not main_errors
    assert not client_errors
