from __future__ import annotations

import json
import os
import subprocess
import traceback
from dataclasses import FrozenInstanceError
from pathlib import Path
from types import SimpleNamespace

import pytest

from memoryd import hermes_install as hermes
from memoryd.hermes_compat import HermesTarget


SECRET = "CHILD-CONFIG-SECRET-SENTINEL"


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
