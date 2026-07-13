from __future__ import annotations

from dataclasses import FrozenInstanceError
import os
from pathlib import Path
import platform
import shutil
import stat
import subprocess

import pytest

from memoryd.hermes_compat import (
    PINNED_HERMES_COMMIT,
    PINNED_HERMES_TAG,
    PINNED_HERMES_VERSION,
    HermesCompatibilityError,
    HermesTarget,
    resolve_hermes_home,
    resolve_hermes_target,
)


REMEDIATION = (
    "pipx install --force --python python3.13 "
    "'git+https://github.com/NousResearch/hermes-agent.git@"
    "3c231eb3979ab9c57d5cd6d02f1d577a3b718b43'"
)


@pytest.fixture(autouse=True)
def _linux(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(platform, "system", lambda: "Linux")


def _mkdir(path: Path, mode: int = 0o700) -> Path:
    path.mkdir(parents=True)
    path.chmod(mode)
    return path


def _write_executable(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)
    return path


def _python_interpreter(tmp_path: Path, *, executable: bool = True) -> Path:
    python = tmp_path / "bin" / "python3.13"
    python.parent.mkdir(parents=True, exist_ok=True)
    python.write_text("", encoding="utf-8")
    python.chmod(0o755 if executable else 0o600)
    return python


def _assert_safe_error(exc_info: pytest.ExceptionInfo[HermesCompatibilityError]) -> str:
    message = str(exc_info.value)
    assert REMEDIATION in message
    return message


def test_pinned_interface_and_frozen_target() -> None:
    assert PINNED_HERMES_VERSION == "0.16.0"
    assert PINNED_HERMES_TAG == "v2026.6.5"
    assert PINNED_HERMES_COMMIT == "3c231eb3979ab9c57d5cd6d02f1d577a3b718b43"

    target = HermesTarget(
        root=Path("/root"),
        home=Path("/home"),
        executable=Path("/bin/hermes"),
        python=Path("/bin/python3.13"),
    )
    with pytest.raises(FrozenInstanceError):
        target.home = Path("/other")  # type: ignore[misc]


def test_default_home_selects_root_without_creating_it(tmp_path: Path) -> None:
    user_home = _mkdir(tmp_path / "user")
    hermes_root = user_home / ".hermes"

    root, home = resolve_hermes_home({"HOME": str(user_home)})

    assert (root, home) == (hermes_root, hermes_root)
    assert root.is_absolute()
    assert not hermes_root.exists()


@pytest.mark.parametrize("active_profile", ["", "default"])
def test_empty_or_default_active_profile_selects_existing_root(
    tmp_path: Path, active_profile: str
) -> None:
    root = _mkdir(tmp_path / ".hermes")
    (root / "active_profile").write_text(active_profile, encoding="utf-8")

    resolved_root, home = resolve_hermes_home({"HERMES_HOME": str(root)})

    assert (resolved_root, home) == (root, root)
    assert stat.S_IMODE(root.stat().st_mode) == 0o700


def test_explicit_profile_is_selected_without_reading_root_marker(tmp_path: Path) -> None:
    root = _mkdir(tmp_path / ".hermes")
    profiles = _mkdir(root / "profiles")
    profile = _mkdir(profiles / "work")
    (root / "active_profile").write_text("../must-not-be-read", encoding="utf-8")

    resolved_root, home = resolve_hermes_home({"HERMES_HOME": str(profile)})

    assert (resolved_root, home) == (root, profile)


def test_valid_named_profile_is_selected(tmp_path: Path) -> None:
    root = _mkdir(tmp_path / ".hermes")
    profile = _mkdir(_mkdir(root / "profiles") / "work_1")
    (root / "active_profile").write_text("work_1", encoding="utf-8")

    resolved_root, home = resolve_hermes_home({"HERMES_HOME": str(root)})

    assert (resolved_root, home) == (root, profile)


@pytest.mark.parametrize(
    "active_profile",
    ["../escape", "nested/profile", "work\nother", "UPPERCASE"],
)
def test_invalid_multiline_or_traversing_profile_is_rejected_without_mutation(
    tmp_path: Path, active_profile: str
) -> None:
    root = _mkdir(tmp_path / ".hermes")
    marker = root / "active_profile"
    marker.write_text(active_profile, encoding="utf-8")
    before = marker.read_bytes()

    with pytest.raises(HermesCompatibilityError) as exc_info:
        resolve_hermes_home({"HERMES_HOME": str(root)})

    _assert_safe_error(exc_info)
    assert marker.read_bytes() == before
    assert not (root / "profiles").exists()


def test_missing_named_profile_is_rejected_without_being_created(tmp_path: Path) -> None:
    root = _mkdir(tmp_path / ".hermes")
    missing = root / "profiles" / "missing"
    (root / "active_profile").write_text("missing", encoding="utf-8")

    with pytest.raises(HermesCompatibilityError) as exc_info:
        resolve_hermes_home({"HERMES_HOME": str(root)})

    _assert_safe_error(exc_info)
    assert not missing.exists()


def test_relative_hermes_home_is_rejected_without_creation(tmp_path: Path) -> None:
    relative = f"relative-{tmp_path.name}"

    with pytest.raises(HermesCompatibilityError) as exc_info:
        resolve_hermes_home({"HERMES_HOME": relative})

    _assert_safe_error(exc_info)
    assert not Path(relative).exists()


def test_lexically_ambiguous_absolute_home_is_rejected(tmp_path: Path) -> None:
    root = _mkdir(tmp_path / ".hermes")
    _mkdir(tmp_path / "detour")
    ambiguous = tmp_path / "detour" / ".." / root.name
    assert ambiguous.is_absolute()
    assert ambiguous.resolve() == root

    with pytest.raises(HermesCompatibilityError) as exc_info:
        resolve_hermes_home({"HERMES_HOME": str(ambiguous)})

    _assert_safe_error(exc_info)


def test_symlinked_root_is_rejected(tmp_path: Path) -> None:
    real_root = _mkdir(tmp_path / "real-hermes")
    linked_root = tmp_path / ".hermes"
    linked_root.symlink_to(real_root, target_is_directory=True)

    with pytest.raises(HermesCompatibilityError) as exc_info:
        resolve_hermes_home({"HERMES_HOME": str(linked_root)})

    _assert_safe_error(exc_info)
    assert linked_root.is_symlink()


def test_symlinked_named_profile_is_rejected(tmp_path: Path) -> None:
    root = _mkdir(tmp_path / ".hermes")
    profiles = _mkdir(root / "profiles")
    real_profile = _mkdir(profiles / "real")
    linked_profile = profiles / "linked"
    linked_profile.symlink_to(real_profile, target_is_directory=True)
    (root / "active_profile").write_text("linked", encoding="utf-8")

    with pytest.raises(HermesCompatibilityError) as exc_info:
        resolve_hermes_home({"HERMES_HOME": str(root)})

    _assert_safe_error(exc_info)
    assert linked_profile.is_symlink()


def test_profile_mode_mismatch_is_rejected_without_chmod(tmp_path: Path) -> None:
    root = _mkdir(tmp_path / ".hermes")
    profile = _mkdir(_mkdir(root / "profiles") / "work", mode=0o755)
    (root / "active_profile").write_text("work", encoding="utf-8")

    with pytest.raises(HermesCompatibilityError) as exc_info:
        resolve_hermes_home({"HERMES_HOME": str(root)})

    _assert_safe_error(exc_info)
    assert stat.S_IMODE(profile.stat().st_mode) == 0o755


def test_non_linux_platform_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(platform, "system", lambda: "Darwin")

    with pytest.raises(HermesCompatibilityError) as exc_info:
        resolve_hermes_target({"HERMES_HOME": str(Path.cwd())})

    _assert_safe_error(exc_info)


def test_missing_hermes_command_is_rejected_without_creating_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / ".hermes"
    monkeypatch.setattr(shutil, "which", lambda command: None)

    with pytest.raises(HermesCompatibilityError) as exc_info:
        resolve_hermes_target({"HERMES_HOME": str(root)})

    _assert_safe_error(exc_info)
    assert not root.exists()


@pytest.mark.parametrize(
    "first_line",
    ["print('no shebang')", "#!/usr/bin/env python3"],
)
def test_malformed_or_relative_python_shebang_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, first_line: str
) -> None:
    hermes = _write_executable(tmp_path / "bin" / "hermes", first_line + "\n")
    monkeypatch.setattr(shutil, "which", lambda command: str(hermes))

    with pytest.raises(HermesCompatibilityError) as exc_info:
        resolve_hermes_target({"HERMES_HOME": str(tmp_path / ".hermes")})

    _assert_safe_error(exc_info)


def test_absolute_non_python_shebang_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    shell = _write_executable(tmp_path / "bin" / "bash", "")
    hermes = _write_executable(tmp_path / "bin" / "hermes", f"#!{shell}\n")
    monkeypatch.setattr(shutil, "which", lambda command: str(hermes))

    with pytest.raises(HermesCompatibilityError) as exc_info:
        resolve_hermes_target({"HERMES_HOME": str(tmp_path / ".hermes")})

    _assert_safe_error(exc_info)


def test_missing_shebang_interpreter_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    missing_python = tmp_path / "bin" / "python3.13"
    hermes = _write_executable(tmp_path / "bin" / "hermes", f"#!{missing_python}\n")
    monkeypatch.setattr(shutil, "which", lambda command: str(hermes))

    with pytest.raises(HermesCompatibilityError) as exc_info:
        resolve_hermes_target({"HERMES_HOME": str(tmp_path / ".hermes")})

    _assert_safe_error(exc_info)


def test_non_executable_shebang_interpreter_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    python = _python_interpreter(tmp_path, executable=False)
    hermes = _write_executable(tmp_path / "bin" / "hermes", f"#!{python}\n")
    monkeypatch.setattr(shutil, "which", lambda command: str(hermes))

    with pytest.raises(HermesCompatibilityError) as exc_info:
        resolve_hermes_target({"HERMES_HOME": str(tmp_path / ".hermes")})

    _assert_safe_error(exc_info)
    assert stat.S_IMODE(python.stat().st_mode) == 0o600


def test_non_executable_hermes_command_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    python = _python_interpreter(tmp_path)
    hermes = tmp_path / "bin" / "hermes"
    hermes.write_text(f"#!{python}\n", encoding="utf-8")
    hermes.chmod(0o600)
    monkeypatch.setattr(shutil, "which", lambda command: str(hermes))

    with pytest.raises(HermesCompatibilityError) as exc_info:
        resolve_hermes_target({"HERMES_HOME": str(tmp_path / ".hermes")})

    _assert_safe_error(exc_info)


def test_version_query_subprocess_failure_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    python = _python_interpreter(tmp_path)
    hermes = _write_executable(tmp_path / "bin" / "hermes", f"#!{python}\n")
    monkeypatch.setattr(shutil, "which", lambda command: str(hermes))

    def fail(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        raise subprocess.CalledProcessError(1, args[0], stderr="query failed")

    monkeypatch.setattr(subprocess, "run", fail)

    with pytest.raises(HermesCompatibilityError) as exc_info:
        resolve_hermes_target({"HERMES_HOME": str(tmp_path / ".hermes")})

    _assert_safe_error(exc_info)


def test_version_mismatch_reports_pin_and_safe_remediation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    python = _python_interpreter(tmp_path)
    hermes = _write_executable(tmp_path / "bin" / "hermes", f"#!{python}\n")
    monkeypatch.setattr(shutil, "which", lambda command: str(hermes))
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0], 0, stdout="0.15.0\n", stderr=""
        ),
    )

    with pytest.raises(HermesCompatibilityError) as exc_info:
        resolve_hermes_target({"HERMES_HOME": str(tmp_path / ".hermes")})

    message = _assert_safe_error(exc_info)
    assert "0.15.0" in message
    assert PINNED_HERMES_VERSION in message


def test_success_resolves_real_command_interpreter_profile_and_pinned_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _mkdir(tmp_path / ".hermes")
    profile = _mkdir(_mkdir(root / "profiles") / "work")
    (root / "active_profile").write_text("work", encoding="utf-8")
    python = _python_interpreter(tmp_path)
    real_hermes = _write_executable(
        tmp_path / "bin" / "hermes-real", f"#!{python}\nprint('hermes')\n"
    )
    hermes_link = tmp_path / "bin" / "hermes"
    hermes_link.symlink_to(real_hermes)
    which_calls: list[str] = []
    run_calls: list[tuple[object, dict[str, object]]] = []

    def which(command: str) -> str:
        which_calls.append(command)
        return str(hermes_link)

    def run(command: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        run_calls.append((command, kwargs))
        return subprocess.CompletedProcess(
            command, 0, stdout=f"{PINNED_HERMES_VERSION}\n", stderr=""
        )

    monkeypatch.setattr(shutil, "which", which)
    monkeypatch.setattr(subprocess, "run", run)

    target = resolve_hermes_target({"HERMES_HOME": str(root)})

    assert target == HermesTarget(
        root=root,
        home=profile,
        executable=real_hermes.resolve(),
        python=python.resolve(),
    )
    assert which_calls == ["hermes"]
    assert len(run_calls) == 1
    command, kwargs = run_calls[0]
    assert isinstance(command, (list, tuple))
    assert command[0] == os.fspath(python.resolve())
    assert command[1] == "-c"
    assert "importlib.metadata" in command[2]
    assert "hermes-agent" in command[2]
    assert kwargs["check"] is True
    assert kwargs["capture_output"] is True
    assert kwargs["text"] is True
