from __future__ import annotations

import builtins
import io
import json
import multiprocessing
import os
import signal
import subprocess
import sys
import threading
import time
import traceback
from dataclasses import FrozenInstanceError
from pathlib import Path
from types import SimpleNamespace

import pytest

from memoryd import hermes_install as hermes
from memoryd.hermes_compat import HermesTarget


SECRET = "CHILD-CONFIG-SECRET-SENTINEL"


def _move_job_under_plugin_lock(
    lock_path: str, source: str, destination: str, locked, release,
) -> None:
    import fcntl

    descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        os.chmod(lock_path, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        locked.set()
        if not release.wait(5):
            return
        os.replace(source, destination)
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _hold_plugin_lock(lock_path: str, locked, hold_seconds: float) -> None:
    import fcntl

    descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        os.chmod(lock_path, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        locked.set()
        time.sleep(hold_seconds)
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _write_provider(home: Path, provider: str | None) -> None:
    value = "null" if provider is None else provider
    path = home / "config.yaml"
    path.write_text(f"memory:\n  provider: {value}\n", encoding="utf-8")
    os.chmod(path, 0o600)


def _read_provider(home: Path) -> str | None:
    for line in (home / "config.yaml").read_text(encoding="utf-8").splitlines():
        if line.strip().startswith("provider:"):
            value = line.split(":", 1)[1].strip()
            return None if value in {"", "null", "~"} else value
    return None


def _target(tmp_path: Path, provider: str | None = "legacy") -> HermesTarget:
    root = tmp_path / "hermes"
    home = root / "profiles" / "selected"
    home.mkdir(parents=True)
    os.chmod(home, 0o700)
    _write_provider(home, provider)
    plugin_config = home / "memoryd.json"
    plugin_config.write_text(
        json.dumps({"url": "http://127.0.0.1:7437"}), encoding="utf-8"
    )
    os.chmod(plugin_config, 0o600)
    return HermesTarget(
        root=root,
        home=home,
        executable=tmp_path / "venv" / "bin" / "hermes",
        python=tmp_path / "venv" / "bin" / "python",
    )


class HermesBoundary:
    def __init__(
        self,
        target: HermesTarget,
        *,
        gateway_running: bool = False,
        failures: dict[str, list[object]] | None = None,
        skip_provider_writes: set[str] | None = None,
        sticky_stop: bool = False,
    ) -> None:
        self.target = target
        self.gateway_running = gateway_running
        self.failures = {name: list(values) for name, values in (failures or {}).items()}
        self.skip_provider_writes = skip_provider_writes or set()
        self.sticky_stop = sticky_stop
        self.events: list[str] = []
        self.environments: list[dict[str, str]] = []

    def _result(self, command: list[str], returncode: int, stdout: str = ""):
        return subprocess.CompletedProcess(command, returncode, stdout, SECRET)

    def _failure(self, event: str, command: list[str]):
        queued = self.failures.get(event)
        if not queued:
            return None
        failure = queued.pop(0)
        if isinstance(failure, BaseException):
            raise failure
        return self._result(command, int(failure), SECRET)

    def run(self, command, **kwargs):
        command = [os.fspath(item) for item in command]
        environment = kwargs["env"]
        assert environment["HERMES_HOME"] == os.fspath(self.target.home)
        assert environment["UNRELATED_ACTIVATION_TEST"] == "preserved"
        assert kwargs.get("shell") in (None, False)
        self.environments.append(dict(environment))

        if command[0] == os.fspath(self.target.python):
            script = command[-1]
            if "get_gateway_runtime_snapshot" in script:
                event = "gateway-probe"
                self.events.append(event)
                if failed := self._failure(event, command):
                    return failed
                return self._result(command, 0 if self.gateway_running else 1)
            event = "provider-probe"
            self.events.append(event)
            if failed := self._failure(event, command):
                return failed
            return self._result(command, 0, json.dumps(_read_provider(self.target.home)) + "\n")

        assert command[0] == os.fspath(self.target.executable)
        arguments = command[1:]
        event = "hermes " + " ".join(arguments)
        self.events.append(event)
        if failed := self._failure(event, command):
            return failed
        if arguments == ["gateway", "stop"]:
            if not self.sticky_stop:
                self.gateway_running = False
        elif arguments == ["gateway", "start"]:
            self.gateway_running = True
        elif arguments[:4] == ["config", "set", "memory.provider", "memoryd"]:
            if "memoryd" not in self.skip_provider_writes:
                _write_provider(self.target.home, "memoryd")
        elif arguments[:3] == ["config", "set", "memory.provider"]:
            provider = arguments[3]
            if provider not in self.skip_provider_writes:
                _write_provider(self.target.home, provider)
        elif arguments == ["memory", "off"]:
            if "off" not in self.skip_provider_writes:
                _write_provider(self.target.home, None)
        return self._result(command, 0)


def _install_boundary(
    monkeypatch, target: HermesTarget, *, gateway_running: bool = False,
    failures: dict[str, list[object]] | None = None,
    skip_provider_writes: set[str] | None = None,
    sticky_stop: bool = False,
) -> HermesBoundary:
    monkeypatch.setenv("UNRELATED_ACTIVATION_TEST", "preserved")
    boundary = HermesBoundary(
        target,
        gateway_running=gateway_running,
        failures=failures,
        skip_provider_writes=skip_provider_writes,
        sticky_stop=sticky_stop,
    )
    monkeypatch.setattr(hermes.subprocess, "run", boundary.run)
    monkeypatch.setattr(
        hermes.cli,
        "status",
        lambda: boundary.events.append("memoryd cli status") or 0,
    )
    return boundary


def _spool_root(target: HermesTarget) -> Path:
    root = target.home / "spool" / "memoryd"
    for name in ("incoming", "processing", "dead-letter"):
        (root / name).mkdir(parents=True, exist_ok=True)
    return root


def _rendered_exception(error: BaseException) -> str:
    return repr(error) + "".join(traceback.format_exception(error))


@pytest.mark.parametrize(
    ("provider", "gateway_running"),
    [(None, False), ("legacy", True), ("memoryd", False)],
)
def test_capture_runtime_state_uses_selected_profile_and_pinned_runtime(
    monkeypatch, tmp_path, provider, gateway_running,
):
    target = _target(tmp_path, provider)
    boundary = _install_boundary(
        monkeypatch, target, gateway_running=gateway_running
    )

    state = hermes.capture_runtime_state(target)

    assert state == hermes.HermesRuntimeState(provider, gateway_running)
    assert boundary.events == ["provider-probe", "gateway-probe"]
    with pytest.raises(FrozenInstanceError):
        state.provider = "changed"


@pytest.mark.parametrize(
    "output",
    ["not-json", "{}", "[]", "false", '"UpperCase"', '"bad provider"', "null\nnull"],
)
def test_capture_rejects_malformed_or_unsafe_provider_without_disclosure(
    monkeypatch, tmp_path, output,
):
    target = _target(tmp_path)
    monkeypatch.setenv("UNRELATED_ACTIVATION_TEST", "preserved")

    def run(command, **kwargs):
        assert kwargs["env"]["HERMES_HOME"] == os.fspath(target.home)
        return subprocess.CompletedProcess(command, 0, output + SECRET, SECRET)

    monkeypatch.setattr(hermes.subprocess, "run", run)

    with pytest.raises(hermes.HermesInstallError, match="provider|state") as caught:
        hermes.capture_runtime_state(target)

    assert SECRET not in _rendered_exception(caught.value)


@pytest.mark.parametrize(
    ("failure_event", "failure"),
    [("provider-probe", 9), ("gateway-probe", 2),
     ("provider-probe", OSError(SECRET))],
)
def test_capture_probe_failures_are_sanitized(
    monkeypatch, tmp_path, failure_event, failure,
):
    target = _target(tmp_path)
    boundary = _install_boundary(
        monkeypatch, target, failures={failure_event: [failure]}
    )

    with pytest.raises(hermes.HermesInstallError, match="provider|gateway") as caught:
        hermes.capture_runtime_state(target)

    assert SECRET not in _rendered_exception(caught.value)
    assert boundary.events[-1] == failure_event


@pytest.mark.parametrize(
    "provider_yaml",
    [
        f"\n    secret: {SECRET}",
        f"\n    - {SECRET}",
        f' "Unsafe Provider {SECRET}"',
    ],
)
def test_real_provider_child_rejects_unsafe_value_without_emitting_it(
    tmp_path, provider_yaml,
):
    home = tmp_path / "profile"
    home.mkdir()
    (home / "config.yaml").write_text(
        f"memory:\n  provider:{provider_yaml}\n", encoding="utf-8"
    )
    environment = dict(os.environ)
    environment["HERMES_HOME"] = os.fspath(home)

    result = subprocess.run(
        [sys.executable, "-c", hermes._PROVIDER_PROBE],
        check=False,
        env=environment,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode != 0
    assert result.stdout == ""
    assert result.stderr == ""
    assert SECRET not in result.stdout + result.stderr


@pytest.mark.parametrize("gateway_running", [False, True])
def test_activation_succeeds_with_exact_check_order_and_original_gateway_state(
    monkeypatch, tmp_path, gateway_running,
):
    target = _target(tmp_path, "legacy")
    _spool_root(target)
    boundary = _install_boundary(
        monkeypatch, target, gateway_running=gateway_running
    )

    assert hermes.activate_and_verify(target) is None

    assert _read_provider(target.home) == "memoryd"
    assert boundary.gateway_running is gateway_running
    checks = [
        "hermes memory status",
        "hermes memoryd config",
        "memoryd cli status",
        "hermes memoryd status",
    ]
    assert [event for event in boundary.events if event in checks] == checks
    assert ("hermes gateway stop" in boundary.events) is gateway_running
    assert ("hermes gateway start" in boundary.events) is gateway_running


def test_active_gateway_must_be_proven_stopped_before_activation(
    monkeypatch, tmp_path,
):
    target = _target(tmp_path, "legacy")
    boundary = _install_boundary(
        monkeypatch, target, gateway_running=True, sticky_stop=True
    )

    with pytest.raises(hermes.HermesInstallError, match="gateway stop"):
        hermes.activate_and_verify(target)

    assert "hermes config set memory.provider memoryd" not in boundary.events
    assert _read_provider(target.home) == "legacy"


def test_activation_must_be_independently_verified_before_checks(
    monkeypatch, tmp_path,
):
    target = _target(tmp_path, "legacy")
    boundary = _install_boundary(
        monkeypatch, target, skip_provider_writes={"memoryd"}
    )

    with pytest.raises(hermes.HermesInstallError, match="provider verification"):
        hermes.activate_and_verify(target)

    assert "hermes memory status" not in boundary.events
    assert _read_provider(target.home) == "legacy"


def test_activation_requires_exact_local_plugin_url(
    monkeypatch, tmp_path,
):
    target = _target(tmp_path, "legacy")
    config = target.home / "memoryd.json"
    config.write_text(json.dumps({"url": "http://localhost:7437"}), encoding="utf-8")
    os.chmod(config, 0o600)
    boundary = _install_boundary(monkeypatch, target)

    with pytest.raises(hermes.HermesInstallError, match="plugin config") as caught:
        hermes.activate_and_verify(target)

    assert "memoryd cli status" not in boundary.events
    assert _read_provider(target.home) == "legacy"
    assert "localhost" not in str(caught.value)


@pytest.mark.parametrize("condition", ["dead-letter", "fault", "unreadable-state"])
def test_activation_rejects_spool_evidence_immediately_and_preserves_it(
    monkeypatch, tmp_path, condition,
):
    target = _target(tmp_path, "legacy")
    root = _spool_root(target)
    if condition == "dead-letter":
        evidence = root / "dead-letter" / "000-secret.json"
        evidence.write_bytes(b"dead-letter-secret-evidence")
    else:
        evidence = root / "state.json"
        evidence.write_text(
            json.dumps({"durability_fault": SECRET})
            if condition == "fault" else "not-json",
            encoding="utf-8",
        )
    before = evidence.read_bytes()
    _install_boundary(monkeypatch, target)

    with pytest.raises(hermes.HermesInstallError, match="spool") as caught:
        hermes.activate_and_verify(target)

    assert evidence.read_bytes() == before
    assert _read_provider(target.home) == "legacy"
    assert SECRET not in _rendered_exception(caught.value)


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink safety")
def test_activation_treats_broken_spool_state_symlink_as_unreadable(
    monkeypatch, tmp_path,
):
    target = _target(tmp_path, "legacy")
    root = _spool_root(target)
    state = root / "state.json"
    state.symlink_to(root / "missing-state.json")
    _install_boundary(monkeypatch, target)

    with pytest.raises(hermes.HermesInstallError, match="spool"):
        hermes.activate_and_verify(target)

    assert state.is_symlink()
    assert _read_provider(target.home) == "legacy"


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink safety")
def test_spool_snapshot_rejects_symlinked_spool_ancestor(tmp_path):
    target = _target(tmp_path, "legacy")
    outside = tmp_path / "outside-spool"
    for name in ("incoming", "processing", "dead-letter"):
        (outside / "memoryd" / name).mkdir(parents=True, exist_ok=True)
    (target.home / "spool").symlink_to(outside, target_is_directory=True)

    with pytest.raises(hermes.HermesInstallError, match="spool"):
        hermes._pending_spool_jobs(target)


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink safety")
def test_spool_snapshot_rejects_symlinked_lock(tmp_path):
    target = _target(tmp_path, "legacy")
    root = _spool_root(target)
    outside = tmp_path / "outside-lock"
    outside.write_text("operator evidence", encoding="utf-8")
    (root / "spool.lock").symlink_to(outside)

    with pytest.raises(hermes.HermesInstallError, match="spool"):
        hermes._pending_spool_jobs(target)

    assert outside.read_text(encoding="utf-8") == "operator evidence"


@pytest.mark.skipif(os.name == "nt", reason="POSIX directory permissions")
def test_spool_snapshot_rejects_unreadable_state_directory(tmp_path):
    target = _target(tmp_path, "legacy")
    incoming = _spool_root(target) / "incoming"
    incoming.chmod(0)
    try:
        with pytest.raises(hermes.HermesInstallError, match="spool"):
            hermes._pending_spool_jobs(target)
    finally:
        incoming.chmod(0o700)


@pytest.mark.skipif(os.name == "nt", reason="POSIX advisory locks")
def test_spool_snapshot_signals_busy_without_waiting_for_plugin_lock(tmp_path):
    target = _target(tmp_path, "legacy")
    root = _spool_root(target)
    source = root / "processing" / "000-job.json"
    destination = root / "incoming" / source.name
    source.write_text("{}", encoding="utf-8")
    context = multiprocessing.get_context("fork")
    locked = context.Event()
    release = context.Event()
    mover = context.Process(
        target=_move_job_under_plugin_lock,
        args=(
            os.fspath(root / "spool.lock"),
            os.fspath(source),
            os.fspath(destination),
            locked,
            release,
        ),
    )
    mover.start()
    assert locked.wait(5)
    results: list[int] = []
    failures: list[BaseException] = []
    finished = threading.Event()

    def inspect() -> None:
        try:
            results.append(hermes._pending_spool_jobs(target))
        except BaseException as error:
            failures.append(error)
        finally:
            finished.set()

    reader = threading.Thread(target=inspect, daemon=True)
    reader.start()
    finished_while_move_locked = finished.wait(0.5)
    release.set()
    mover.join(5)
    reader.join(5)

    assert finished_while_move_locked
    assert mover.exitcode == 0
    assert not reader.is_alive()
    assert results == []
    assert [failure.__class__.__name__ for failure in failures] == [
        "_SpoolLockBusy"
    ]
    assert hermes._pending_spool_jobs(target) == 1
    assert destination.exists()
    assert not source.exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX advisory locks")
def test_spool_drain_times_out_while_plugin_lock_is_held_without_mutating_job(
    tmp_path,
):
    target = _target(tmp_path, "legacy")
    root = _spool_root(target)
    job = root / "incoming" / "000-job.json"
    evidence = b"pending-secret-evidence"
    job.write_bytes(evidence)
    context = multiprocessing.get_context("fork")
    locked = context.Event()
    holder = context.Process(
        target=_hold_plugin_lock,
        args=(os.fspath(root / "spool.lock"), locked, 0.8),
    )
    holder.start()
    assert locked.wait(5)

    started = time.monotonic()
    try:
        with pytest.raises(hermes.HermesInstallError, match="spool.*drain"):
            hermes._wait_for_spool_drain(target, timeout=0.2)
        elapsed = time.monotonic() - started
    finally:
        holder.join(5)

    assert holder.exitcode == 0
    assert 0.15 <= elapsed < 0.6
    assert job.read_bytes() == evidence


class FakeClock:
    def __init__(self, on_sleep=None) -> None:
        self.now = 0.0
        self.on_sleep = on_sleep

    def monotonic(self) -> float:
        return self.now

    def sleep(self, delay: float) -> None:
        self.now += delay
        if self.on_sleep is not None:
            self.on_sleep()


def test_activation_polls_real_spool_until_incoming_and_processing_are_zero(
    monkeypatch, tmp_path,
):
    target = _target(tmp_path, "legacy")
    root = _spool_root(target)
    incoming = root / "incoming" / "000-job.json"
    processing = root / "processing" / "001-job.json"
    incoming.write_text("{}", encoding="utf-8")
    processing.write_text("{}", encoding="utf-8")

    def drain_once():
        incoming.unlink(missing_ok=True)
        processing.unlink(missing_ok=True)

    clock = FakeClock(drain_once)
    monkeypatch.setattr(hermes.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(hermes.time, "sleep", clock.sleep)
    boundary = _install_boundary(monkeypatch, target)

    hermes.activate_and_verify(target)

    assert clock.now > 0
    assert _read_provider(target.home) == "memoryd"
    assert boundary.gateway_running is False


def test_activation_times_out_after_default_fifteen_seconds_without_requeue(
    monkeypatch, tmp_path,
):
    target = _target(tmp_path, "legacy")
    root = _spool_root(target)
    incoming = root / "incoming" / "000-job.json"
    incoming.write_bytes(b"pending-secret-evidence")
    clock = FakeClock()
    monkeypatch.setattr(hermes.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(hermes.time, "sleep", clock.sleep)
    _install_boundary(monkeypatch, target)

    with pytest.raises(hermes.HermesInstallError, match="spool drain"):
        hermes.activate_and_verify(target)

    assert clock.now == pytest.approx(15.0)
    assert incoming.read_bytes() == b"pending-secret-evidence"
    assert _read_provider(target.home) == "legacy"


def test_spool_drain_rejects_zero_snapshot_completed_after_deadline(
    monkeypatch, tmp_path,
):
    target = _target(tmp_path, "legacy")
    clock = FakeClock()

    def slow_zero_snapshot(_target):
        clock.now += 0.3
        return 0

    monkeypatch.setattr(hermes.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(hermes.time, "sleep", clock.sleep)
    monkeypatch.setattr(hermes, "_pending_spool_jobs", slow_zero_snapshot)

    with pytest.raises(hermes.HermesInstallError, match="spool.*drain"):
        hermes._wait_for_spool_drain(target, timeout=0.2)

    assert clock.now == pytest.approx(0.3)


@pytest.mark.parametrize(
    ("gateway_running", "failure_event", "failure"),
    [
        (True, "hermes gateway stop", 7),
        (False, "hermes config set memory.provider memoryd", 7),
        (False, "hermes memory status", 7),
        (False, "hermes memoryd config", 7),
        (False, "hermes memoryd status", 7),
        (True, "hermes gateway start", 7),
    ],
)
def test_failure_at_each_hermes_stage_rolls_back_provider_and_gateway(
    monkeypatch, tmp_path, gateway_running, failure_event, failure,
):
    target = _target(tmp_path, "external-provider")
    boundary = _install_boundary(
        monkeypatch,
        target,
        gateway_running=gateway_running,
        failures={failure_event: [failure]},
    )

    with pytest.raises(hermes.HermesInstallError) as caught:
        hermes.activate_and_verify(target)

    assert _read_provider(target.home) == "external-provider"
    assert boundary.gateway_running is gateway_running
    assert SECRET not in _rendered_exception(caught.value)


@pytest.mark.parametrize(
    "interruption", [KeyboardInterrupt(SECRET), SystemExit(SECRET), RuntimeError(SECRET)]
)
def test_in_process_failure_or_interruption_is_sanitized_and_rolled_back(
    monkeypatch, tmp_path, interruption,
):
    target = _target(tmp_path, "legacy")
    boundary = _install_boundary(monkeypatch, target, gateway_running=True)

    def interrupted_status():
        boundary.events.append("memoryd cli status")
        raise interruption

    monkeypatch.setattr(hermes.cli, "status", interrupted_status)

    with pytest.raises(hermes.HermesInstallError, match="memoryd status") as caught:
        hermes.activate_and_verify(target)

    assert _read_provider(target.home) == "legacy"
    assert boundary.gateway_running is True
    assert SECRET not in _rendered_exception(caught.value)


def test_rollback_to_builtin_only_uses_memory_off_and_verifies_stopped_gateway(
    monkeypatch, tmp_path,
):
    target = _target(tmp_path, None)
    boundary = _install_boundary(
        monkeypatch,
        target,
        failures={"hermes memory status": [5]},
    )

    with pytest.raises(hermes.HermesInstallError):
        hermes.activate_and_verify(target)

    assert "hermes memory off" in boundary.events
    assert _read_provider(target.home) is None
    assert boundary.gateway_running is False


def test_rollback_failure_names_all_failed_stages_and_primary_stage(
    monkeypatch, tmp_path,
):
    target = _target(tmp_path, "legacy")
    boundary = _install_boundary(
        monkeypatch,
        target,
        gateway_running=True,
        failures={
            "hermes memory status": [9],
            "hermes config set memory.provider legacy": [8],
            "hermes gateway start": [7],
        },
    )

    with pytest.raises(hermes.HermesInstallError) as caught:
        hermes.activate_and_verify(target)

    message = str(caught.value)
    assert "Hermes memory status" in message
    assert "provider restore" in message
    assert "gateway restore" in message
    assert SECRET not in _rendered_exception(caught.value)
    assert boundary.events.count("hermes gateway start") >= 1


def test_failed_transaction_preserves_all_memoryd_artifacts(
    monkeypatch, tmp_path,
):
    target = _target(tmp_path, "legacy")
    evidence = {
        target.home / "spool" / "memoryd" / "incoming" / "job.json": b"profile spool",
        tmp_path / "memory" / "spool" / "incoming" / "job.json": b"daemon spool",
        tmp_path / "memory" / "archive" / "memory.jsonl": b"archive",
        tmp_path / "memory" / "backups" / "old" / "manifest.json": b"backup",
        tmp_path / "memory" / "logs" / "daemon.log": b"log",
    }
    for path, payload in evidence.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
    before = {path: path.read_bytes() for path in evidence}
    _install_boundary(
        monkeypatch,
        target,
        failures={"hermes memory status": [RuntimeError(SECRET)]},
    )

    with pytest.raises(hermes.HermesInstallError):
        hermes.activate_and_verify(target)

    assert {path: path.read_bytes() for path in evidence} == before
    assert _read_provider(target.home) == "legacy"


class GuidedSignalBoundary:
    def __init__(self, *, signal_during_restore: int | None = None) -> None:
        self.previous = {
            int(signal.SIGINT): object(),
            int(signal.SIGTERM): object(),
        }
        self.current = dict(self.previous)
        self.signal_during_restore = signal_during_restore
        self.restore_signal_delivered = False

    def getsignal(self, signum: int) -> object:
        return self.current[int(signum)]

    def set_signal(self, signum: int, handler: object) -> object:
        numeric = int(signum)
        old = self.current[numeric]
        if (
            handler is self.previous[numeric]
            and self.signal_during_restore == numeric
            and not self.restore_signal_delivered
        ):
            self.restore_signal_delivered = True
            assert callable(old)
            old(signum, None)
        self.current[numeric] = handler
        return old

    def deliver(self, signum: int) -> None:
        handler = self.current[int(signum)]
        assert callable(handler)
        handler(signum, None)


def _prepare_guided_activation(
    monkeypatch: pytest.MonkeyPatch,
    target: HermesTarget,
    artifact: Path,
) -> GuidedSignalBoundary:
    credentials = hermes.ProviderCredentials("openrouter-key", "voyage-key")
    monkeypatch.setattr(hermes, "require_guided_environment", lambda: None)
    monkeypatch.setattr(hermes, "resolve_guided_hermes_target", lambda: target)
    monkeypatch.setattr(hermes, "validate_hermes_compatibility", lambda *_args: None)
    monkeypatch.setattr(hermes.cli, "_resource_dir", lambda _name: target.home)
    monkeypatch.setattr(
        hermes, "resolve_guided_memory_home", lambda: artifact.parent,
    )
    monkeypatch.setattr(hermes, "classify_memory_home", lambda _home: "managed")
    monkeypatch.setattr(hermes, "confirm_operator", lambda: None)
    monkeypatch.setattr(
        hermes, "collect_provider_credentials", lambda _config: credentials,
    )
    monkeypatch.setattr(hermes, "validate_provider_credentials", lambda _value: None)
    monkeypatch.setattr(
        hermes, "install_hermes_core", lambda _target, _credentials: artifact,
    )
    signals = GuidedSignalBoundary()
    monkeypatch.setattr(hermes.signal, "getsignal", signals.getsignal)
    monkeypatch.setattr(hermes.signal, "signal", signals.set_signal)
    return signals


def _guided_outcome() -> tuple[int | None, BaseException | None]:
    try:
        return hermes.guided_hermes_install(), None
    except BaseException as error:
        return None, error


def test_signal_during_post_activation_report_rolls_back_provider_and_gateway(
    monkeypatch, tmp_path, capsys,
):
    target = _target(tmp_path, "legacy")
    _spool_root(target)
    boundary = _install_boundary(monkeypatch, target, gateway_running=True)
    artifact = tmp_path / "memory" / "initial.snapshot"
    artifact.parent.mkdir()
    artifact.write_bytes(b"preserved installation evidence")
    signals = _prepare_guided_activation(monkeypatch, target, artifact)
    real_print = builtins.print
    delivered = False

    def interrupt_success_report(*args, **kwargs):
        nonlocal delivered
        if not delivered and isinstance(kwargs.get("file"), io.StringIO):
            delivered = True
            signals.deliver(signal.SIGTERM)
        return real_print(*args, **kwargs)

    monkeypatch.setattr(builtins, "print", interrupt_success_report)

    result, escaped = _guided_outcome()

    assert escaped is None
    assert result == 143
    assert delivered
    assert _read_provider(target.home) == "legacy"
    assert boundary.gateway_running is True
    assert artifact.read_bytes() == b"preserved installation evidence"
    assert signals.current == signals.previous
    output = capsys.readouterr()
    assert "Authoritative Hermes profile" not in output.out
    assert "14-day/200-turn canary" not in output.out


def test_signal_during_failure_reporting_cannot_escape_or_leak_handlers(
    monkeypatch, tmp_path, capsys,
):
    target = _target(tmp_path, "legacy")
    boundary = _install_boundary(
        monkeypatch,
        target,
        gateway_running=True,
        failures={"hermes memory status": [7]},
    )
    artifact = tmp_path / "memory" / "initial.snapshot"
    artifact.parent.mkdir()
    artifact.write_bytes(b"preserved installation evidence")
    signals = _prepare_guided_activation(monkeypatch, target, artifact)
    real_print = builtins.print
    delivered = False

    def interrupt_failure_report(*args, **kwargs):
        nonlocal delivered
        if not delivered and kwargs.get("file") is sys.stderr:
            delivered = True
            signals.deliver(signal.SIGTERM)
        return real_print(*args, **kwargs)

    monkeypatch.setattr(builtins, "print", interrupt_failure_report)

    result, escaped = _guided_outcome()

    assert escaped is None
    assert result == 143
    assert delivered
    assert _read_provider(target.home) == "legacy"
    assert boundary.gateway_running is True
    assert artifact.read_bytes() == b"preserved installation evidence"
    assert signals.current == signals.previous
    output = capsys.readouterr()
    assert "Authoritative Hermes profile" not in output.out
    assert "14-day/200-turn canary" not in output.out


@pytest.mark.parametrize("signum", [signal.SIGINT, signal.SIGTERM])
def test_signal_during_each_handler_restore_rolls_back_and_restores_both(
    monkeypatch, tmp_path, capsys, signum,
):
    target = _target(tmp_path, "legacy")
    _spool_root(target)
    boundary = _install_boundary(monkeypatch, target, gateway_running=True)
    artifact = tmp_path / "memory" / "initial.snapshot"
    artifact.parent.mkdir()
    artifact.write_bytes(b"preserved installation evidence")
    signals = _prepare_guided_activation(monkeypatch, target, artifact)
    signals.signal_during_restore = int(signum)

    result, escaped = _guided_outcome()

    assert escaped is None
    assert result == 128 + int(signum)
    assert signals.restore_signal_delivered
    assert _read_provider(target.home) == "legacy"
    assert boundary.gateway_running is True
    assert artifact.read_bytes() == b"preserved installation evidence"
    assert signals.current == signals.previous
    output = capsys.readouterr()
    assert "Authoritative Hermes profile" not in output.out
    assert "14-day/200-turn canary" not in output.out


def test_failure_report_oserror_preserves_result_and_always_restores_handlers(
    monkeypatch, tmp_path, capsys,
):
    target = _target(tmp_path, "legacy")
    boundary = _install_boundary(
        monkeypatch,
        target,
        gateway_running=True,
        failures={"hermes memory status": [7]},
    )
    artifact = tmp_path / "memory" / "initial.snapshot"
    artifact.parent.mkdir()
    artifact.write_bytes(b"preserved installation evidence")
    signals = _prepare_guided_activation(monkeypatch, target, artifact)
    real_print = builtins.print

    def fail_stderr_report(*args, **kwargs):
        if kwargs.get("file") is sys.stderr:
            raise OSError(SECRET)
        return real_print(*args, **kwargs)

    monkeypatch.setattr(builtins, "print", fail_stderr_report)

    result, escaped = _guided_outcome()

    assert escaped is None
    assert result == 1
    assert _read_provider(target.home) == "legacy"
    assert boundary.gateway_running is True
    assert artifact.read_bytes() == b"preserved installation evidence"
    assert signals.current == signals.previous
    output = capsys.readouterr()
    assert SECRET not in output.out + output.err


def test_sigint_during_committed_stdout_flush_propagates_without_rollback(
    monkeypatch, tmp_path, capsys,
):
    target = _target(tmp_path, "legacy")
    _spool_root(target)
    boundary = _install_boundary(monkeypatch, target, gateway_running=True)
    artifact = tmp_path / "memory" / "initial.snapshot"
    artifact.parent.mkdir()
    artifact.write_bytes(b"preserved installation evidence")
    signals = _prepare_guided_activation(monkeypatch, target, artifact)
    signals.previous[int(signal.SIGINT)] = signal.default_int_handler
    signals.current[int(signal.SIGINT)] = signal.default_int_handler
    real_print = builtins.print
    delivered = False

    def interrupt_final_stdout(*args, **kwargs):
        nonlocal delivered
        result = real_print(*args, **kwargs)
        if (
            not delivered
            and kwargs.get("file") is None
            and kwargs.get("end") == ""
        ):
            delivered = True
            signals.deliver(signal.SIGINT)
        return result

    monkeypatch.setattr(builtins, "print", interrupt_final_stdout)

    result, escaped = _guided_outcome()

    assert result is None
    assert isinstance(escaped, KeyboardInterrupt)
    assert delivered
    assert _read_provider(target.home) == "memoryd"
    assert boundary.gateway_running is True
    assert artifact.read_bytes() == b"preserved installation evidence"
    assert signals.current == signals.previous
    output = capsys.readouterr()
    assert "Authoritative Hermes profile" in output.out
    assert "14-day/200-turn canary" in output.out
