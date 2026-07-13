from __future__ import annotations

import json
import os
import stat
import sys
import types
from pathlib import Path

import pytest

from memoryd import cli


@pytest.fixture(autouse=True)
def _isolated_memory_home(monkeypatch, tmp_path):
    monkeypatch.setattr(cli, "_home", lambda: tmp_path / "memory")


class _Cursor:
    def __init__(self, row=(1,)):
        self._row = row

    def fetchone(self):
        return self._row


class _Connection:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def execute(self, *_args, **_kwargs):
        return _Cursor()


def _fake_psycopg(monkeypatch, connect):
    monkeypatch.setitem(sys.modules, "psycopg", types.SimpleNamespace(connect=connect))


def test_hermes_plugin_install_uses_active_profile_and_exact_private_config(
        monkeypatch, tmp_path):
    hermes_home = tmp_path / "hermes-profile"
    hermes_home.mkdir(mode=0o700)
    source = tmp_path / "plugin-source"
    source.mkdir()
    (source / "__init__.py").write_text("# provider\n", encoding="utf-8")
    (source / "plugin.yaml").write_text(
        "name: memoryd\nversion: 0.3.0\n", encoding="utf-8")

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setattr(cli, "_resource_dir", lambda name: source)

    stale = hermes_home / "memoryd.json"
    stale.write_text('{"url":"http://127.0.0.1:1"}', encoding="utf-8")
    cli.install_hermes_plugin()

    installed = hermes_home / "plugins" / "memoryd"
    assert (installed / "__init__.py").is_file()
    assert not (hermes_home / "plugins" / "memory" / "memoryd").exists()
    assert json.loads(stale.read_text(encoding="utf-8")) == {
        "url": "http://127.0.0.1:7437"}
    if os.name != "nt":
        assert stat.S_IMODE(stale.stat().st_mode) == 0o600
    assert not list(hermes_home.glob(".memoryd.json.*.tmp"))


def test_only_explicit_docker_absence_is_definitive():
    assert cli._container_definitively_absent(
        1, "Error: No such object: memoryd-pgvector")
    assert cli._container_definitively_absent(
        1, "Error: No such container: memoryd-pgvector")
    assert not cli._container_definitively_absent(
        1, "docker executable was not found")


def test_fresh_container_uses_random_masked_password(monkeypatch, capsys):
    docker_calls: list[tuple[str, ...]] = []
    env_observations: list[tuple[Path, str, int]] = []
    passwords = iter(("A" * 43, "B" * 43))

    def docker(*args):
        docker_calls.append(args)
        if args[0] == "inspect":
            return 1, "Error: No such object: memoryd-pgvector"
        if args[:2] == ("volume", "inspect"):
            return 1, "Error: No such volume: memoryd_pgdata"
        if args[0] == "run" and "--env-file" in args:
            path = Path(args[args.index("--env-file") + 1])
            env_observations.append((
                path, path.read_text(encoding="utf-8"),
                stat.S_IMODE(path.stat().st_mode)))
        return 0, "container-id"

    monkeypatch.setattr(cli, "_docker", docker)
    monkeypatch.setattr(cli, "_free_port", lambda: 5439)
    monkeypatch.setattr(cli, "_pg_ready", lambda _dsn, _wait: True)
    monkeypatch.setattr("secrets.token_urlsafe", lambda size: next(passwords))
    _fake_psycopg(monkeypatch, lambda *_args, **_kwargs: _Connection())

    first = cli.ensure_container()
    run = next(call for call in docker_calls if call[0] == "run")

    assert "A" * 43 not in " ".join(run)
    assert len(env_observations) == 1
    env_path, env_text, env_mode = env_observations[0]
    assert env_text == (
        "POSTGRES_PASSWORD=" + "A" * 43 + "\nPOSTGRES_DB=memoryd\n")
    if os.name != "nt":
        assert env_mode == 0o600
    assert not env_path.exists()
    assert "A" * 43 in first
    assert "A" * 43 not in capsys.readouterr().out
    assert cli._mask(first).endswith("postgres:***@127.0.0.1:5439/memoryd")

def test_fresh_container_failure_never_exposes_generated_password(monkeypatch):
    password = "generated-super-secret-password"
    observed_env_paths: list[Path] = []

    def docker(*args):
        if args[0] == "inspect":
            return 1, "Error: No such object: memoryd-pgvector"
        if args[:2] == ("volume", "inspect"):
            return 1, "Error: No such volume: memoryd_pgdata"
        if args[0] == "run":
            assert password not in " ".join(args)
            path = Path(args[args.index("--env-file") + 1])
            assert path.read_text(encoding="utf-8").startswith(
                f"POSTGRES_PASSWORD={password}\n")
            observed_env_paths.append(path)
            return 1, f"failed command contained {password}"
        return 0, ""

    monkeypatch.setattr(cli, "_docker", docker)
    monkeypatch.setattr(cli, "_free_port", lambda: 5439)
    monkeypatch.setattr("secrets.token_urlsafe", lambda _size: password)
    _fake_psycopg(monkeypatch, lambda *_args, **_kwargs: _Connection())

    with pytest.raises(SystemExit) as exc:
        cli.ensure_container()

    assert password not in str(exc.value)
    assert "***" in str(exc.value)
    assert len(observed_env_paths) == 1
    assert not observed_env_paths[0].exists()


def test_next_install_cleans_only_safe_stale_docker_env_files(
        monkeypatch, tmp_path):
    home = tmp_path / "memory"
    home.mkdir()
    stale = home / ".memoryd-docker-env-deadbeef.tmp"
    stale.write_text("POSTGRES_PASSWORD=stale-secret\n", encoding="utf-8")
    if os.name != "nt":
        stale.chmod(0o600)
    unrelated = home / ".memoryd-docker-env-deadbeef.tmp.keep"
    unrelated.write_text("must remain", encoding="utf-8")
    directory = home / ".memoryd-docker-env-directory.tmp"
    directory.mkdir()
    monkeypatch.setattr(cli, "_docker", lambda *_args: (1, "offline"))

    with pytest.raises(SystemExit, match="Docker is not running"):
        cli.ensure_container()

    assert not stale.exists()
    assert unrelated.is_file()
    assert directory.is_dir()


def test_crash_after_container_create_rerun_adopts_managed_credentials(
        monkeypatch, tmp_path):
    running = False
    calls: list[tuple[str, ...]] = []
    readiness_dsns: list[str] = []
    password = "persisted-random-secret"

    def docker(*args):
        nonlocal running
        calls.append(args)
        if args[0] == "inspect":
            return ((0, "exists") if running else
                    (1, "Error: No such object: memoryd-pgvector"))
        if args[:2] == ("volume", "inspect"):
            return 1, "Error: No such volume: memoryd_pgdata"
        if args[0] == "run":
            record = tmp_path / "memory" / ".managed-postgres.json"
            assert record.is_file(), "credential record must precede docker run"
            running = True
            return 0, "container-id"
        return 0, ""

    monkeypatch.setattr(cli, "_docker", docker)
    monkeypatch.setattr(cli, "_container_port", lambda: "5439")
    monkeypatch.setattr(cli, "_free_port", lambda: 5439)
    monkeypatch.setattr(
        cli, "_pg_ready",
        lambda dsn, _wait: readiness_dsns.append(dsn) or True)
    monkeypatch.setattr("secrets.token_urlsafe", lambda _size: password)
    _fake_psycopg(monkeypatch, lambda *_args, **_kwargs: _Connection())

    first = cli.ensure_container()  # caller crashes before migrations/config
    second = cli.ensure_container()

    assert first == second
    assert password in second
    assert sum(call[0] == "run" for call in calls) == 1
    assert readiness_dsns
    assert all(password in dsn for dsn in readiness_dsns)


def test_absent_container_reuses_record_for_initialized_volume(
        monkeypatch, tmp_path):
    stored_password = "credential-that-initialized-the-volume"
    cli._write_managed_credentials(
        cli._managed_credential_value(5439, stored_password))
    running = False
    run_calls: list[tuple[str, ...]] = []

    def docker(*args):
        nonlocal running
        if args[0] == "info":
            return 0, ""
        if args[0] == "inspect":
            return ((0, "exists") if running else
                    (1, "Error: No such object: memoryd-pgvector"))
        if args[:2] == ("volume", "inspect"):
            return 0, "initialized-volume"
        if args[0] == "run":
            run_calls.append(args)
            running = True
            return 0, "container-id"
        return 0, ""

    monkeypatch.setattr(cli, "_docker", docker)
    monkeypatch.setattr(cli, "_free_port", lambda: 5440)
    monkeypatch.setattr(
        cli, "_pg_ready",
        lambda dsn, _wait: stored_password in dsn)
    monkeypatch.setattr(
        "secrets.token_urlsafe",
        lambda _size: pytest.fail("must not replace an established credential"))
    _fake_psycopg(monkeypatch, lambda *_args, **_kwargs: _Connection())

    dsn = cli.ensure_container()

    assert stored_password in dsn
    assert ":5440/" in dsn
    assert len(run_calls) == 1
    record = json.loads(
        (tmp_path / "memory" / ".managed-postgres.json").read_text())
    assert record["password"] == stored_password
    assert record["port"] == 5440


def test_initialized_legacy_volume_is_recovered_without_random_secret(
        monkeypatch, tmp_path):
    probe_calls: list[tuple[str, ...]] = []

    def docker(*args):
        if args[0] == "info":
            return 0, ""
        if args[0] == "inspect":
            return 1, "Error: No such object: memoryd-pgvector"
        if args[:2] == ("volume", "inspect"):
            return 0, "initialized-volume"
        if args[0] == "run" and "--rm" in args:
            probe_calls.append(args)
            return 0, ""
        return 0, "container-id"

    monkeypatch.setattr(cli, "_docker", docker)
    monkeypatch.setattr(cli, "_free_port", lambda: 5439)
    monkeypatch.setattr(
        cli, "_pg_ready",
        lambda dsn, _wait: f":{cli.LEGACY_PG_PASSWORD}@" in dsn)
    monkeypatch.setattr(
        "secrets.token_urlsafe",
        lambda _size: pytest.fail("legacy recovery must not invent a password"))
    _fake_psycopg(monkeypatch, lambda *_args, **_kwargs: _Connection())

    dsn = cli.ensure_container()

    assert f":{cli.LEGACY_PG_PASSWORD}@" in dsn
    assert len(probe_calls) == 1
    probe = probe_calls[0]
    assert probe[probe.index("--entrypoint") + 1] == "test"
    assert probe[-2:] == ("-s", "/var/lib/postgresql/data/PG_VERSION")
    assert "sh" not in probe and "-c" not in probe
    record = json.loads(
        (tmp_path / "memory" / ".managed-postgres.json").read_text())
    assert record["password"] == cli.LEGACY_PG_PASSWORD


def test_existing_empty_volume_is_classified_fresh_and_gets_random_secret(
        monkeypatch, tmp_path):
    password = "random-for-empty-volume"
    probe_calls: list[tuple[str, ...]] = []
    persistent_runs: list[tuple[str, ...]] = []

    def docker(*args):
        if args[0] == "info":
            return 0, ""
        if args[0] == "inspect":
            return 1, "Error: No such object: memoryd-pgvector"
        if args[:2] == ("volume", "inspect"):
            return 0, "existing-empty-volume"
        if args[0] == "run" and "--rm" in args:
            probe_calls.append(args)
            return 1, ""
        if args[0] == "run":
            persistent_runs.append(args)
            return 0, "container-id"
        return 0, ""

    monkeypatch.setattr(cli, "_docker", docker)
    monkeypatch.setattr(cli, "_free_port", lambda: 5439)
    monkeypatch.setattr(cli, "_pg_ready", lambda dsn, _wait: password in dsn)
    monkeypatch.setattr("secrets.token_urlsafe", lambda _size: password)
    _fake_psycopg(monkeypatch, lambda *_args, **_kwargs: _Connection())

    dsn = cli.ensure_container()

    assert password in dsn
    assert len(probe_calls) == 1
    assert len(persistent_runs) == 1
    record = json.loads(
        (tmp_path / "memory" / ".managed-postgres.json").read_text())
    assert record["password"] == password


def test_existing_volume_probe_inconclusive_refuses_without_mutation(
        monkeypatch, tmp_path):
    generated: list[str] = []
    persistent_runs: list[tuple[str, ...]] = []

    def docker(*args):
        if args[0] == "info":
            return 0, ""
        if args[0] == "inspect":
            return 1, "Error: No such object: memoryd-pgvector"
        if args[:2] == ("volume", "inspect"):
            return 0, "existing-volume"
        if args[0] == "run" and "--rm" in args:
            return 125, "Docker daemon lost the probe"
        if args[0] == "run":
            persistent_runs.append(args)
            return 0, "container-id"
        return 0, ""

    monkeypatch.setattr(cli, "_docker", docker)
    monkeypatch.setattr(cli, "_free_port", lambda: 5439)
    monkeypatch.setattr(cli, "_pg_ready", lambda _dsn, _wait: True)
    monkeypatch.setattr(
        "secrets.token_urlsafe",
        lambda _size: generated.append("random") or "random")
    _fake_psycopg(monkeypatch, lambda *_args, **_kwargs: _Connection())

    with pytest.raises(SystemExit, match="cannot classify PostgreSQL data"):
        cli.ensure_container()

    assert generated == []
    assert persistent_runs == []
    assert not (tmp_path / "memory" / ".managed-postgres.json").exists()


def test_initialized_unknown_volume_refuses_without_random_record(
        monkeypatch, tmp_path):
    generated: list[str] = []

    def docker(*args):
        if args[0] == "info":
            return 0, ""
        if args[0] == "inspect":
            return 1, "Error: No such object: memoryd-pgvector"
        if args[:2] == ("volume", "inspect"):
            return 0, "initialized-volume"
        return 0, "container-id"

    monkeypatch.setattr(cli, "_docker", docker)
    monkeypatch.setattr(cli, "_free_port", lambda: 5439)
    monkeypatch.setattr(cli, "_pg_ready", lambda _dsn, _wait: False)
    monkeypatch.setattr(
        "secrets.token_urlsafe",
        lambda _size: generated.append("fresh-random") or "fresh-random")
    _fake_psycopg(monkeypatch, lambda *_args, **_kwargs: _Connection())

    with pytest.raises(SystemExit, match="credentials are unknown"):
        cli.ensure_container()

    assert generated == []
    assert not (tmp_path / "memory" / ".managed-postgres.json").exists()


def test_definitive_docker_run_failure_removes_pending_credential_record(
        monkeypatch, tmp_path):
    observed_record = False

    def docker(*args):
        nonlocal observed_record
        if args[0] == "inspect":
            return 1, "Error: No such object: memoryd-pgvector"
        if args[:2] == ("volume", "inspect"):
            return 1, "Error: No such volume: memoryd_pgdata"
        if args[0] == "run":
            observed_record = (
                tmp_path / "memory" / ".managed-postgres.json").is_file()
            return 1, "creation failed"
        return 0, ""

    monkeypatch.setattr(cli, "_docker", docker)
    monkeypatch.setattr(cli, "_free_port", lambda: 5439)
    _fake_psycopg(monkeypatch, lambda *_args, **_kwargs: _Connection())

    with pytest.raises(SystemExit, match="docker run failed"):
        cli.ensure_container()

    assert observed_record
    assert not (tmp_path / "memory" / ".managed-postgres.json").exists()


def test_docker_run_failure_with_initialized_volume_retains_pending_record(
        monkeypatch, tmp_path):
    run_attempted = False
    password = "volume-may-have-used-this-secret"

    def docker(*args):
        nonlocal run_attempted
        if args[0] == "inspect":
            return 1, "Error: No such object: memoryd-pgvector"
        if args[:2] == ("volume", "inspect"):
            if run_attempted:
                return 0, "initialized-volume"
            return 1, "Error: No such volume: memoryd_pgdata"
        if args[0] == "run":
            run_attempted = True
            return 1, "container creation failed after volume initialization"
        return 0, ""

    monkeypatch.setattr(cli, "_docker", docker)
    monkeypatch.setattr(cli, "_free_port", lambda: 5439)
    monkeypatch.setattr("secrets.token_urlsafe", lambda _size: password)
    _fake_psycopg(monkeypatch, lambda *_args, **_kwargs: _Connection())

    with pytest.raises(SystemExit, match="credentials retained"):
        cli.ensure_container()

    record = json.loads(
        (tmp_path / "memory" / ".managed-postgres.json").read_text())
    assert record["password"] == password


def test_docker_run_timeout_with_delayed_container_retains_credentials(
        monkeypatch, tmp_path):
    exists = False
    password = "delayed-container-secret"

    def docker(*args):
        nonlocal exists
        if args[0] == "inspect":
            return ((0, "exists") if exists else
                    (1, "Error: No such object: memoryd-pgvector"))
        if args[:2] == ("volume", "inspect"):
            return 1, "Error: No such volume: memoryd_pgdata"
        if args[0] == "run":
            exists = True
            return 1, f"timed out after creating with {password}"
        return 0, ""

    monkeypatch.setattr(cli, "_docker", docker)
    monkeypatch.setattr(cli, "_free_port", lambda: 5439)
    monkeypatch.setattr("secrets.token_urlsafe", lambda _size: password)
    _fake_psycopg(monkeypatch, lambda *_args, **_kwargs: _Connection())

    with pytest.raises(SystemExit, match="credentials retained.*re-run") as exc:
        cli.ensure_container()

    assert password not in str(exc.value)
    assert (tmp_path / "memory" / ".managed-postgres.json").is_file()


def test_docker_run_failure_with_unknown_inspect_retains_credentials(
        monkeypatch, tmp_path):
    inspections = 0

    def docker(*args):
        nonlocal inspections
        if args[0] == "inspect":
            inspections += 1
            if inspections == 1:
                return 1, "Error: No such object: memoryd-pgvector"
            return 1, "daemon response was inconclusive"
        if args[:2] == ("volume", "inspect"):
            return 1, "Error: No such volume: memoryd_pgdata"
        if args[0] == "run":
            return 1, "connection reset"
        return 0, ""

    monkeypatch.setattr(cli, "_docker", docker)
    monkeypatch.setattr(cli, "_free_port", lambda: 5439)
    _fake_psycopg(monkeypatch, lambda *_args, **_kwargs: _Connection())

    with pytest.raises(SystemExit, match="credentials retained.*inspect"):
        cli.ensure_container()

    assert (tmp_path / "memory" / ".managed-postgres.json").is_file()


def test_existing_legacy_container_is_adopted_without_deletion(monkeypatch):
    docker_calls: list[tuple[str, ...]] = []

    def docker(*args):
        docker_calls.append(args)
        if args[:2] == ("inspect", cli.CONTAINER):
            return 0, "exists"
        if args[0] == "inspect":
            return 0, "5440"
        return 0, ""

    monkeypatch.setattr(cli, "_docker", docker)
    monkeypatch.setattr(cli, "_container_port", lambda: "5440")
    monkeypatch.setattr(cli, "_pg_ready", lambda dsn, _wait: ":memoryd@" in dsn)
    _fake_psycopg(monkeypatch, lambda *_args, **_kwargs: _Connection())

    assert cli.ensure_container() == (
        "postgresql://postgres:memoryd@127.0.0.1:5440/memoryd"
    )
    assert not any(call[:2] == ("rm", "-f") for call in docker_calls)
    assert any(call[:3] == ("update", "--restart", "unless-stopped")
               for call in docker_calls)


def test_existing_unknown_container_credentials_refuse_without_deletion(
        monkeypatch):
    docker_calls: list[tuple[str, ...]] = []

    def docker(*args):
        docker_calls.append(args)
        return (0, "exists") if args[0] == "inspect" else (0, "")

    monkeypatch.setattr(cli, "_docker", docker)
    monkeypatch.setattr(cli, "_container_port", lambda: "5432")
    monkeypatch.setattr(cli, "_pg_ready", lambda _dsn, _wait: False)
    _fake_psycopg(monkeypatch, lambda *_args, **_kwargs: _Connection())

    with pytest.raises(SystemExit, match="MEMORYD_DSN") as exc:
        cli.ensure_container()

    assert "not been removed" in str(exc.value)
    assert not any(call[0] == "rm" for call in docker_calls)


def test_install_reuses_working_config_dsn_without_docker(monkeypatch, tmp_path):
    dsn = "postgresql://operator:secret@db.example/memoryd"
    (tmp_path / "config.json").write_text(json.dumps({"dsn": dsn}))
    used: list[str] = []
    monkeypatch.setattr(cli, "_home", lambda: tmp_path)
    monkeypatch.setattr(cli, "ensure_container", lambda: pytest.fail("Docker used"))
    monkeypatch.setattr(cli, "apply_migrations", lambda value: used.append(value) or [])
    monkeypatch.setattr(cli, "write_config", lambda _dsn: tmp_path / "config.json")
    monkeypatch.setattr(cli, "register_claude_hooks", lambda: tmp_path / "settings")
    monkeypatch.setattr(cli, "install_hermes_plugin", lambda: None)
    monkeypatch.setattr(cli, "install_autostart", lambda: None)
    monkeypatch.setattr(cli, "_start_daemon_now", lambda: None)
    monkeypatch.setattr(cli, "_health", lambda: {"ok": True})
    monkeypatch.setattr(cli, "status", lambda: 0)
    monkeypatch.setattr(cli.time, "sleep", lambda _seconds: None)
    _fake_psycopg(monkeypatch, lambda value, **_kwargs: (
        used.append(value) or _Connection()))

    assert cli.install() == 0
    assert dsn in used


def test_linux_installer_writes_backup_units_and_enables_timer(
        monkeypatch, tmp_path):
    calls: list[list[str]] = []
    monkeypatch.setattr(cli.sys, "platform", "linux")
    monkeypatch.setattr(
        cli.sys, "executable", "/opt/My Python/bin/python%worker")
    monkeypatch.setattr(Path, "expanduser", lambda self: (
        tmp_path if self.parts[:1] == ("~",) else self))
    monkeypatch.setattr(cli, "_run", lambda cmd, timeout=120: (
        calls.append(cmd) or (0, "")))

    cli.install_autostart()

    service = (tmp_path / "memoryd-backup.service").read_text()
    timer = (tmp_path / "memoryd-backup.timer").read_text()
    assert "ExecStartPre=systemctl --user stop memoryd.service" in service
    assert ('ExecStart="/opt/My Python/bin/python%%worker" -m memoryd '
            'backup create --retain 14') in service
    assert "ExecStopPost=systemctl --user start memoryd.service" in service
    daemon_service = (tmp_path / "memoryd.service").read_text()
    assert ('ExecStart="/opt/My Python/bin/python%%worker" '
            '-m memoryd serve') in daemon_service
    assert "OnCalendar=*-*-* 02:35:00" in timer
    assert "Persistent=true" in timer
    enable = next(call for call in calls if "enable" in call)
    assert enable[-3:] == [
        "memoryd.service", "memoryd-microsleep.timer", "memoryd-backup.timer"]


def test_linux_uninstall_disables_and_removes_backup_units(monkeypatch, tmp_path):
    calls: list[list[str]] = []
    for name in ("memoryd-backup.service", "memoryd-backup.timer"):
        (tmp_path / name).write_text("unit")
    monkeypatch.setattr(cli.sys, "platform", "linux")
    monkeypatch.setattr(Path, "expanduser", lambda self: (
        tmp_path if self.parts[:1] == ("~",) else self))
    monkeypatch.setattr(cli, "_run", lambda cmd, timeout=120: (
        calls.append(cmd) or (0, "")))
    monkeypatch.setattr(cli, "_home", lambda: tmp_path / "data")

    cli.uninstall()

    disable_timer = ["systemctl", "--user", "disable", "--now",
                     "memoryd-backup.timer"]
    stop_backup = ["systemctl", "--user", "stop", "memoryd-backup.service"]
    stop_daemon = ["systemctl", "--user", "disable", "--now",
                   "memoryd.service", "memoryd-microsleep.timer"]
    assert disable_timer in calls
    assert stop_backup in calls
    assert stop_daemon in calls
    assert calls.index(disable_timer) < calls.index(stop_backup)
    assert calls.index(stop_backup) < calls.index(stop_daemon)
    assert not (tmp_path / "memoryd-backup.service").exists()
    assert not (tmp_path / "memoryd-backup.timer").exists()
