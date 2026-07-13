from __future__ import annotations

import hashlib
import io
import json
import os
import socket
import stat
import struct
import sys
import tarfile
import threading
import types
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import pytest

from memoryd import backup
from memoryd import cli


def _libpq_info(conninfo: str, env: dict[str, str], monkeypatch) -> dict[str, str]:
    from psycopg import pq

    with monkeypatch.context() as patch:
        for name in ("PGPASSWORD", "PGSSLPASSWORD", "PGSERVICE",
                     "PGSERVICEFILE"):
            patch.delenv(name, raising=False)
        patch.setenv("PGSERVICEFILE", env["PGSERVICEFILE"])
        connection = pq.PGconn.connect_start(conninfo.encode())
        try:
            return {
                option.keyword.decode(): option.val.decode()
                for option in connection.info if option.val is not None
            }
        finally:
            connection.finish()


def _capture_libpq_password(
        conninfo: str, env: dict[str, str], monkeypatch) -> str:
    import psycopg

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    listener.settimeout(5)
    host, port = listener.getsockname()
    captured: list[str] = []
    server_errors: list[BaseException] = []

    def receive_packet(connection: socket.socket) -> bytes:
        size = struct.unpack("!I", connection.recv(4))[0]
        payload = b""
        while len(payload) < size - 4:
            payload += connection.recv(size - 4 - len(payload))
        return payload

    def serve() -> None:
        try:
            connection, _address = listener.accept()
            with connection:
                connection.settimeout(5)
                receive_packet(connection)
                connection.sendall(b"R" + struct.pack("!II", 8, 3))
                kind = connection.recv(1)
                assert kind == b"p"
                password = receive_packet(connection)
                captured.append(password.removesuffix(b"\0").decode())
                fields = b"SFATAL\0C28P01\0Mauthentication failed\0\0"
                connection.sendall(b"E" + struct.pack("!I", len(fields) + 4) + fields)
        except BaseException as exc:  # noqa: BLE001
            server_errors.append(exc)
        finally:
            listener.close()

    thread = threading.Thread(target=serve)
    thread.start()
    try:
        with monkeypatch.context() as patch:
            for name in ("PGPASSWORD", "PGSSLPASSWORD", "PGSERVICE",
                         "PGSERVICEFILE"):
                patch.delenv(name, raising=False)
            patch.setenv("PGSERVICEFILE", env["PGSERVICEFILE"])
            with pytest.raises(psycopg.OperationalError):
                psycopg.connect(
                    conninfo, host=host, port=port, connect_timeout=2,
                    sslmode="disable", gssencmode="disable")
    finally:
        thread.join(timeout=6)
    assert not thread.is_alive()
    assert not server_errors
    assert captured
    return captured[0]


def _home(tmp_path: Path) -> Path:
    home = tmp_path / "memory"
    (home / "archive" / "objects").mkdir(parents=True)
    (home / "archive" / "objects" / "one").write_bytes(b"archive")
    (home / "spool" / "incoming").mkdir(parents=True)
    (home / "spool" / "incoming" / "job.json").write_text('{"job": 1}')
    (home / "backups" / "never.txt").parent.mkdir(parents=True)
    (home / "backups" / "never.txt").write_text("excluded")
    (home / "digest").mkdir()
    (home / "digest" / "never.md").write_text("excluded")
    (home / "config.json").write_text(json.dumps({
        "dsn": "postgresql://postgres:db-secret@localhost/memoryd",
        "port": 7437,
        "home": str(home),
        "env": {
            "ANTHROPIC_API_KEY": "api-secret-value",
            "MEMORYD_LLM_MODEL": "claude-test",
        },
        "visas": {"default": "allow"},
    }))
    return home


def _prepare(monkeypatch, home: Path) -> None:
    for name in backup.KNOWN_SECRET_ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(
        backup, "_utc_now",
        lambda: datetime(2026, 7, 13, 1, 23, 45, tzinfo=timezone.utc))
    monkeypatch.setattr(backup, "_doctor_findings", lambda _home: [])
    monkeypatch.setattr(
        backup, "_database_migrations",
        lambda _dsn: ["001_init.sql", "002_extraction.sql"], raising=False)
    monkeypatch.setattr(
        backup, "_dump_database",
        lambda _dsn, path: path.write_bytes(b"PGDMP\x00unit-test"))

    @contextmanager
    def ownership_stub(_home, _dsn, *, purpose):
        assert purpose in {"backup", "restore"}
        yield

    monkeypatch.setattr(
        backup, "offline_ownership", ownership_stub, raising=False)


def _manifest(snapshot: Path) -> dict:
    return json.loads((snapshot / "manifest.json").read_text())


def _refresh_file_entry(snapshot: Path, name: str) -> None:
    manifest = _manifest(snapshot)
    data = (snapshot / name).read_bytes()
    manifest["files"][name] = {
        "sha256": hashlib.sha256(data).hexdigest(),
        "bytes": len(data),
    }
    (snapshot / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True))


def _write_tar(path: Path, members: list[tuple[str, bytes | None]]) -> None:
    with tarfile.open(path, "w:gz") as archive:
        for name, payload in members:
            member = tarfile.TarInfo(name)
            if payload is None:
                member.type = tarfile.DIRTYPE
                archive.addfile(member)
            else:
                member.size = len(payload)
                archive.addfile(member, io.BytesIO(payload))


def test_create_produces_verified_secret_free_owner_only_snapshot(
        monkeypatch, tmp_path):
    home = _home(tmp_path)
    output = tmp_path / "snapshots"
    _prepare(monkeypatch, home)

    snapshot = backup.create_backup(output=output, home=home, retain=14)

    assert snapshot.name == "20260713T012345Z-v1"
    assert {path.name for path in snapshot.iterdir()} == {
        "database.dump", "memory.tar.gz", "config.sanitized.json",
        "manifest.json",
    }
    result = backup.verify_snapshot(snapshot)
    assert result.ok, result.reason
    manifest = _manifest(snapshot)
    assert manifest["schema_version"] == 1
    assert manifest["required_secret_env_names"] == ["ANTHROPIC_API_KEY"]
    assert manifest["db_migrations"]
    combined = (snapshot / "manifest.json").read_text() + (
        snapshot / "config.sanitized.json").read_text()
    assert "api-secret-value" not in combined
    assert "db-secret" not in combined
    sanitized = json.loads((snapshot / "config.sanitized.json").read_text())
    assert "dsn" not in sanitized
    assert sanitized["env"] == {"MEMORYD_LLM_MODEL": "claude-test"}
    with tarfile.open(snapshot / "memory.tar.gz", "r:gz") as archive:
        names = {member.name for member in archive.getmembers()}
    assert "archive/objects/one" in names
    assert "spool/incoming/job.json" in names
    assert not any(name.startswith(("backups", "digest")) for name in names)
    if os.name != "nt":
        assert stat.S_IMODE(snapshot.stat().st_mode) == 0o700
        assert all(stat.S_IMODE(path.stat().st_mode) == 0o600
                   for path in snapshot.iterdir())


def test_create_does_not_treat_http_503_as_offline_ownership(
        monkeypatch, tmp_path):
    home = _home(tmp_path)
    _prepare(monkeypatch, home)
    called = False
    health_called = False

    def http_503():
        nonlocal health_called
        health_called = True
        return {"ok": False, "status": 503}

    monkeypatch.setattr(cli, "_health", http_503)

    def dump(_dsn, path):
        nonlocal called
        called = True
        path.write_bytes(b"PGDMP\x00health-is-diagnostic")

    monkeypatch.setattr(backup, "_dump_database", dump)

    snapshot = backup.create_backup(output=tmp_path / "out", home=home)

    assert called
    assert not health_called
    assert snapshot.is_dir()


def test_live_unhealthy_daemon_home_lock_blocks_backup_without_health_probe(
        monkeypatch, tmp_path):
    from memoryd import ownership

    home = _home(tmp_path)
    _prepare(monkeypatch, home)
    monkeypatch.setattr(
        cli, "_health",
        lambda: pytest.fail("HTTP health must not decide offline ownership"))
    monkeypatch.setattr(backup, "offline_ownership", ownership.offline_ownership)

    @contextmanager
    def database_available(_dsn, *, purpose):
        assert purpose == "backup"
        yield

    monkeypatch.setattr(ownership, "database_ownership", database_available)

    with ownership.home_ownership(home, purpose="server"):
        with pytest.raises(backup.BackupError, match="home is in use"):
            backup.create_backup(output=tmp_path / "out", home=home)


def test_server_start_cannot_race_into_backup_after_offline_gate(
        monkeypatch, tmp_path):
    from memoryd import ownership

    home = _home(tmp_path)
    _prepare(monkeypatch, home)
    monkeypatch.setattr(backup, "offline_ownership", ownership.offline_ownership)

    @contextmanager
    def database_available(_dsn, *, purpose):
        assert purpose == "backup"
        yield

    monkeypatch.setattr(ownership, "database_ownership", database_available)
    dump_started = threading.Event()
    release_dump = threading.Event()

    def dump(_dsn, path):
        dump_started.set()
        assert release_dump.wait(timeout=5)
        path.write_bytes(b"PGDMP\x00locked")

    monkeypatch.setattr(backup, "_dump_database", dump)
    result: list[Path] = []
    errors: list[BaseException] = []

    def create() -> None:
        try:
            result.append(backup.create_backup(
                output=tmp_path / "out", home=home))
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    thread = threading.Thread(target=create)
    thread.start()
    assert dump_started.wait(timeout=5)
    try:
        with pytest.raises(ownership.OwnershipError, match="home is in use"):
            with ownership.home_ownership(home, purpose="server"):
                pytest.fail("server acquired a home during backup")
    finally:
        release_dump.set()
        thread.join(timeout=10)

    assert not thread.is_alive()
    assert errors == []
    assert len(result) == 1


def test_create_honors_relocated_home_from_config(monkeypatch, tmp_path):
    config_home = _home(tmp_path)
    actual_home = tmp_path / "relocated"
    actual_home.mkdir()
    (config_home / "archive").rename(actual_home / "archive")
    (config_home / "spool").rename(actual_home / "spool")
    config = json.loads((config_home / "config.json").read_text())
    config["home"] = str(actual_home)
    (config_home / "config.json").write_text(json.dumps(config))
    _prepare(monkeypatch, config_home)

    snapshot = backup.create_backup(
        output=tmp_path / "out", home=config_home)

    with tarfile.open(snapshot / "memory.tar.gz", "r:gz") as archive:
        assert "archive/objects/one" in archive.getnames()


def test_create_records_api_key_name_present_only_in_environment(
        monkeypatch, tmp_path):
    home = _home(tmp_path)
    config = json.loads((home / "config.json").read_text())
    config["env"] = {"MEMORYD_LLM_MODEL": "claude-test"}
    (home / "config.json").write_text(json.dumps(config))
    _prepare(monkeypatch, home)
    monkeypatch.setenv("OPENAI_API_KEY", "environment-only-secret")

    snapshot = backup.create_backup(output=tmp_path / "out", home=home)

    assert _manifest(snapshot)["required_secret_env_names"] == ["OPENAI_API_KEY"]
    assert "environment-only-secret" not in (
        snapshot / "manifest.json").read_text()


@pytest.mark.parametrize("applied", [
    ["001_init.sql"],
    ["001_init.sql", "009_site_extension.sql"],
])
def test_create_manifest_uses_actual_database_migration_rows(
        monkeypatch, tmp_path, applied):
    home = _home(tmp_path)
    _prepare(monkeypatch, home)
    monkeypatch.setattr(backup, "_database_migrations", lambda _dsn: applied)

    snapshot = backup.create_backup(output=tmp_path / "out", home=home)

    assert _manifest(snapshot)["db_migrations"] == applied


def test_create_refuses_missing_or_invalid_migration_ledger(monkeypatch, tmp_path):
    home = _home(tmp_path)
    _prepare(monkeypatch, home)
    monkeypatch.setattr(
        backup, "_database_migrations",
        lambda _dsn: (_ for _ in ()).throw(
            backup.BackupError("schema_migrations table missing")))

    with pytest.raises(backup.BackupError, match="schema_migrations"):
        backup.create_backup(output=tmp_path / "out", home=home)


@pytest.mark.parametrize("rows", [
    [],
    [("001_init.sql",), ("001_init.sql",)],
    [("../001_init.sql",)],
    [("002_extraction.sql",), ("001_init.sql",)],
])
def test_database_migration_query_rejects_invalid_actual_rows(monkeypatch, rows):
    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def execute(self, query):
            assert query == "SELECT filename FROM schema_migrations ORDER BY filename"
            return types.SimpleNamespace(fetchall=lambda: rows)

    import psycopg
    monkeypatch.setattr(psycopg, "connect", lambda *_args, **_kwargs: Connection())

    with pytest.raises(backup.BackupError, match="ledger"):
        backup._database_migrations("postgresql:///memoryd")


def test_database_migration_query_reports_missing_table(monkeypatch):
    import psycopg
    monkeypatch.setattr(
        psycopg, "connect",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("relation schema_migrations does not exist")))

    with pytest.raises(backup.BackupError, match="schema_migrations"):
        backup._database_migrations("postgresql:///memoryd")


def test_create_refuses_doctor_errors_and_dead_letters(monkeypatch, tmp_path):
    home = _home(tmp_path)
    _prepare(monkeypatch, home)
    monkeypatch.setattr(backup, "_doctor_findings", lambda _home: [
        {"severity": "error", "code": "dead_letter_jobs"}])

    with pytest.raises(backup.BackupError, match="dead_letter_jobs"):
        backup.create_backup(output=tmp_path / "out", home=home)


def test_create_refuses_symlinked_archive_source_root(monkeypatch, tmp_path):
    home = _home(tmp_path)
    external = tmp_path / "external-archive"
    (home / "archive").rename(external)
    try:
        (home / "archive").symlink_to(external, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"directory symlinks unavailable: {exc}")
    _prepare(monkeypatch, home)

    with pytest.raises(backup.BackupError, match="source root"):
        backup.create_backup(output=tmp_path / "out", home=home)


def test_child_swap_to_symlink_is_not_dereferenced_and_fails_verification(
        monkeypatch, tmp_path):
    home = _home(tmp_path)
    external = tmp_path / "external-secret"
    external.write_bytes(b"must-not-enter-backup")
    child = home / "archive" / "objects" / "one"
    real_scan = backup._safe_source_tree
    scans = 0

    def swap_after_validation(root):
        nonlocal scans
        result = real_scan(root)
        scans += 1
        if scans == 2:
            child.unlink()
            try:
                child.symlink_to(external)
            except OSError as exc:
                pytest.skip(f"file symlinks unavailable: {exc}")
        return result

    monkeypatch.setattr(backup, "_safe_source_tree", swap_after_validation)
    tar_path = tmp_path / "memory.tar.gz"

    backup._create_memory_tar(home, tar_path)

    with tarfile.open(tar_path, "r:gz") as archive:
        member = archive.getmember("archive/objects/one")
        assert member.issym()
        assert member.size == 0
    with pytest.raises(backup.BackupError, match="unsafe tar member type"):
        backup._validate_tar(tar_path)


def test_posix_chmod_failure_aborts_create(monkeypatch, tmp_path):
    home = _home(tmp_path)
    _prepare(monkeypatch, home)
    monkeypatch.setattr(backup, "POSIX_MODE_ENFORCED", True, raising=False)
    monkeypatch.setattr(
        backup.os, "chmod",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            PermissionError("chmod denied")))

    with pytest.raises(backup.BackupError, match="permissions"):
        backup.create_backup(output=tmp_path / "out", home=home)

    assert not list((tmp_path / "out").glob(".memoryd-backup-*"))


def test_posix_chmod_failure_aborts_restore_and_cleans_staging(
        monkeypatch, tmp_path):
    home = _home(tmp_path)
    _prepare(monkeypatch, home)
    snapshot = backup.create_backup(output=tmp_path / "out", home=home)
    os.chmod(snapshot, 0o700)
    for path in snapshot.iterdir():
        os.chmod(path, 0o600)
    monkeypatch.setattr(backup, "_target_db_has_tables", lambda _dsn: False)
    monkeypatch.setattr(backup, "POSIX_MODE_ENFORCED", True, raising=False)
    monkeypatch.setattr(backup, "_require_mode", lambda _path, _mode: None)
    monkeypatch.setattr(
        backup.os, "chmod",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            PermissionError("chmod denied")))
    target = tmp_path / "target"

    with pytest.raises(backup.BackupError, match="permissions"):
        backup.restore_backup(snapshot, target_dsn="postgresql:///empty",
                              target_home=target)

    assert not target.exists()
    assert not list(tmp_path.glob(".target.restore-*"))


@pytest.mark.parametrize("name", ["database.dump", "memory.tar.gz"])
def test_verify_detects_corruption(monkeypatch, tmp_path, name):
    home = _home(tmp_path)
    _prepare(monkeypatch, home)
    snapshot = backup.create_backup(output=tmp_path / "out", home=home)
    with (snapshot / name).open("ab") as handle:
        handle.write(b"corrupt")

    result = backup.verify_snapshot(snapshot)

    assert not result.ok
    assert name in result.reason


def test_verify_rejects_incomplete_manifest(monkeypatch, tmp_path):
    home = _home(tmp_path)
    _prepare(monkeypatch, home)
    snapshot = backup.create_backup(output=tmp_path / "out", home=home)
    manifest = _manifest(snapshot)
    del manifest["created_at"]
    (snapshot / "manifest.json").write_text(json.dumps(manifest))

    result = backup.verify_snapshot(snapshot)

    assert not result.ok
    assert "manifest" in result.reason.lower()


@pytest.mark.parametrize("schema,migrations", [
    (True, ["001_init.sql"]),
    (1, []),
    (1, ["001_init.sql", "001_init.sql"]),
    (1, ["002_second.sql", "001_init.sql"]),
])
def test_verify_rejects_non_strict_schema_and_migrations(
        monkeypatch, tmp_path, schema, migrations):
    home = _home(tmp_path)
    _prepare(monkeypatch, home)
    snapshot = backup.create_backup(output=tmp_path / "out", home=home)
    manifest = _manifest(snapshot)
    manifest["schema_version"] = schema
    manifest["db_migrations"] = migrations
    (snapshot / "manifest.json").write_text(json.dumps(manifest))

    result = backup.verify_snapshot(snapshot)

    assert not result.ok


def test_verify_rejects_wrong_snapshot_or_file_mode(monkeypatch, tmp_path):
    home = _home(tmp_path)
    _prepare(monkeypatch, home)
    snapshot = backup.create_backup(output=tmp_path / "out", home=home)
    os.chmod(snapshot, 0o755)
    os.chmod(snapshot / "manifest.json", 0o644)
    monkeypatch.setattr(backup, "POSIX_MODE_ENFORCED", True, raising=False)

    result = backup.verify_snapshot(snapshot)

    assert not result.ok
    assert "mode" in result.reason.lower()


def test_verify_checks_mode_of_every_snapshot_file(monkeypatch, tmp_path):
    home = _home(tmp_path)
    _prepare(monkeypatch, home)
    snapshot = backup.create_backup(output=tmp_path / "out", home=home)
    checked: list[tuple[Path, int]] = []

    def reject_manifest(path, expected):
        checked.append((path, expected))
        if path.name == "manifest.json":
            raise backup.BackupError("unsafe mode for manifest.json")

    monkeypatch.setattr(backup, "_require_mode", reject_manifest)

    result = backup.verify_snapshot(snapshot)

    assert not result.ok
    assert (snapshot / "manifest.json", 0o600) in checked


@pytest.mark.parametrize("kind", ["traversal", "symlink"])
def test_verify_rejects_unsafe_tar_members(monkeypatch, tmp_path, kind):
    home = _home(tmp_path)
    _prepare(monkeypatch, home)
    snapshot = backup.create_backup(output=tmp_path / "out", home=home)
    tar_path = snapshot / "memory.tar.gz"
    with tarfile.open(tar_path, "w:gz") as archive:
        if kind == "traversal":
            member = tarfile.TarInfo("../escape")
            member.size = 1
            archive.addfile(member, io.BytesIO(b"x"))
        else:
            member = tarfile.TarInfo("archive/link")
            member.type = tarfile.SYMTYPE
            member.linkname = "/etc/passwd"
            archive.addfile(member)
    _refresh_file_entry(snapshot, "memory.tar.gz")

    result = backup.verify_snapshot(snapshot)

    assert not result.ok
    assert "tar" in result.reason.lower()


def test_verify_requires_archive_and_spool_tar_roots(monkeypatch, tmp_path):
    home = _home(tmp_path)
    _prepare(monkeypatch, home)
    snapshot = backup.create_backup(output=tmp_path / "out", home=home)
    tar_path = snapshot / "memory.tar.gz"
    with tarfile.open(tar_path, "w:gz") as archive:
        member = tarfile.TarInfo("archive")
        member.type = tarfile.DIRTYPE
        archive.addfile(member)
    _refresh_file_entry(snapshot, "memory.tar.gz")

    result = backup.verify_snapshot(snapshot)

    assert not result.ok
    assert "spool" in result.reason.lower()


def test_verify_reports_empty_normalized_tar_path_as_corrupt(
        monkeypatch, tmp_path):
    home = _home(tmp_path)
    _prepare(monkeypatch, home)
    snapshot = backup.create_backup(output=tmp_path / "out", home=home)
    tar_path = snapshot / "memory.tar.gz"
    with tarfile.open(tar_path, "w:gz") as archive:
        archive.addfile(tarfile.TarInfo("."))
    _refresh_file_entry(snapshot, "memory.tar.gz")

    result = backup.verify_snapshot(snapshot)

    assert not result.ok
    assert "tar" in result.reason.lower()


def test_verify_and_restore_reject_normalized_tar_aliases(monkeypatch, tmp_path):
    home = _home(tmp_path)
    _prepare(monkeypatch, home)
    snapshot = backup.create_backup(output=tmp_path / "out", home=home)
    tar_path = snapshot / "memory.tar.gz"
    _write_tar(tar_path, [
        ("archive", None), ("spool", None),
        ("archive/x", b"first"), ("archive/./x", b"second"),
    ])
    _refresh_file_entry(snapshot, "memory.tar.gz")

    result = backup.verify_snapshot(snapshot)
    assert not result.ok
    assert "duplicate" in result.reason.lower()
    with pytest.raises(backup.BackupError, match="verification failed"):
        backup.restore_backup(
            snapshot, target_dsn="postgresql:///empty",
            target_home=tmp_path / "restored")


def test_extract_validates_and_extracts_same_open_tar_handle(
        monkeypatch, tmp_path):
    safe = tmp_path / "safe.tar.gz"
    hostile = tmp_path / "hostile.tar.gz"
    _write_tar(safe, [
        ("archive", None), ("spool", None), ("archive/good", b"safe")])
    _write_tar(hostile, [("../escaped", b"hostile")])
    real_open = tarfile.open
    opened: list[Path] = []

    def switched_open(_path, mode="r", **kwargs):
        selected = safe if not opened else hostile
        opened.append(selected)
        return real_open(selected, mode, **kwargs)

    monkeypatch.setattr(backup.tarfile, "open", switched_open)
    destination = tmp_path / "stage"
    destination.mkdir()

    backup._extract_memory_tar(tmp_path / "swapped.tar.gz", destination)

    assert opened == [safe]
    assert (destination / "archive" / "good").read_bytes() == b"safe"
    assert not (tmp_path / "escaped").exists()


def test_extract_privatises_implicit_intermediate_directories(
        monkeypatch, tmp_path):
    source = tmp_path / "source.tar.gz"
    _write_tar(source, [
        ("archive", None), ("spool", None),
        ("archive/implicit/child/data", b"safe"),
    ])
    destination = tmp_path / "stage"
    destination.mkdir()
    modes: dict[Path, int] = {}
    monkeypatch.setattr(
        backup, "_chmod", lambda path, mode: modes.__setitem__(path, mode))

    backup._extract_memory_tar(source, destination)

    assert modes[destination / "archive" / "implicit"] == 0o700
    assert modes[destination / "archive" / "implicit" / "child"] == 0o700


def test_list_reports_valid_and_corrupt_without_mutating(monkeypatch, tmp_path):
    home = _home(tmp_path)
    _prepare(monkeypatch, home)
    output = tmp_path / "out"
    good = backup.create_backup(output=output, home=home)
    bad = output / "20260712T012345Z-v1"
    bad.mkdir()
    (bad / "manifest.json").write_text("not json")
    before = sorted(path.name for path in output.iterdir())

    rows = backup.list_backups(output)

    assert [(row.path, row.ok) for row in rows] == [(bad, False), (good, True)]
    assert sorted(path.name for path in output.iterdir()) == before


def test_retention_removes_only_old_valid_generated_directories(
        monkeypatch, tmp_path):
    home = _home(tmp_path)
    _prepare(monkeypatch, home)
    output = tmp_path / "out"
    times = iter([
        datetime(2026, 7, 10, 1, tzinfo=timezone.utc),
        datetime(2026, 7, 11, 1, tzinfo=timezone.utc),
        datetime(2026, 7, 12, 1, tzinfo=timezone.utc),
    ])
    monkeypatch.setattr(backup, "_utc_now", lambda: next(times))
    first = backup.create_backup(output=output, home=home, retain=14)
    second = backup.create_backup(output=output, home=home, retain=14)
    junk = output / "notes"
    junk.mkdir()
    corrupt = output / "20200101T000000Z-v1"
    corrupt.mkdir()
    (corrupt / "manifest.json").write_text("{}")
    link = output / "19990101T000000Z-v1"
    try:
        link.symlink_to(first, target_is_directory=True)
    except OSError:
        link = None

    third = backup.create_backup(output=output, home=home, retain=2)

    assert not first.exists()
    assert second.exists() and third.exists()
    assert junk.exists() and corrupt.exists()
    if link is not None:
        assert link.is_symlink()


def test_retention_reuses_new_result_but_verifies_every_old_candidate(
        monkeypatch, tmp_path):
    home = _home(tmp_path)
    _prepare(monkeypatch, home)
    output = tmp_path / "out"
    times = iter([
        datetime(2026, 7, day, 1, tzinfo=timezone.utc)
        for day in (10, 11, 12)
    ])
    monkeypatch.setattr(backup, "_utc_now", lambda: next(times))
    snapshots = [
        backup.create_backup(output=output, home=home, retain=14)
        for _ in range(3)
    ]
    real_verify = backup.verify_snapshot
    checked: list[Path] = []

    def counted(path, **kwargs):
        checked.append(Path(path))
        return real_verify(path, **kwargs)

    monkeypatch.setattr(backup, "verify_snapshot", counted)

    backup._apply_retention(
        output, 2, verified={snapshots[-1]: backup.Verification(True)})

    assert not snapshots[0].exists()
    assert snapshots[1].exists() and snapshots[2].exists()
    assert checked == snapshots[:-1]


def test_create_does_not_fully_verify_new_snapshot_twice(monkeypatch, tmp_path):
    home = _home(tmp_path)
    _prepare(monkeypatch, home)
    real_verify = backup.verify_snapshot
    calls: list[Path] = []

    def counted(snapshot, **kwargs):
        calls.append(Path(snapshot))
        return real_verify(snapshot, **kwargs)

    monkeypatch.setattr(backup, "verify_snapshot", counted)

    backup.create_backup(output=tmp_path / "out", home=home)

    assert len(calls) == 1


def test_retention_preserves_snapshot_with_inconsistent_size_metadata(
        monkeypatch, tmp_path):
    home = _home(tmp_path)
    _prepare(monkeypatch, home)
    output = tmp_path / "out"
    times = iter([
        datetime(2026, 7, day, 1, tzinfo=timezone.utc)
        for day in (10, 11, 12)
    ])
    monkeypatch.setattr(backup, "_utc_now", lambda: next(times))
    snapshots = [
        backup.create_backup(output=output, home=home, retain=14)
        for _ in range(3)
    ]
    manifest = _manifest(snapshots[0])
    manifest["files"]["database.dump"]["bytes"] += 1
    (snapshots[0] / "manifest.json").write_text(json.dumps(manifest))

    backup._apply_retention(output, 2)

    assert snapshots[0].exists()


def test_retention_preserves_same_size_checksum_corruption(
        monkeypatch, tmp_path):
    home = _home(tmp_path)
    _prepare(monkeypatch, home)
    output = tmp_path / "out"
    times = iter([
        datetime(2026, 7, day, 1, tzinfo=timezone.utc)
        for day in (10, 11, 12)
    ])
    monkeypatch.setattr(backup, "_utc_now", lambda: next(times))
    snapshots = [
        backup.create_backup(output=output, home=home, retain=14)
        for _ in range(3)
    ]
    dump = snapshots[0] / "database.dump"
    original = dump.read_bytes()
    dump.write_bytes(bytes([original[0] ^ 0xFF]) + original[1:])
    assert dump.stat().st_size == len(original)

    backup._apply_retention(output, 2)

    assert snapshots[0].exists()


def test_restore_verifies_then_publishes_home_with_target_config(
        monkeypatch, tmp_path):
    source_home = _home(tmp_path)
    _prepare(monkeypatch, source_home)
    snapshot = backup.create_backup(output=tmp_path / "out", home=source_home)
    target_home = tmp_path / "restored"
    restored: list[tuple[Path, str]] = []
    monkeypatch.setattr(backup, "_target_db_has_tables", lambda _dsn: False)
    monkeypatch.setattr(
        backup, "_restore_database",
        lambda dump, dsn: restored.append((dump, dsn)))
    dsn = "postgresql://restore:target-secret@localhost/empty"

    backup.restore_backup(snapshot, target_dsn=dsn, target_home=target_home)

    assert (target_home / "archive" / "objects" / "one").read_bytes() == b"archive"
    assert (target_home / "spool" / "incoming" / "job.json").exists()
    config = json.loads((target_home / "config.json").read_text())
    assert config["dsn"] == dsn
    assert config["home"] == str(target_home)
    assert config["env"] == {"MEMORYD_LLM_MODEL": "claude-test"}
    assert restored and restored[0][1] == dsn


def test_restore_checks_staged_home_and_config_modes(monkeypatch, tmp_path):
    source_home = _home(tmp_path)
    _prepare(monkeypatch, source_home)
    snapshot = backup.create_backup(output=tmp_path / "out", home=source_home)
    monkeypatch.setattr(backup, "_target_db_has_tables", lambda _dsn: False)
    monkeypatch.setattr(backup, "_restore_database", lambda _dump, _dsn: None)
    checked: list[tuple[Path, int]] = []
    monkeypatch.setattr(
        backup, "_require_mode",
        lambda path, expected: checked.append((path, expected)))
    target = tmp_path / "restored"

    backup.restore_backup(
        snapshot, target_dsn="postgresql:///empty", target_home=target)

    assert any(path.name.startswith(".restored.restore-") and mode == 0o700
               for path, mode in checked)
    assert any(path.name == "config.json" and mode == 0o600
               for path, mode in checked)


def test_restore_refuses_nonempty_home_and_nonempty_db_but_not_health(
        monkeypatch, tmp_path):
    source_home = _home(tmp_path)
    _prepare(monkeypatch, source_home)
    snapshot = backup.create_backup(output=tmp_path / "out", home=source_home)
    target = tmp_path / "target"
    target.mkdir()
    (target / "keep").write_text("data")
    monkeypatch.setattr(backup, "_target_db_has_tables", lambda _dsn: False)

    with pytest.raises(backup.BackupError, match="not empty"):
        backup.restore_backup(snapshot, target_dsn="postgresql:///db",
                              target_home=target)
    assert (target / "keep").read_text() == "data"

    target = tmp_path / "other"
    monkeypatch.setattr(backup, "_target_db_has_tables", lambda _dsn: True)
    with pytest.raises(backup.BackupError, match="user tables"):
        backup.restore_backup(snapshot, target_dsn="postgresql:///db",
                              target_home=target)
    assert not target.exists()

    monkeypatch.setattr(
        cli, "_health",
        lambda: pytest.fail("HTTP health must not decide offline ownership"))
    monkeypatch.setattr(backup, "_target_db_has_tables", lambda _dsn: False)
    monkeypatch.setattr(backup, "_restore_database", lambda _dump, _dsn: None)
    third = backup.restore_backup(
        snapshot, target_dsn="postgresql:///db2",
        target_home=tmp_path / "third")
    assert third.is_dir()


def test_restore_arbitrary_home_is_blocked_by_same_database_owner(
        monkeypatch, tmp_path):
    from memoryd import ownership

    source_home = _home(tmp_path)
    _prepare(monkeypatch, source_home)
    snapshot = backup.create_backup(output=tmp_path / "out", home=source_home)
    target_home = tmp_path / "different-home"
    dsn = "postgresql:///shared-target"
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

        def execute(self, sql, _params):
            assert "pg_try_advisory_lock" in sql
            self.acquired = self.database not in held
            if self.acquired:
                held.add(self.database)
            return Cursor(self.acquired)

        def close(self):
            if self.acquired:
                held.remove(self.database)

    monkeypatch.setitem(
        sys.modules, "psycopg", types.SimpleNamespace(
            connect=lambda value, **_kwargs: Connection(value)))
    monkeypatch.setattr(backup, "offline_ownership", ownership.offline_ownership)

    with ownership.database_ownership(dsn, purpose="server"):
        with pytest.raises(backup.BackupError, match="database is in use"):
            backup.restore_backup(
                snapshot, target_dsn=dsn, target_home=target_home)

    assert not target_home.exists()
    with ownership.home_ownership(target_home, purpose="server"):
        pass


def test_restore_error_releases_home_and_database_ownership(
        monkeypatch, tmp_path):
    from memoryd import ownership

    source_home = _home(tmp_path)
    _prepare(monkeypatch, source_home)
    snapshot = backup.create_backup(output=tmp_path / "out", home=source_home)
    target_home = tmp_path / "target"
    dsn = "postgresql:///release-after-error"
    held: set[str] = set()

    class Cursor:
        def __init__(self, row):
            self.row = row

        def fetchone(self):
            return self.row

    class Connection:
        def __init__(self, database):
            self.database = database
            self.acquired = False

        def execute(self, sql, _params=None):
            if "pg_try_advisory_lock" in sql:
                self.acquired = self.database not in held
                if self.acquired:
                    held.add(self.database)
                return Cursor((self.acquired,))
            return Cursor((False,))

        def close(self):
            if self.acquired:
                held.remove(self.database)

    monkeypatch.setitem(
        sys.modules, "psycopg", types.SimpleNamespace(
            connect=lambda value, **_kwargs: Connection(value)))
    monkeypatch.setattr(backup, "offline_ownership", ownership.offline_ownership)
    monkeypatch.setattr(backup, "_target_db_has_tables", lambda _dsn: False)
    monkeypatch.setattr(
        backup, "_restore_database",
        lambda *_args: (_ for _ in ()).throw(backup.BackupError("injected")))

    with pytest.raises(backup.BackupError, match="injected"):
        backup.restore_backup(
            snapshot, target_dsn=dsn, target_home=target_home)

    assert held == set()
    with ownership.offline_ownership(
            target_home, dsn, purpose="server"):
        pass


def test_restore_command_failure_removes_only_staging_and_warns_db_risk(
        monkeypatch, tmp_path):
    source_home = _home(tmp_path)
    _prepare(monkeypatch, source_home)
    snapshot = backup.create_backup(output=tmp_path / "out", home=source_home)
    target = tmp_path / "target"
    monkeypatch.setattr(backup, "_target_db_has_tables", lambda _dsn: False)
    monkeypatch.setattr(
        backup, "_restore_database",
        lambda _dump, _dsn: (_ for _ in ()).throw(backup.BackupError("pg failed")))

    with pytest.raises(backup.BackupError, match="partial empty-target DB risk"):
        backup.restore_backup(snapshot, target_dsn="postgresql:///empty",
                              target_home=target)

    assert snapshot.exists()
    assert not target.exists()
    assert not list(tmp_path.glob(".target.restore-*"))


def test_posix_existing_empty_target_publishes_with_one_replace(
        monkeypatch, tmp_path):
    source_home = _home(tmp_path)
    _prepare(monkeypatch, source_home)
    snapshot = backup.create_backup(output=tmp_path / "out", home=source_home)
    target = tmp_path / "target"
    target.mkdir()
    monkeypatch.setattr(
        backup, "WINDOWS_RESTORE_REQUIRES_ABSENT_HOME", False, raising=False)
    monkeypatch.setattr(backup, "_target_db_has_tables", lambda _dsn: False)
    monkeypatch.setattr(backup, "_restore_database", lambda _dump, _dsn: None)
    real_replace = backup.os.replace
    calls: list[tuple[Path, Path]] = []

    def posix_replace(source, destination):
        source = Path(source)
        destination = Path(destination)
        calls.append((source, destination))
        if destination == target and target.is_dir():
            target.rmdir()  # simulate POSIX's atomic empty-directory replacement
        return real_replace(source, destination)

    monkeypatch.setattr(backup.os, "replace", posix_replace)

    backup.restore_backup(snapshot, target_dsn="postgresql:///empty",
                          target_home=target)

    assert len(calls) == 1
    assert calls[0][1] == target
    assert (target / "config.json").is_file()


def test_windows_existing_empty_target_refuses_before_database_access(
        monkeypatch, tmp_path):
    source_home = _home(tmp_path)
    _prepare(monkeypatch, source_home)
    snapshot = backup.create_backup(output=tmp_path / "out", home=source_home)
    target = tmp_path / "target"
    target.mkdir()
    database_calls: list[str] = []
    monkeypatch.setattr(
        backup, "WINDOWS_RESTORE_REQUIRES_ABSENT_HOME", True, raising=False)
    monkeypatch.setattr(
        backup, "_target_db_has_tables",
        lambda dsn: database_calls.append(dsn) or False)
    monkeypatch.setattr(
        backup, "_restore_database",
        lambda _dump, _dsn: database_calls.append("restore"))

    with pytest.raises(backup.BackupError, match="Windows.*absent"):
        backup.restore_backup(snapshot, target_dsn="postgresql:///empty",
                              target_home=target)

    assert database_calls == []
    assert target.is_dir() and not list(target.iterdir())


def test_windows_absent_target_publishes_atomically(monkeypatch, tmp_path):
    source_home = _home(tmp_path)
    _prepare(monkeypatch, source_home)
    snapshot = backup.create_backup(output=tmp_path / "out", home=source_home)
    target = tmp_path / "target"
    monkeypatch.setattr(
        backup, "WINDOWS_RESTORE_REQUIRES_ABSENT_HOME", True, raising=False)
    monkeypatch.setattr(backup, "_target_db_has_tables", lambda _dsn: False)
    monkeypatch.setattr(backup, "_restore_database", lambda _dump, _dsn: None)

    backup.restore_backup(snapshot, target_dsn="postgresql:///empty",
                          target_home=target)

    assert (target / "config.json").is_file()


def test_posix_replace_failure_preserves_existing_empty_target(
        monkeypatch, tmp_path):
    source_home = _home(tmp_path)
    _prepare(monkeypatch, source_home)
    snapshot = backup.create_backup(output=tmp_path / "out", home=source_home)
    target = tmp_path / "target"
    target.mkdir()
    monkeypatch.setattr(
        backup, "WINDOWS_RESTORE_REQUIRES_ABSENT_HOME", False, raising=False)
    monkeypatch.setattr(backup, "_target_db_has_tables", lambda _dsn: False)
    monkeypatch.setattr(backup, "_restore_database", lambda _dump, _dsn: None)
    calls: list[tuple[Path, Path]] = []

    def fail_replace(source, destination):
        calls.append((Path(source), Path(destination)))
        raise OSError("injected publish failure")

    monkeypatch.setattr(backup.os, "replace", fail_replace)

    with pytest.raises(backup.BackupError, match="publish failure"):
        backup.restore_backup(snapshot, target_dsn="postgresql:///empty",
                              target_home=target)

    assert len(calls) == 1
    assert target.is_dir()
    assert not list(target.iterdir())
    assert not list(tmp_path.glob(".target.restore-*"))


@pytest.mark.parametrize("dsn", [
    ("postgresql://operator:super-secret@localhost:5432/memoryd"
     "?sslpassword=tls-secret"),
    ("host=localhost port=5432 dbname=memoryd user=operator "
     "password=super-secret sslpassword=tls-secret"),
])
def test_local_pg_tools_keep_all_passwords_out_of_argv(
        monkeypatch, tmp_path, dsn):
    commands: list[tuple[list[str], dict[str, str]]] = []
    service_files: list[tuple[Path, str, int, int, Path, str, int]] = []
    dump = tmp_path / "dump"

    def run(command, **kwargs):
        env = kwargs["env"]
        commands.append((command, env))
        service_path = env.get("PGSERVICEFILE")
        if service_path is not None:
            path = Path(service_path)
            pass_file = path.parent / "pgpass.conf"
            service_files.append((
                path, path.read_text(encoding="utf-8"),
                stat.S_IMODE(path.stat().st_mode),
                stat.S_IMODE(path.parent.stat().st_mode),
                pass_file, pass_file.read_text(encoding="utf-8"),
                stat.S_IMODE(pass_file.stat().st_mode)))
        if "pg_dump" in command[0]:
            dump.write_bytes(b"PGDMPbinary\x00\xff")
        return types.SimpleNamespace(returncode=0, stderr=b"")

    monkeypatch.setattr(backup.shutil, "which", lambda name: f"/tools/{name}")
    monkeypatch.setattr(backup.subprocess, "run", run)
    monkeypatch.setenv("PGPASSWORD", "inherited-password")
    monkeypatch.setenv("PGSSLPASSWORD", "inherited-ssl-password")
    backup._dump_database(dsn, dump)
    backup._restore_database(dump, dsn)

    assert dump.read_bytes() == b"PGDMPbinary\x00\xff"
    assert all("super-secret" not in " ".join(command)
               for command, _env in commands)
    assert all("tls-secret" not in " ".join(command)
               for command, _env in commands)
    assert all(command[command.index("--dbname") + 1] == "service=memoryd"
               for command, _env in commands)
    assert all("PGPASSWORD" not in env and "PGSSLPASSWORD" not in env
               for _command, env in commands)
    assert all("super-secret" not in env.values() and
               "tls-secret" not in env.values()
               for _command, env in commands)
    assert len(service_files) == 2
    for path, text, mode, parent_mode, pass_file, pass_text, pass_mode in service_files:
        assert "[memoryd]" in text
        assert "password=super-secret" not in text
        assert f"passfile={pass_file}" in text
        assert "sslpassword=tls-secret" in text
        assert pass_text == "*:*:*:*:super-secret\n"
        if os.name != "nt":
            assert mode == 0o600
            assert pass_mode == 0o600
            assert parent_mode == 0o700
        assert not path.exists()
        assert not pass_file.exists()
        assert not path.parent.exists()
    assert "--format=custom" in commands[0][0]
    assert "--exit-on-error" in commands[1][0]
    assert "--no-owner" in commands[1][0]
    assert "--single-transaction" in commands[1][0]
    assert "--no-privileges" in commands[1][0]


def test_direct_password_boundary_whitespace_round_trips_through_libpq(
        monkeypatch, tmp_path):
    from psycopg.conninfo import make_conninfo

    monkeypatch.setattr(
        backup, "_default_home", lambda: tmp_path / "memory")
    password = " \t data:base\\secret \t"
    values, _redactions = backup._safe_conninfo(make_conninfo(
        host="ignored.invalid", port="1", dbname="memoryd",
        user="operator", password=password))

    with backup._libpq_service(values) as (safe_dsn, env, _redactions):
        captured = _capture_libpq_password(safe_dsn, env, monkeypatch)

    assert captured == password


def test_inherited_service_is_resolved_once_and_inline_values_win(
        monkeypatch, tmp_path):
    upstream = tmp_path / "upstream.conf"
    upstream.write_text(
        "[upstream]\n"
        "host=upstream.invalid\n"
        "port=6543\n"
        "dbname=upstream_db\n"
        "user=upstream_user\n"
        "password=upstream-password\n"
        "sslpassword=upstream-tls\n",
        encoding="utf-8")
    monkeypatch.setenv("PGSERVICEFILE", str(upstream))
    monkeypatch.setattr(
        backup, "_default_home", lambda: tmp_path / "memory")
    values, _redactions = backup._safe_conninfo(
        "service=upstream host=127.0.0.1 port=1 "
        "password=' inline-password ' sslpassword=inline-tls")

    with backup._libpq_service(values) as (safe_dsn, env, discovered):
        resolved = _libpq_info(safe_dsn, env, monkeypatch)

    assert resolved["service"] == "memoryd"
    assert resolved["host"] == "127.0.0.1"
    assert resolved["port"] == "1"
    assert resolved["dbname"] == "upstream_db"
    assert resolved["user"] == "upstream_user"
    assert resolved["sslpassword"] == "inline-tls"
    assert "upstream" not in resolved.values()
    assert set(discovered) == {" inline-password ", "inline-tls"}


@pytest.mark.parametrize("lookup", ["user", "system"])
def test_service_lookup_uses_libpq_user_and_system_locations(
        monkeypatch, tmp_path, lookup):
    monkeypatch.delenv("PGSERVICEFILE", raising=False)
    user_root = tmp_path / "user"
    if os.name == "nt":
        monkeypatch.setenv("APPDATA", str(user_root))
        user_file = user_root / "postgresql" / ".pg_service.conf"
    else:
        monkeypatch.setenv("HOME", str(user_root))
        user_file = user_root / ".pg_service.conf"
    system_root = tmp_path / "system"
    monkeypatch.setenv("PGSYSCONFDIR", str(system_root))
    service_file = (
        user_file if lookup == "user" else system_root / "pg_service.conf")
    service_file.parent.mkdir(parents=True)
    service_file.write_text(
        "[looked-up]\n"
        "host=127.0.0.1\n"
        "port=1\n"
        f"user={lookup}-user\n"
        "dbname=memoryd\n",
        encoding="utf-8")
    monkeypatch.setattr(
        backup, "_default_home", lambda: tmp_path / "memory")

    with backup._libpq_service(
            {"service": "looked-up"}) as (safe_dsn, env, _redactions):
        resolved = _libpq_info(safe_dsn, env, monkeypatch)

    assert resolved["user"] == f"{lookup}-user"
    assert resolved["dbname"] == "memoryd"


def test_missing_selected_service_falls_through_to_compiled_system_file(
        monkeypatch, tmp_path):
    selected = tmp_path / "selected.conf"
    selected.write_text(
        "[different-service]\nhost=ignored.invalid\n", encoding="utf-8")
    system_root = tmp_path / "compiled-system"
    system_root.mkdir()
    (system_root / "pg_service.conf").write_text(
        "[system-service]\n"
        "host=127.0.0.1\n"
        "port=1\n"
        "user=system-user\n"
        "dbname=memoryd\n",
        encoding="utf-8")
    monkeypatch.setenv("PGSERVICEFILE", str(selected))
    monkeypatch.delenv("PGSYSCONFDIR", raising=False)
    monkeypatch.setenv("PGPASSWORD", "must-not-reach-pg-config")
    monkeypatch.setattr(
        backup, "_default_home", lambda: tmp_path / "memory")
    monkeypatch.setattr(
        backup.shutil, "which",
        lambda name: "/tools/pg_config" if name == "pg_config" else None)
    calls: list[tuple[list[str], dict]] = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        return types.SimpleNamespace(
            returncode=0, stdout=str(system_root).encode(), stderr=b"")

    monkeypatch.setattr(backup.subprocess, "run", run)

    with backup._libpq_service(
            {"service": "system-service"}) as (safe_dsn, env, _redactions):
        resolved = _libpq_info(safe_dsn, env, monkeypatch)

    assert resolved["user"] == "system-user"
    assert calls[0][0] == ["/tools/pg_config", "--sysconfdir"]
    assert calls[0][1]["shell"] is False
    assert calls[0][1]["timeout"] == 5
    assert "PGPASSWORD" not in calls[0][1]["env"]


@pytest.mark.parametrize("dsn_format", ["keyword", "uri"])
def test_explicit_servicefile_is_honored_with_older_or_newer_libpq(
        monkeypatch, tmp_path, dsn_format):
    from urllib.parse import quote

    service_file = tmp_path / "explicit.conf"
    service_file.write_text(
        "[explicit]\n"
        "host=127.0.0.1\n"
        "port=1\n"
        "user=explicit-user\n"
        "dbname=memoryd\n",
        encoding="utf-8")
    escaped = str(service_file).replace("\\", "\\\\").replace("'", "\\'")
    dsn = (f"service=explicit servicefile='{escaped}'"
           if dsn_format == "keyword" else
           f"postgresql:///?service=explicit&servicefile={quote(str(service_file), safe='')}")
    values, _redactions = backup._safe_conninfo(dsn)
    monkeypatch.setattr(
        backup, "_default_home", lambda: tmp_path / "memory")

    with backup._libpq_service(values) as (safe_dsn, env, _redactions):
        resolved = _libpq_info(safe_dsn, env, monkeypatch)

    assert resolved["user"] == "explicit-user"
    assert resolved["dbname"] == "memoryd"


@pytest.mark.parametrize("name", ["sslpassword", "application_name"])
def test_unencodable_service_trailing_whitespace_is_rejected_without_secret(
        monkeypatch, tmp_path, name):
    monkeypatch.setattr(
        backup, "_default_home", lambda: tmp_path / "memory")
    secret = "boundary-value "

    with pytest.raises(backup.BackupError) as exc:
        with backup._libpq_service({
                "host": "127.0.0.1", name: secret}):
            pytest.fail("unsafe service value must not be yielded")

    assert "boundary whitespace" in str(exc.value)
    assert name in str(exc.value)
    assert secret not in str(exc.value)


def test_sslpassword_leading_whitespace_round_trips_through_libpq(
        monkeypatch, tmp_path):
    monkeypatch.setattr(
        backup, "_default_home", lambda: tmp_path / "memory")
    sslpassword = " \t tls-secret"

    with backup._libpq_service({
            "host": "127.0.0.1", "port": "1",
            "sslpassword": sslpassword}) as (safe_dsn, env, _redactions):
        resolved = _libpq_info(safe_dsn, env, monkeypatch)

    assert resolved["sslpassword"] == sslpassword


def test_restore_tool_failure_redacts_secrets_and_keeps_mock_database_empty(
        monkeypatch, tmp_path):
    database_objects: list[str] = []
    observed_service_files: list[Path] = []
    dump = tmp_path / "dump"
    dump.write_bytes(b"PGDMPmock")

    def run(command, **kwargs):
        service_path = kwargs["env"].get("PGSERVICEFILE")
        if service_path is not None:
            path = Path(service_path)
            text = path.read_text(encoding="utf-8")
            assert "password=super-secret" not in text
            assert (path.parent / "pgpass.conf").is_file()
            assert "sslpassword=tls-secret" in text
            observed_service_files.append(path)
        if "--single-transaction" not in command:
            database_objects.append("partially-restored-table")
        return types.SimpleNamespace(
            returncode=1,
            stderr=b"password=super-secret sslpassword=tls-secret failure")

    monkeypatch.setattr(backup.shutil, "which", lambda name: f"/tools/{name}")
    monkeypatch.setattr(backup.subprocess, "run", run)
    dsn = ("host=localhost dbname=memoryd user=operator "
           "password=super-secret sslpassword=tls-secret")

    with pytest.raises(backup.BackupError) as exc:
        backup._restore_database(dump, dsn)

    assert database_objects == []
    assert "super-secret" not in str(exc.value)
    assert "tls-secret" not in str(exc.value)
    assert len(observed_service_files) == 1
    assert not observed_service_files[0].exists()
    assert not observed_service_files[0].parent.exists()


def test_service_file_secrets_are_redacted_from_tool_diagnostics(
        monkeypatch, tmp_path):
    upstream = tmp_path / "upstream.conf"
    upstream.write_text(
        "[upstream]\n"
        "host=127.0.0.1\n"
        "port=1\n"
        "dbname=memoryd\n"
        "user=operator\n"
        "password=service-database-secret\n"
        "sslpassword=service-tls-secret\n",
        encoding="utf-8")
    monkeypatch.setenv("PGSERVICEFILE", str(upstream))
    monkeypatch.setattr(
        backup, "_default_home", lambda: tmp_path / "memory")
    monkeypatch.setattr(backup.shutil, "which", lambda _name: "/tools/pg_restore")
    monkeypatch.setattr(
        backup.subprocess, "run",
        lambda *_args, **_kwargs: types.SimpleNamespace(
            returncode=1,
            stderr=(b"password=service-database-secret "
                    b"sslpassword=service-tls-secret failure")))
    dump = tmp_path / "dump"
    dump.write_bytes(b"PGDMPservice")

    with pytest.raises(backup.BackupError) as exc:
        backup._restore_database(dump, "service=upstream")

    assert "service-database-secret" not in str(exc.value)
    assert "service-tls-secret" not in str(exc.value)
    assert "password=***" in str(exc.value)
    assert "sslpassword=***" in str(exc.value)


def test_service_cleanup_retries_transient_unlink_under_private_home_root(
        monkeypatch, tmp_path):
    home = tmp_path / "memory"
    dump = tmp_path / "dump"
    observed: list[Path] = []
    unlink_attempts = 0
    real_unlink = Path.unlink

    def run(command, **kwargs):
        path = Path(kwargs["env"]["PGSERVICEFILE"])
        observed.append(path)
        dump.write_bytes(b"PGDMPservice")
        return types.SimpleNamespace(returncode=0, stderr=b"")

    def flaky_unlink(path, *args, **kwargs):
        nonlocal unlink_attempts
        if path.name == "pg_service.conf" and path in observed:
            unlink_attempts += 1
            if unlink_attempts == 1:
                raise PermissionError("transient scanner handle")
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(backup, "_default_home", lambda: home)
    monkeypatch.setattr(backup, "SERVICE_CLEANUP_RETRY_S", 0.05,
                        raising=False)
    monkeypatch.setattr(backup.shutil, "which", lambda _name: "/tools/pg_dump")
    monkeypatch.setattr(backup.subprocess, "run", run)
    monkeypatch.setattr(Path, "unlink", flaky_unlink)

    backup._dump_database(
        "postgresql://operator:secret@localhost/memoryd", dump)

    assert unlink_attempts >= 2
    service_file = observed[0]
    assert service_file.parent.parent == home / ".pg-service"
    assert not service_file.exists()
    assert not service_file.parent.exists()
    assert service_file.parent.parent.is_dir()


def test_persistent_service_residue_is_cleaned_by_next_operation(
        monkeypatch, tmp_path):
    home = tmp_path / "memory"
    dump = tmp_path / "dump"
    observed: list[Path] = []
    blocked: set[Path] = set()
    real_unlink = Path.unlink

    def run(command, **kwargs):
        path = Path(kwargs["env"]["PGSERVICEFILE"])
        observed.append(path)
        dump.write_bytes(b"PGDMPservice")
        return types.SimpleNamespace(returncode=0, stderr=b"")

    def persistent_unlink(path, *args, **kwargs):
        if path in blocked:
            raise PermissionError("persistent scanner handle")
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(backup, "_default_home", lambda: home)
    monkeypatch.setattr(backup, "SERVICE_CLEANUP_RETRY_S", 0.01,
                        raising=False)
    monkeypatch.setattr(backup.shutil, "which", lambda _name: "/tools/pg_dump")
    monkeypatch.setattr(backup.subprocess, "run", run)
    monkeypatch.setattr(Path, "unlink", persistent_unlink)

    def block_first(command, **kwargs):
        result = run(command, **kwargs)
        blocked.add(observed[-1])
        return result

    monkeypatch.setattr(backup.subprocess, "run", block_first)
    with pytest.raises(backup.BackupError, match="clean.*service"):
        backup._dump_database(
            "postgresql://operator:secret@localhost/memoryd", dump)
    stale = observed[0]
    assert stale.is_file()

    blocked.clear()
    monkeypatch.setattr(backup.subprocess, "run", run)
    backup._dump_database(
        "postgresql://operator:secret@localhost/memoryd", dump)

    assert not stale.exists()
    assert not stale.parent.exists()
    assert len(observed) == 2


def test_service_cleanup_failure_preserves_original_tool_error_as_primary(
        monkeypatch, tmp_path):
    home = tmp_path / "memory"
    dump = tmp_path / "dump"
    blocked: set[Path] = set()
    real_unlink = Path.unlink

    def run(command, **kwargs):
        blocked.add(Path(kwargs["env"]["PGSERVICEFILE"]))
        return types.SimpleNamespace(
            returncode=1, stderr=b"original database failure")

    def persistent_unlink(path, *args, **kwargs):
        if path in blocked:
            raise PermissionError("persistent scanner handle")
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(backup, "_default_home", lambda: home)
    monkeypatch.setattr(backup, "SERVICE_CLEANUP_RETRY_S", 0.01,
                        raising=False)
    monkeypatch.setattr(backup.shutil, "which", lambda _name: "/tools/pg_restore")
    monkeypatch.setattr(backup.subprocess, "run", run)
    monkeypatch.setattr(Path, "unlink", persistent_unlink)
    dump.write_bytes(b"PGDMPservice")

    with pytest.raises(
            backup.BackupError, match="original database failure") as exc:
        backup._restore_database(
            dump, "postgresql://operator:secret@localhost/memoryd")

    notes = getattr(exc.value, "__notes__", [])
    assert any("service cleanup failed" in note for note in notes)


def test_service_root_rejects_unrecognized_stale_artifact(
        monkeypatch, tmp_path):
    root = tmp_path / "memory" / ".pg-service"
    root.mkdir(parents=True)
    if os.name != "nt":
        root.chmod(0o700)
    unexpected = root / "not-an-operation"
    unexpected.write_text("do not delete", encoding="utf-8")
    monkeypatch.setattr(backup, "_default_home", lambda: tmp_path / "memory")
    monkeypatch.setattr(
        backup.subprocess, "run",
        lambda *_args, **_kwargs: pytest.fail("tool must not run"))
    monkeypatch.setattr(backup.shutil, "which", lambda _name: "/tools/pg_dump")

    with pytest.raises(backup.BackupError, match="unrecognized service artifact"):
        backup._dump_database(
            "postgresql://operator:secret@localhost/memoryd",
            tmp_path / "dump")

    assert unexpected.is_file()


def test_service_lock_serializes_active_operations_across_threads(
        monkeypatch, tmp_path):
    home = tmp_path / "memory"
    first_entered = threading.Event()
    release_first = threading.Event()
    second_entered = threading.Event()
    observed: list[Path] = []
    first_survived: list[bool] = []
    observation_lock = threading.Lock()

    def run(command, **kwargs):
        service_file = Path(kwargs["env"]["PGSERVICEFILE"])
        with observation_lock:
            index = len(observed)
            observed.append(service_file)
        if index == 0:
            first_entered.set()
            release_first.wait(timeout=5)
            first_survived.append(service_file.is_file())
        else:
            second_entered.set()
        Path(command[command.index("--file") + 1]).write_bytes(
            b"PGDMPserialized")
        return types.SimpleNamespace(returncode=0, stderr=b"")

    monkeypatch.setattr(backup, "_default_home", lambda: home)
    monkeypatch.setattr(backup.shutil, "which", lambda _name: "/tools/pg_dump")
    monkeypatch.setattr(backup.subprocess, "run", run)
    dsn = "postgresql://operator:secret@localhost/memoryd"

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(backup._dump_database, dsn, tmp_path / "one.dump")
        assert first_entered.wait(timeout=5)
        second = pool.submit(backup._dump_database, dsn, tmp_path / "two.dump")
        second_was_blocked = not second_entered.wait(timeout=0.2)
        release_first.set()
        first_error = first.exception(timeout=5)
        second_error = second.exception(timeout=5)

    assert second_was_blocked
    assert first_survived == [True]
    assert first_error is None and second_error is None
    assert len(set(observed)) == 2
    lock_file = home / ".pg-service" / "state.lock"
    assert lock_file.is_file()
    if os.name != "nt":
        assert stat.S_IMODE(lock_file.stat().st_mode) == 0o600


def test_crash_releases_service_lock_and_next_operation_cleans_residue(
        monkeypatch, tmp_path):
    home = tmp_path / "memory"
    child_env = os.environ.copy()
    child_env["MEMORYD_HOME"] = str(home)
    script = (
        "import os\n"
        "from memoryd.backup import _libpq_service\n"
        "with _libpq_service({'host': 'localhost', 'dbname': 'memoryd', "
        "'user': 'operator', 'password': 'secret'}) as (_, env, _):\n"
        "    print(env['PGSERVICEFILE'], flush=True)\n"
        "    os._exit(0)\n")
    child = backup.subprocess.run(
        [backup.sys.executable, "-c", script], capture_output=True,
        text=True, env=child_env, cwd=Path(__file__).resolve().parents[1],
        timeout=10)
    assert child.returncode == 0, child.stderr
    stale = Path(child.stdout.strip())
    assert stale.is_file()
    lock_file = home / ".pg-service" / "state.lock"
    lock_was_durable = lock_file.is_file()

    dump = tmp_path / "dump"

    def run(command, **_kwargs):
        dump.write_bytes(b"PGDMPafter-crash")
        return types.SimpleNamespace(returncode=0, stderr=b"")

    monkeypatch.setattr(backup, "_default_home", lambda: home)
    monkeypatch.setattr(backup.shutil, "which", lambda _name: "/tools/pg_dump")
    monkeypatch.setattr(backup.subprocess, "run", run)
    backup._dump_database(
        "postgresql://operator:secret@localhost/memoryd", dump)

    assert lock_was_durable
    assert not stale.exists()
    assert not stale.parent.exists()
    assert lock_file.is_file()


def test_invalid_conninfo_diagnostic_does_not_echo_password_fields():
    dsn = ("host=localhost password=super-secret sslpassword=tls-secret "
           "unterminated='value")

    with pytest.raises(backup.BackupError) as exc:
        backup._safe_conninfo(dsn)

    assert "super-secret" not in str(exc.value)
    assert "tls-secret" not in str(exc.value)


def test_docker_dump_fallback_refuses_mismatched_localhost_port(
        monkeypatch, tmp_path):
    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(backup.shutil, "which", lambda _name: None)
    monkeypatch.setattr(cli, "_container_port", lambda: "5440")
    monkeypatch.setattr(
        cli, "_docker",
        lambda *args: (calls.append(args) or (0, "exists")))

    with pytest.raises(backup.BackupError, match="pg_dump is unavailable"):
        backup._dump_database(
            "postgresql://postgres:secret@127.0.0.1:5432/memoryd",
            tmp_path / "dump")

    assert not any(call[0] in {"exec", "cp"} for call in calls)


def test_concurrent_docker_dumps_use_and_clean_distinct_remote_paths(
        monkeypatch, tmp_path):
    started = threading.Barrier(2)
    dump_paths: list[str] = []
    copied_paths: list[str] = []
    cleaned_paths: list[str] = []

    def docker(*args):
        if args[0] == "inspect":
            return 0, "exists"
        if args[:3] == ("exec", cli.CONTAINER, "pg_dump"):
            dump_paths.append(args[-1])
            started.wait(timeout=5)
            return 0, ""
        if args[0] == "cp":
            copied_paths.append(args[1].split(":", 1)[1])
            Path(args[2]).write_bytes(b"PGDMPconcurrent")
            return 0, ""
        if args[:4] == ("exec", cli.CONTAINER, "rm", "-f"):
            cleaned_paths.append(args[4])
            return 0, ""
        return 0, ""

    monkeypatch.setattr(backup.shutil, "which", lambda _name: None)
    monkeypatch.setattr(cli, "_container_port", lambda: "5432")
    monkeypatch.setattr(cli, "_docker", docker)
    destinations = [tmp_path / "one.dump", tmp_path / "two.dump"]
    dsn = "postgresql://postgres:secret@127.0.0.1:5432/memoryd"

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda path: backup._dump_database(dsn, path),
                      destinations))

    assert len(set(dump_paths)) == 2
    assert set(copied_paths) == set(dump_paths)
    assert set(cleaned_paths) == set(dump_paths)


def test_interleaved_docker_restores_never_delete_peer_remote_path(
        monkeypatch, tmp_path):
    copied = threading.Barrier(2)
    copy_paths: list[str] = []
    restore_paths: list[str] = []
    cleaned_paths: list[str] = []

    def docker(*args):
        if args[0] == "inspect":
            return 0, "exists"
        if args[0] == "cp":
            remote = args[2].split(":", 1)[1]
            copy_paths.append(remote)
            copied.wait(timeout=5)
            return 0, ""
        if args[:3] == ("exec", cli.CONTAINER, "pg_restore"):
            restore_paths.append(args[-1])
            return 0, ""
        if args[:4] == ("exec", cli.CONTAINER, "rm", "-f"):
            cleaned_paths.append(args[4])
            return 0, ""
        return 0, ""

    monkeypatch.setattr(backup.shutil, "which", lambda _name: None)
    monkeypatch.setattr(cli, "_container_port", lambda: "5432")
    monkeypatch.setattr(cli, "_docker", docker)
    dumps = [tmp_path / "one.dump", tmp_path / "two.dump"]
    for path in dumps:
        path.write_bytes(b"PGDMPconcurrent")
    dsn = "postgresql://postgres:secret@127.0.0.1:5432/memoryd"

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda path: backup._restore_database(path, dsn), dumps))

    assert len(set(copy_paths)) == 2
    assert set(restore_paths) == set(copy_paths)
    assert set(cleaned_paths) == set(copy_paths)


def test_docker_restore_cp_failure_cleans_its_unique_remote_path(
        monkeypatch, tmp_path):
    copied_paths: list[str] = []
    cleaned_paths: list[str] = []

    def docker(*args):
        if args[0] == "inspect":
            return 0, "exists"
        if args[0] == "cp":
            copied_paths.append(args[2].split(":", 1)[1])
            return 1, "partial copy failed"
        if args[:4] == ("exec", cli.CONTAINER, "rm", "-f"):
            cleaned_paths.append(args[4])
            return 0, ""
        return 0, ""

    monkeypatch.setattr(backup.shutil, "which", lambda _name: None)
    monkeypatch.setattr(cli, "_container_port", lambda: "5432")
    monkeypatch.setattr(cli, "_docker", docker)
    dump = tmp_path / "restore.dump"
    dump.write_bytes(b"PGDMPpartial")

    with pytest.raises(backup.BackupError, match="partial copy failed"):
        backup._restore_database(
            dump,
            "postgresql://postgres:secret@127.0.0.1:5432/memoryd")

    assert len(copied_paths) == 1
    assert cleaned_paths == copied_paths


def test_cli_routes_backup_arguments_and_exit_code(monkeypatch):
    received: list[list[str]] = []
    monkeypatch.setattr(backup, "main", lambda args: received.append(args) or 7)
    monkeypatch.setattr(
        cli.sys, "argv",
        ["memoryd", "backup", "verify", "/tmp/snapshot"])

    with pytest.raises(SystemExit) as exc:
        cli.main()

    assert exc.value.code == 7
    assert received == [["verify", "/tmp/snapshot"]]
