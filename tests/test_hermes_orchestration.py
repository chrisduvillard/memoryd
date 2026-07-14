from __future__ import annotations

import contextlib
import signal
from pathlib import Path
import pytest

from memoryd import hermes_install as hermes
import memoryd.hermes_compat as compat
from memoryd.hermes_compat import HermesCompatibilityError, HermesTarget


OPENROUTER_SECRET = "orchestration-openrouter-secret"
VOYAGE_SECRET = "orchestration-voyage-secret"


@pytest.fixture(autouse=True)
def _clean_guided_provider_routing(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in hermes._PROVIDER_ROUTING_ENV:
        monkeypatch.delenv(name, raising=False)


def _workflow(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    classification: str = "fresh",
    failures: dict[str, BaseException] | None = None,
) -> tuple[list[str], HermesTarget, Path, dict[int, object], dict[int, object]]:
    events: list[str] = []
    failures = {} if failures is None else failures
    target = HermesTarget(
        root=tmp_path / "hermes",
        home=tmp_path / "hermes" / "profiles" / "authoritative",
        executable=tmp_path / "bin" / "hermes",
        python=tmp_path / "venv" / "bin" / "python",
    )
    memory_home = tmp_path / "memory"
    plugin = tmp_path / "packaged-plugin"
    snapshot = memory_home / "backups" / "initial" / "manifest.json"
    credentials = hermes.ProviderCredentials(OPENROUTER_SECRET, VOYAGE_SECRET)

    def observe(stage: str) -> None:
        events.append(stage)
        failure = failures.get(stage)
        if failure is not None:
            raise failure

    def require_environment() -> None:
        observe("environment")

    def resolve_target() -> HermesTarget:
        observe("target")
        return target

    def resource_dir(name: str) -> Path:
        assert name == "hermes_plugin"
        observe("plugin")
        return plugin

    def validate_compatibility(
        actual_target: HermesTarget, actual_plugin: Path,
    ) -> None:
        assert actual_target == target
        assert actual_plugin == plugin
        observe("compatibility")

    def home() -> Path:
        observe("home")
        return memory_home

    def classify(actual_home: Path) -> str:
        assert actual_home == memory_home
        observe("classify")
        return classification

    def confirm() -> None:
        observe("confirm")

    def collect(config_path: Path) -> hermes.ProviderCredentials:
        assert config_path == memory_home / "config.json"
        observe("credentials")
        return credentials

    def validate_credentials(actual: hermes.ProviderCredentials) -> None:
        assert actual is credentials
        observe("provider validation")

    def install(
        actual_target: HermesTarget,
        actual_credentials: hermes.ProviderCredentials,
    ) -> Path:
        assert actual_target == target
        assert actual_credentials is credentials
        observe("core install")
        return snapshot

    @contextlib.contextmanager
    def activate(actual_target: HermesTarget):
        assert actual_target == target
        observe("activation")
        yield

    monkeypatch.setattr(hermes, "require_guided_environment", require_environment)
    monkeypatch.setattr(
        hermes, "resolve_guided_hermes_target", resolve_target, raising=False,
    )
    monkeypatch.setattr(hermes.cli, "_resource_dir", resource_dir)
    monkeypatch.setattr(
        hermes, "validate_hermes_compatibility", validate_compatibility, raising=False,
    )
    monkeypatch.setattr(hermes, "resolve_guided_memory_home", home, raising=False)
    monkeypatch.setattr(hermes, "classify_memory_home", classify)
    monkeypatch.setattr(hermes, "confirm_operator", confirm)
    monkeypatch.setattr(hermes, "collect_provider_credentials", collect)
    monkeypatch.setattr(hermes, "validate_provider_credentials", validate_credentials)
    monkeypatch.setattr(hermes, "install_hermes_core", install)
    monkeypatch.setattr(hermes, "_activation_transaction", activate)

    previous = {
        int(signal.SIGINT): object(),
        int(signal.SIGTERM): object(),
    }
    current = dict(previous)
    installed: dict[int, object] = {}

    def getsignal(signum: int) -> object:
        return current[int(signum)]

    def set_signal(signum: int, handler: object) -> object:
        numeric = int(signum)
        old = current[numeric]
        current[numeric] = handler
        if handler is not previous[numeric]:
            installed[numeric] = handler
        return old

    monkeypatch.setattr(signal, "getsignal", getsignal)
    monkeypatch.setattr(signal, "signal", set_signal)
    return events, target, snapshot, previous, current


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("MEMORYD_HOME", "/tmp/orchestration-home-SENSITIVE"),
        (
            "MEMORYD_DSN",
            "postgresql://user:SENSITIVE@remote.invalid/unrelated",
        ),
    ],
)
def test_ambient_memory_redirect_stops_before_target_or_plugin_mutation(
    monkeypatch, tmp_path, capsys, name, value,
):
    operator = tmp_path / "operator"
    operator.mkdir(mode=0o700)
    monkeypatch.setattr(
        hermes, "_operator_home_from_passwd", lambda: operator, raising=False,
    )
    monkeypatch.delenv("MEMORYD_HOME", raising=False)
    monkeypatch.delenv("MEMORYD_DSN", raising=False)
    monkeypatch.setenv(name, value)
    monkeypatch.setattr(hermes, "require_guided_environment", lambda: None)
    monkeypatch.setattr(
        hermes,
        "resolve_guided_hermes_target",
        lambda: pytest.fail("Hermes target inspection must not start"),
    )
    monkeypatch.setattr(
        hermes.cli,
        "_resource_dir",
        lambda _name: pytest.fail("plugin inspection must not start"),
    )
    monkeypatch.setattr(
        hermes,
        "install_hermes_core",
        lambda *_args: pytest.fail("target mutation must not start"),
    )

    assert hermes.guided_hermes_install() == 1

    output = capsys.readouterr()
    assert output.out == ""
    assert name in output.err
    assert value not in output.err
    assert "SENSITIVE" not in output.err


def test_missing_default_profile_stops_before_prompt_probe_or_target_mutation(
    monkeypatch, tmp_path, capsys,
):
    operator = tmp_path / "operator"
    operator.mkdir(mode=0o700)
    hermes_root = operator / ".hermes"
    memory_home = operator / "memory"
    monkeypatch.setattr(hermes, "require_guided_environment", lambda: None)
    monkeypatch.setattr(
        hermes, "_operator_home_from_passwd", lambda: operator, raising=False,
    )
    monkeypatch.setattr(compat.platform, "system", lambda: "Linux")
    monkeypatch.setattr(
        compat, "_resolve_command", lambda: tmp_path / "bin" / "hermes",
    )
    monkeypatch.setattr(
        compat, "_resolve_python", lambda _command: tmp_path / "venv" / "python",
    )
    monkeypatch.setattr(
        compat, "_query_version", lambda _python: compat.PINNED_HERMES_VERSION,
    )
    monkeypatch.delenv("HERMES_HOME", raising=False)
    monkeypatch.delenv("MEMORYD_HOME", raising=False)
    monkeypatch.delenv("MEMORYD_DSN", raising=False)

    for name in (
        "validate_hermes_compatibility",
        "classify_memory_home",
        "confirm_operator",
        "collect_provider_credentials",
        "validate_provider_credentials",
        "install_hermes_core",
    ):
        monkeypatch.setattr(
            hermes, name,
            lambda *_args, _name=name, **_kwargs: pytest.fail(
                f"{_name} must not run for a missing selected profile"
            ),
        )

    assert hermes.guided_hermes_install() == 1

    assert not hermes_root.exists()
    assert not memory_home.exists()
    output = capsys.readouterr()
    assert output.out == ""
    assert "profile" in output.err.lower()


@pytest.mark.parametrize(
    "override",
    [
        "MEMORYD_LLM_BASE",
        "MEMORYD_LLM_MODEL",
        "MEMORYD_MODEL_PROFILE",
        "MEMORYD_EMBED_BASE",
        "MEMORYD_EMBED_MODEL",
    ],
)
def test_hostile_ambient_provider_override_stops_before_prompt_probe_or_core(
    monkeypatch, tmp_path, capsys, override,
):
    sentinel = "HOSTILE-PROVIDER-OVERRIDE-SENTINEL"
    monkeypatch.setenv(override, f"https://{sentinel}.invalid/collect")
    events, _target, _snapshot, previous, current = _workflow(
        monkeypatch, tmp_path,
    )

    assert hermes.guided_hermes_install() == 1

    assert events == ["environment", "home", "target", "plugin", "compatibility", "classify"]
    output = capsys.readouterr()
    assert output.out == ""
    assert override in output.err
    assert sentinel not in output.err
    assert current == previous


@pytest.mark.parametrize("classification", ["fresh", "managed"])
def test_guided_install_composes_exact_order_and_reports_success_without_secrets(
    monkeypatch, tmp_path, capsys, classification,
):
    events, target, snapshot, previous, current = _workflow(
        monkeypatch, tmp_path, classification=classification,
    )

    result = hermes.guided_hermes_install()

    assert result == 0
    assert events == [
        "environment",
        "home",
        "target",
        "plugin",
        "compatibility",
        "classify",
        "confirm",
        "credentials",
        "provider validation",
        "core install",
        "activation",
    ]
    output = capsys.readouterr()
    assert output.err == ""
    assert f"Authoritative Hermes profile: {target.home}" in output.out
    assert "http://127.0.0.1:7437" in output.out
    assert f"Verified initial snapshot: {snapshot}" in output.out
    assert "Four healthy checks" in output.out
    assert "prior gateway state" in output.out
    assert "14-day/200-turn canary" in output.out
    assert OPENROUTER_SECRET not in output.out
    assert VOYAGE_SECRET not in output.out
    assert current == previous


@pytest.mark.parametrize(
    ("stage", "failure"),
    [
        ("environment", hermes.HermesInstallError("unsafe environment")),
        ("target", HermesCompatibilityError("unsupported Hermes target")),
        ("compatibility", HermesCompatibilityError("contract drift")),
        ("classify", hermes.HermesInstallError("unknown memory home")),
        ("confirm", hermes.HermesInstallError("operator cancelled")),
        ("credentials", hermes.HermesInstallError("credential input failed")),
        (
            "provider validation",
            hermes.HermesInstallError(
                f"provider validation rejected {OPENROUTER_SECRET} and {VOYAGE_SECRET}"
            ),
        ),
    ],
)
def test_each_pre_mutation_failure_stops_without_core_or_activation(
    monkeypatch, tmp_path, capsys, stage, failure,
):
    events, _target, _snapshot, previous, current = _workflow(
        monkeypatch, tmp_path, failures={stage: failure},
    )

    result = hermes.guided_hermes_install()

    assert result == 1
    assert "core install" not in events
    assert "activation" not in events
    output = capsys.readouterr()
    assert output.out == ""
    assert output.err.count("\n") == 1
    assert "Hermes guided installation failed:" in output.err
    assert OPENROUTER_SECRET not in output.err
    assert VOYAGE_SECRET not in output.err
    assert current == previous


def test_core_failure_is_sanitized_and_does_not_activate(
    monkeypatch, tmp_path, capsys,
):
    failure = hermes.HermesInstallError(
        f"core failed for {OPENROUTER_SECRET} and {VOYAGE_SECRET}"
    )
    events, _target, _snapshot, _previous, _current = _workflow(
        monkeypatch, tmp_path, failures={"core install": failure},
    )

    assert hermes.guided_hermes_install() == 1

    assert events[-1] == "core install"
    assert "activation" not in events
    output = capsys.readouterr()
    assert output.out == ""
    assert "core failed" in output.err
    assert OPENROUTER_SECRET not in output.err
    assert VOYAGE_SECRET not in output.err


def test_activation_failure_propagates_sanitized_rollback_evidence(
    monkeypatch, tmp_path, capsys,
):
    failure = hermes.HermesInstallError(
        "Hermes activation failed. Rollback incomplete at: gateway restore. "
        f"Credentials: {OPENROUTER_SECRET} {VOYAGE_SECRET}."
    )
    events, _target, _snapshot, _previous, _current = _workflow(
        monkeypatch, tmp_path, failures={"activation": failure},
    )

    assert hermes.guided_hermes_install() == 1

    assert events[-1] == "activation"
    output = capsys.readouterr()
    assert output.out == ""
    assert "Rollback incomplete at: gateway restore" in output.err
    assert OPENROUTER_SECRET not in output.err
    assert VOYAGE_SECRET not in output.err


@pytest.mark.parametrize("stage", ["environment", "core install", "activation"])
def test_keyboard_interrupt_returns_130_without_a_failure_traceback(
    monkeypatch, tmp_path, capsys, stage,
):
    events, _target, _snapshot, previous, current = _workflow(
        monkeypatch, tmp_path, failures={stage: KeyboardInterrupt()},
    )

    result = hermes.guided_hermes_install()

    assert result == 130
    if stage == "environment":
        assert "core install" not in events
    output = capsys.readouterr()
    assert output.out == ""
    assert output.err == "Hermes guided installation interrupted (SIGINT).\n"
    assert current == previous


def test_sigterm_during_activation_returns_143_after_activation_cleanup_and_restores_handlers(
    monkeypatch, tmp_path, capsys,
):
    events, target, _snapshot, previous, current = _workflow(monkeypatch, tmp_path)

    @contextlib.contextmanager
    def activation_with_cleanup(actual_target: HermesTarget):
        assert actual_target == target
        events.append("activation")
        try:
            handler = current[int(signal.SIGTERM)]
            assert callable(handler)
            handler(signal.SIGTERM, None)
            yield
        except BaseException:
            events.append("activation rollback")
            raise hermes.HermesInstallError("activation interrupted after rollback") from None

    monkeypatch.setattr(hermes, "_activation_transaction", activation_with_cleanup)

    result = hermes.guided_hermes_install()

    assert result == 143
    assert events[-2:] == ["activation", "activation rollback"]
    output = capsys.readouterr()
    assert output.out == ""
    assert output.err == "Hermes guided installation interrupted (SIGTERM).\n"
    assert current == previous


def test_programming_error_before_mutation_is_not_suppressed(
    monkeypatch, tmp_path, capsys,
):
    events, _target, _snapshot, previous, current = _workflow(
        monkeypatch,
        tmp_path,
        failures={"compatibility": RuntimeError("programming defect")},
    )

    with pytest.raises(RuntimeError, match="programming defect"):
        hermes.guided_hermes_install()

    assert "core install" not in events
    assert "activation" not in events
    assert capsys.readouterr() == ("", "")
    assert current == previous
