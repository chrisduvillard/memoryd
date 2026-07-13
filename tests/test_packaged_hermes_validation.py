from __future__ import annotations

import hashlib
import importlib.resources
import importlib.util
import os
from pathlib import Path
import shutil
import subprocess

import pytest

import memoryd
import memoryd.hermes_compat as hermes_compat
from memoryd.hermes_compat import (
    PINNED_HERMES_COMMIT,
    PINNED_HERMES_TAG,
    PINNED_HERMES_VERSION,
    HermesCompatibilityError,
    HermesTarget,
    validate_hermes_compatibility,
)
from memoryd.hermes_validation import contract, installed_runtime
from memoryd.hermes_validation import resources


REPO = Path(__file__).resolve().parents[1]
PINNED_SHA256 = "597210754e83a0eab2c522c233d87cb2dbad6d2b423c6bccd07cf6162072c5bd"
SENSITIVE_OUTPUT = "validator-secret-output"


def _canonical_plugin() -> Path:
    package_plugin = Path(memoryd.__file__).resolve().parent / "hermes_plugin"
    if package_plugin.is_dir():
        return package_plugin
    return REPO / "hermes_plugin" / "memoryd"


def _target(tmp_path: Path) -> HermesTarget:
    root = tmp_path / "authoritative-hermes"
    home = root / "profiles" / "work"
    home.mkdir(parents=True)
    (home / "keep").write_text("authoritative", encoding="utf-8")
    return HermesTarget(
        root=root,
        home=home,
        executable=tmp_path / "pipx" / "bin" / "hermes",
        python=tmp_path / "pipx" / "venv" / "bin" / "python",
    )


def _source_layout(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "source-checkout"
    package = root / "memoryd"
    shutil.copytree(REPO / "memoryd", package)
    shutil.copytree(REPO / "hermes_plugin" / "memoryd", root / "hermes_plugin" / "memoryd")
    shutil.copytree(REPO / "migrations", root / "migrations")
    (root / "scripts").mkdir()
    for name in ("check_hermes_contract.py", "validate_installed_hermes.py"):
        shutil.copy2(REPO / "scripts" / name, root / "scripts" / name)
    shutil.copy2(REPO / "pyproject.toml", root / "pyproject.toml")
    adjacent_agent = root / "agent"
    adjacent_agent.mkdir()
    (adjacent_agent / "memory_provider.py").write_text(
        "raise RuntimeError('adjacent agent leaked')\n", encoding="utf-8"
    )
    adjacent_distribution = root / "hermes_agent-0.16.0.dist-info"
    adjacent_distribution.mkdir()
    (adjacent_distribution / "METADATA").write_text(
        "Name: hermes-agent\nVersion: 0.16.0\n", encoding="utf-8"
    )
    cache = package / "__pycache__"
    cache.mkdir(exist_ok=True)
    (cache / "sentinel.cpython-311.pyc").write_bytes(b"not bytecode")
    return package, root / "hermes_plugin" / "memoryd"


def _successful_run(
    calls: list[tuple[list[str], dict[str, object]]],
):
    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, stdout="ok\n", stderr="")

    return run


def _load_wrapper(relative: str, name: str):
    path = REPO / relative
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_packaged_pin_resource_is_exact_and_immutable() -> None:
    resource = importlib.resources.files("memoryd.hermes_validation").joinpath(
        "agent", "memory_provider.py"
    )
    pinned_bytes = resource.read_bytes()

    assert contract.PINNED_TAG == PINNED_HERMES_TAG == "v2026.6.5"
    assert (
        contract.PINNED_COMMIT
        == PINNED_HERMES_COMMIT
        == "3c231eb3979ab9c57d5cd6d02f1d577a3b718b43"
    )
    assert len(pinned_bytes) == 12297
    assert hashlib.sha256(pinned_bytes).hexdigest() == PINNED_SHA256
    assert pinned_bytes == (
        REPO / "scripts" / "_stubs" / "agent" / "memory_provider.py"
    ).read_bytes()


def test_arbitrary_plugin_source_is_rejected_before_child_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = _target(tmp_path)
    arbitrary = tmp_path / "untrusted-plugin"
    arbitrary.mkdir()
    (arbitrary / "__init__.py").write_text("# untrusted\n", encoding="utf-8")
    calls: list[object] = []
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: calls.append(args))

    with pytest.raises(HermesCompatibilityError, match="bundled memoryd plugin"):
        validate_hermes_compatibility(target, arbitrary)

    assert calls == []
    assert (target.home / "keep").read_text(encoding="utf-8") == "authoritative"


def test_validation_uses_exact_target_interpreter_commands_and_isolated_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = _target(tmp_path)
    calls: list[tuple[list[str], dict[str, object]]] = []
    monkeypatch.setenv("HERMES_HOME", str(target.home))
    monkeypatch.setenv("MEMORYD_HOME", str(tmp_path / "real-memoryd-home"))
    monkeypatch.setenv("MEMORYD_SECRET", SENSITIVE_OUTPUT)
    monkeypatch.setattr(subprocess, "run", _successful_run(calls))

    validate_hermes_compatibility(target, _canonical_plugin())

    assert len(calls) == 2
    contract_command, contract_kwargs = calls[0]
    lifecycle_command, lifecycle_kwargs = calls[1]
    expected_prefix = [os.fspath(target.python), "-P", "-m"]
    assert contract_command == [
        *expected_prefix,
        "memoryd.hermes_validation.contract",
        "--require-pinned-bytes",
    ]
    isolated_home = Path(lifecycle_command[lifecycle_command.index("--hermes-home") + 1])
    private_import_root = Path(contract_kwargs["env"]["PYTHONPATH"])
    staged_plugin = private_import_root / "memoryd" / "hermes_plugin"
    assert lifecycle_command == [
        *expected_prefix,
        "memoryd.hermes_validation.installed_runtime",
        "--hermes-home",
        os.fspath(isolated_home),
        "--plugin-source",
        os.fspath(staged_plugin),
        "--expected-version",
        PINNED_HERMES_VERSION,
    ]
    assert isolated_home != target.home
    assert not isolated_home.exists()
    for kwargs in (contract_kwargs, lifecycle_kwargs):
        assert kwargs["check"] is False
        assert kwargs["capture_output"] is True
        assert kwargs["text"] is True
        env = kwargs["env"]
        assert isinstance(env, dict)
        assert Path(env["PYTHONPATH"]).name == "import-root"
        assert Path(env["PYTHONPATH"]).parent == isolated_home.parent
        assert Path(env["PYTHONPATH"]) != Path(memoryd.__file__).resolve().parent.parent
        assert env["PYTHONNOUSERSITE"] == "1"
        assert env["HERMES_HOME"] == os.fspath(isolated_home)
        assert Path(env["MEMORYD_HOME"]).parent == isolated_home.parent
        assert env.get("MEMORYD_SECRET") is None
        assert Path(kwargs["cwd"]) == isolated_home.parent
    assert contract_kwargs["env"] == lifecycle_kwargs["env"]
    assert (target.home / "keep").read_text(encoding="utf-8") == "authoritative"


def test_private_import_root_excludes_adjacent_agent_and_distribution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    package, plugin = _source_layout(tmp_path)
    monkeypatch.setattr(hermes_compat, "__file__", str(package / "hermes_compat.py"))
    target = _target(tmp_path)
    observations: list[dict[str, object]] = []

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        environment = kwargs["env"]
        assert isinstance(environment, dict)
        import_root = Path(environment["PYTHONPATH"])
        staged = import_root / "memoryd"
        observations.append({
            "entries": sorted(path.name for path in import_root.iterdir()),
            "agent": (import_root / "agent").exists(),
            "distribution": any(import_root.glob("hermes_agent-*.dist-info")),
            "pycache": any(staged.rglob("__pycache__")),
            "pyc": any(staged.rglob("*.pyc")),
            "plugin": os.fspath(staged / "hermes_plugin"),
            "command": command,
        })
        return subprocess.CompletedProcess(command, 0, stdout="ok\n", stderr="")

    monkeypatch.setattr(subprocess, "run", run)

    validate_hermes_compatibility(target, plugin)

    assert len(observations) == 2
    assert all(item["entries"] == ["memoryd"] for item in observations)
    assert all(item["agent"] is False for item in observations)
    assert all(item["distribution"] is False for item in observations)
    assert all(item["pycache"] is False and item["pyc"] is False for item in observations)
    lifecycle_command = observations[1]["command"]
    assert isinstance(lifecycle_command, list)
    assert lifecycle_command[lifecycle_command.index("--plugin-source") + 1] == observations[1]["plugin"]


def test_damaged_wheel_cannot_use_adjacent_fake_plugin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    site_packages = tmp_path / "venv" / "lib" / "python3.11" / "site-packages"
    package = site_packages / "memoryd"
    package.mkdir(parents=True)
    damaged_module = package / "hermes_compat.py"
    damaged_module.write_text("# damaged installed package\n", encoding="utf-8")
    adjacent = site_packages / "hermes_plugin" / "memoryd"
    adjacent.mkdir(parents=True)
    (adjacent / "__init__.py").write_text("# adjacent fake\n", encoding="utf-8")
    monkeypatch.setattr(hermes_compat, "__file__", str(damaged_module))
    calls: list[object] = []
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: calls.append(args))

    with pytest.raises(HermesCompatibilityError, match="bundled memoryd plugin"):
        validate_hermes_compatibility(_target(tmp_path), adjacent)

    assert calls == []


def test_verified_editable_checkout_can_use_canonical_source_plugin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    package, plugin = _source_layout(tmp_path)

    class EditableDistribution:
        def locate_file(self, _name: str) -> Path:
            return package

        def read_text(self, name: str) -> str | None:
            if name == "direct_url.json":
                return '{"dir_info": {"editable": true}}'
            return None

    monkeypatch.setattr(
        resources.metadata, "distribution", lambda _name: EditableDistribution()
    )

    assert resources.canonical_plugin_source(package) == plugin.resolve()


@pytest.mark.parametrize("plugin_name", ["missing", "invalid-file"])
def test_missing_or_invalid_plugin_is_always_a_compatibility_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, plugin_name: str
) -> None:
    package = tmp_path / "damaged" / "memoryd"
    package.mkdir(parents=True)
    module = package / "hermes_compat.py"
    module.write_text("# damaged package\n", encoding="utf-8")
    monkeypatch.setattr(hermes_compat, "__file__", str(module))
    plugin = tmp_path / plugin_name
    if plugin_name == "invalid-file":
        plugin.write_text("not a directory\n", encoding="utf-8")

    with pytest.raises(HermesCompatibilityError, match="bundled memoryd plugin"):
        validate_hermes_compatibility(_target(tmp_path), plugin)


def test_staged_package_tampering_after_lifecycle_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = _target(tmp_path)
    calls = 0

    def tamper(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        nonlocal calls
        calls += 1
        if calls == 2:
            environment = kwargs["env"]
            assert isinstance(environment, dict)
            import_root = Path(environment["PYTHONPATH"])
            if import_root.name == "import-root":
                (import_root / "memoryd" / "hermes_validation" / "contract.py").write_text(
                    "tampered = True\n", encoding="utf-8"
                )
        return subprocess.CompletedProcess(command, 0, stdout="ok\n", stderr="")

    monkeypatch.setattr(subprocess, "run", tamper)

    with pytest.raises(
        HermesCompatibilityError, match="(?i)staged memoryd package changed"
    ):
        validate_hermes_compatibility(target, _canonical_plugin())

    assert calls == 2


def test_staged_package_manifest_errors_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def unreadable_manifest(_root: Path) -> dict[str, str]:
        raise OSError("sensitive staged path")

    monkeypatch.setattr(hermes_compat, "_package_manifest", unreadable_manifest)

    assert not hermes_compat._staged_package_matches(tmp_path, {})


def test_contract_stage_failure_is_sanitized_and_stops_lifecycle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = _target(tmp_path)
    calls: list[list[str]] = []

    def fail(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        return subprocess.CompletedProcess(
            command, 17, stdout=SENSITIVE_OUTPUT, stderr=SENSITIVE_OUTPUT
        )

    monkeypatch.setattr(subprocess, "run", fail)

    with pytest.raises(HermesCompatibilityError) as exc_info:
        validate_hermes_compatibility(target, _canonical_plugin())

    message = str(exc_info.value)
    assert "contract validation failed" in message.lower()
    assert "exit code 17" in message.lower()
    assert SENSITIVE_OUTPUT not in message
    assert len(calls) == 1
    assert (target.home / "keep").read_text(encoding="utf-8") == "authoritative"


def test_lifecycle_stage_failure_is_sanitized(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = _target(tmp_path)
    calls: list[list[str]] = []

    def run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        return subprocess.CompletedProcess(
            command,
            0 if len(calls) == 1 else 23,
            stdout=SENSITIVE_OUTPUT,
            stderr=SENSITIVE_OUTPUT,
        )

    monkeypatch.setattr(subprocess, "run", run)

    with pytest.raises(HermesCompatibilityError) as exc_info:
        validate_hermes_compatibility(target, _canonical_plugin())

    message = str(exc_info.value)
    assert "lifecycle validation failed" in message.lower()
    assert "exit code 23" in message.lower()
    assert SENSITIVE_OUTPUT not in message
    assert len(calls) == 2
    assert (target.home / "keep").read_text(encoding="utf-8") == "authoritative"


def test_subprocess_launch_failure_is_sanitized(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = _target(tmp_path)

    def fail(*_args: object, **_kwargs: object) -> None:
        raise OSError(SENSITIVE_OUTPUT)

    monkeypatch.setattr(subprocess, "run", fail)

    with pytest.raises(HermesCompatibilityError) as exc_info:
        validate_hermes_compatibility(target, _canonical_plugin())

    message = str(exc_info.value)
    assert "contract validation could not start" in message.lower()
    assert SENSITIVE_OUTPUT not in message
    assert (target.home / "keep").read_text(encoding="utf-8") == "authoritative"


def test_script_wrappers_reexport_packaged_implementations() -> None:
    contract_wrapper = _load_wrapper(
        "scripts/check_hermes_contract.py", "memoryd_contract_wrapper"
    )
    lifecycle_wrapper = _load_wrapper(
        "scripts/validate_installed_hermes.py", "memoryd_lifecycle_wrapper"
    )

    assert contract_wrapper.check_contract is contract.check_contract
    assert contract_wrapper.main is contract.main
    assert (
        lifecycle_wrapper.validate_installed_runtime
        is installed_runtime.validate_installed_runtime
    )
    assert lifecycle_wrapper.prepare_isolated_home is installed_runtime.prepare_isolated_home
    assert lifecycle_wrapper.main is installed_runtime.main
    assert len((REPO / "scripts" / "check_hermes_contract.py").read_text().splitlines()) < 25
    assert len((REPO / "scripts" / "validate_installed_hermes.py").read_text().splitlines()) < 25


def test_success_returns_none_and_preserves_authoritative_homes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = _target(tmp_path)
    memoryd_home = tmp_path / "authoritative-memoryd"
    memoryd_home.mkdir()
    marker = memoryd_home / "keep"
    marker.write_text("authoritative", encoding="utf-8")
    monkeypatch.setenv("MEMORYD_HOME", str(memoryd_home))
    calls: list[tuple[list[str], dict[str, object]]] = []
    monkeypatch.setattr(subprocess, "run", _successful_run(calls))

    result = validate_hermes_compatibility(target, _canonical_plugin())

    assert result is None
    assert len(calls) == 2
    assert marker.read_text(encoding="utf-8") == "authoritative"
    assert (target.home / "keep").read_text(encoding="utf-8") == "authoritative"
