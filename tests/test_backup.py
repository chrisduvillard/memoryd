from __future__ import annotations

import hashlib
import io
import json
import os
import stat
import tarfile
import types
from datetime import datetime, timezone
from pathlib import Path

import pytest

from memoryd import backup
from memoryd import cli


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
    monkeypatch.setattr(backup, "_daemon_health", lambda: None)
    monkeypatch.setattr(backup, "_doctor_findings", lambda _home: [])
    monkeypatch.setattr(
        backup, "_dump_database",
        lambda _dsn, path: path.write_bytes(b"PGDMP\x00unit-test"))


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


def test_create_refuses_running_daemon_before_dump(monkeypatch, tmp_path):
    home = _home(tmp_path)
    called = False
    monkeypatch.setattr(backup, "_daemon_health", lambda: {"ok": True})

    def dump(_dsn, _path):
        nonlocal called
        called = True

    monkeypatch.setattr(backup, "_dump_database", dump)

    with pytest.raises(backup.BackupError, match="stop.*daemon"):
        backup.create_backup(output=tmp_path / "out", home=home)
    assert not called


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


def test_create_refuses_doctor_errors_and_dead_letters(monkeypatch, tmp_path):
    home = _home(tmp_path)
    monkeypatch.setattr(backup, "_daemon_health", lambda: None)
    monkeypatch.setattr(backup, "_doctor_findings", lambda _home: [
        {"severity": "error", "code": "dead_letter_jobs"}])

    with pytest.raises(backup.BackupError, match="dead_letter_jobs"):
        backup.create_backup(output=tmp_path / "out", home=home)


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


def test_restore_refuses_daemon_nonempty_home_and_nonempty_db(
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

    monkeypatch.setattr(backup, "_daemon_health", lambda: {"ok": True})
    with pytest.raises(backup.BackupError, match="stop.*daemon"):
        backup.restore_backup(snapshot, target_dsn="postgresql:///db2",
                              target_home=tmp_path / "third")


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


def test_local_pg_tools_keep_password_out_of_argv(monkeypatch, tmp_path):
    commands: list[tuple[list[str], dict[str, str]]] = []
    dump = tmp_path / "dump"

    def run(command, **kwargs):
        commands.append((command, kwargs["env"]))
        if "pg_dump" in command[0]:
            dump.write_bytes(b"PGDMPbinary\x00\xff")
        return types.SimpleNamespace(returncode=0, stderr=b"")

    monkeypatch.setattr(backup.shutil, "which", lambda name: f"/tools/{name}")
    monkeypatch.setattr(backup.subprocess, "run", run)
    dsn = "postgresql://operator:super-secret@localhost:5432/memoryd"

    backup._dump_database(dsn, dump)
    backup._restore_database(dump, dsn)

    assert dump.read_bytes() == b"PGDMPbinary\x00\xff"
    assert all("super-secret" not in " ".join(command)
               for command, _env in commands)
    assert all(env["PGPASSWORD"] == "super-secret"
               for _command, env in commands)
    assert "--format=custom" in commands[0][0]
    assert "--exit-on-error" in commands[1][0]
    assert "--no-owner" in commands[1][0]


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
