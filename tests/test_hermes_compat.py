from __future__ import annotations

from dataclasses import FrozenInstanceError
import json
import os
from pathlib import Path
import platform
import shlex
import shutil
import stat
import subprocess

import pytest

import memoryd.hermes_compat as compat
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


def _write_active_profile(root: Path, value: str, mode: int = 0o600) -> Path:
    marker = root / "active_profile"
    marker.write_text(value, encoding="utf-8")
    marker.chmod(mode)
    return marker


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


def test_missing_default_home_is_discoverable_but_not_a_guided_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    hermes_root = tmp_path / ".hermes"
    environment = {"HERMES_HOME": str(hermes_root)}
    assert resolve_hermes_home(environment) == (hermes_root, hermes_root)

    executable = tmp_path / "bin" / "hermes"
    python = tmp_path / "venv" / "bin" / "python"
    monkeypatch.setattr(compat, "_resolve_command", lambda: executable)
    monkeypatch.setattr(compat, "_resolve_python", lambda _command: python)
    monkeypatch.setattr(
        compat, "_query_version", lambda _python: PINNED_HERMES_VERSION,
    )

    with pytest.raises(HermesCompatibilityError, match="profile.*does not exist"):
        resolve_hermes_target(environment)

    assert not hermes_root.exists()


@pytest.mark.parametrize("active_profile", ["", "default"])
def test_empty_or_default_active_profile_selects_existing_root(
    tmp_path: Path, active_profile: str
) -> None:
    root = _mkdir(tmp_path / ".hermes")
    _write_active_profile(root, active_profile)

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
    _write_active_profile(root, "work_1")

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
    marker = _write_active_profile(root, active_profile)
    before = marker.read_bytes()

    with pytest.raises(HermesCompatibilityError) as exc_info:
        resolve_hermes_home({"HERMES_HOME": str(root)})

    _assert_safe_error(exc_info)
    assert marker.read_bytes() == before
    assert not (root / "profiles").exists()


def test_missing_named_profile_is_rejected_without_being_created(tmp_path: Path) -> None:
    root = _mkdir(tmp_path / ".hermes")
    missing = root / "profiles" / "missing"
    _write_active_profile(root, "missing")

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


@pytest.mark.parametrize(
    "suffix", [(), ("child",)], ids=["home", "ancestor"]
)
def test_symlink_loop_in_home_is_reported_as_compatibility_error(
    tmp_path: Path, suffix: tuple[str, ...]
) -> None:
    loop = tmp_path / ".hermes"
    loop.symlink_to(loop, target_is_directory=True)
    configured = loop.joinpath(*suffix)

    with pytest.raises(HermesCompatibilityError) as exc_info:
        resolve_hermes_home({"HERMES_HOME": str(configured)})

    _assert_safe_error(exc_info)


def test_symlinked_named_profile_is_rejected(tmp_path: Path) -> None:
    root = _mkdir(tmp_path / ".hermes")
    profiles = _mkdir(root / "profiles")
    real_profile = _mkdir(profiles / "real")
    linked_profile = profiles / "linked"
    linked_profile.symlink_to(real_profile, target_is_directory=True)
    _write_active_profile(root, "linked")

    with pytest.raises(HermesCompatibilityError) as exc_info:
        resolve_hermes_home({"HERMES_HOME": str(root)})

    _assert_safe_error(exc_info)
    assert linked_profile.is_symlink()


def test_named_profile_accepts_pinned_hermes_default_modes_inside_private_root(
    tmp_path: Path,
) -> None:
    root = _mkdir(tmp_path / ".hermes")
    profiles = _mkdir(root / "profiles", mode=0o755)
    profile = _mkdir(profiles / "work", mode=0o755)
    marker = _write_active_profile(root, "work\n", mode=0o644)

    assert resolve_hermes_home({"HERMES_HOME": str(root)}) == (root, profile)
    assert stat.S_IMODE(root.stat().st_mode) == 0o700
    assert stat.S_IMODE(profiles.stat().st_mode) == 0o755
    assert stat.S_IMODE(profile.stat().st_mode) == 0o755
    assert stat.S_IMODE(marker.stat().st_mode) == 0o644


@pytest.mark.parametrize("component", ["profiles", "profile", "marker"])
def test_named_profile_rejects_group_or_other_writable_descendants(
    tmp_path: Path, component: str,
) -> None:
    root = _mkdir(tmp_path / ".hermes")
    profiles = _mkdir(root / "profiles", mode=0o755)
    profile = _mkdir(profiles / "work", mode=0o755)
    marker = _write_active_profile(root, "work", mode=0o644)
    {"profiles": profiles, "profile": profile, "marker": marker}[component].chmod(0o777)

    with pytest.raises(HermesCompatibilityError, match="writable"):
        resolve_hermes_home({"HERMES_HOME": str(root)})


def test_selected_profile_owned_by_another_uid_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _mkdir(tmp_path / ".hermes")
    profile = _mkdir(_mkdir(root / "profiles") / "work")
    _write_active_profile(root, "work")
    monkeypatch.setattr(
        os, "geteuid", lambda: profile.stat().st_uid + 1, raising=False,
    )

    with pytest.raises(HermesCompatibilityError, match="owned"):
        resolve_hermes_home({"HERMES_HOME": str(root)})


def test_owned_descendant_validator_rejects_wrong_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = _mkdir(tmp_path / "work", mode=0o755)
    monkeypatch.setattr(
        compat, "_effective_uid", lambda: profile.stat().st_uid + 1,
    )

    with pytest.raises(HermesCompatibilityError, match="owned"):
        compat._validate_owned_directory(profile, "Selected Hermes profile")


@pytest.mark.parametrize("configured_form", ["root", "profile"])
def test_named_profile_root_requires_owner_only_mode_with_safe_remediation(
    tmp_path: Path, configured_form: str,
) -> None:
    root = _mkdir(tmp_path / "Hermes root's private")
    profiles = _mkdir(root / "profiles", mode=0o755)
    profile = _mkdir(profiles / "work", mode=0o755)
    _write_active_profile(root, "work", mode=0o644)
    root.chmod(0o755)
    configured = root if configured_form == "root" else profile

    with pytest.raises(HermesCompatibilityError) as exc_info:
        resolve_hermes_home({"HERMES_HOME": str(configured)})

    expected = shlex.join(["chmod", "700", "--", os.fspath(root)])
    assert expected in str(exc_info.value)
    assert os.fspath(profile) not in expected
    assert stat.S_IMODE(root.stat().st_mode) == 0o755


@pytest.mark.parametrize("control", ["\n", "\r", "\x1f", "\x7f"])
def test_hermes_home_rejects_control_characters_without_echoing_path(
    tmp_path: Path, control: str,
) -> None:
    configured = tmp_path / f"Hermes-{control}-SENSITIVE"

    with pytest.raises(HermesCompatibilityError, match="control") as exc_info:
        resolve_hermes_home({"HERMES_HOME": os.fspath(configured)})

    message = str(exc_info.value)
    assert "SENSITIVE" not in message
    assert control not in message
    assert "\n" not in message
    assert "\r" not in message


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


def test_symlink_loop_in_hermes_command_is_reported_as_compatibility_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    loop = tmp_path / "bin" / "hermes"
    loop.parent.mkdir()
    loop.symlink_to(loop)
    monkeypatch.setattr(shutil, "which", lambda command: str(loop))

    with pytest.raises(HermesCompatibilityError) as exc_info:
        resolve_hermes_target({"HERMES_HOME": str(tmp_path / ".hermes")})

    _assert_safe_error(exc_info)


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


def test_symlink_loop_in_python_interpreter_is_reported_as_compatibility_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    loop = tmp_path / "bin" / "python3.13"
    loop.parent.mkdir(parents=True)
    loop.symlink_to(loop)
    hermes = _write_executable(tmp_path / "bin" / "hermes", f"#!{loop}\n")
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


def test_python_interpreter_inaccessible_to_current_process_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    python = _python_interpreter(tmp_path)
    hermes = _write_executable(tmp_path / "bin" / "hermes", f"#!{python}\n")
    monkeypatch.setattr(shutil, "which", lambda command: str(hermes))
    monkeypatch.setattr(
        os, "access", lambda path, mode: Path(path) != python.resolve()
    )
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0], 0, stdout=f"{PINNED_HERMES_VERSION}\n", stderr=""
        ),
    )

    with pytest.raises(HermesCompatibilityError) as exc_info:
        resolve_hermes_target({"HERMES_HOME": str(tmp_path / ".hermes")})

    _assert_safe_error(exc_info)


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


def test_hermes_command_inaccessible_to_current_process_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    python = _python_interpreter(tmp_path)
    hermes = _write_executable(tmp_path / "bin" / "hermes", f"#!{python}\n")
    monkeypatch.setattr(shutil, "which", lambda command: str(hermes))
    monkeypatch.setattr(
        os, "access", lambda path, mode: Path(path) != hermes.resolve()
    )
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0], 0, stdout=f"{PINNED_HERMES_VERSION}\n", stderr=""
        ),
    )

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


def test_version_query_timeout_is_bounded_and_sanitized(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "VERSION-TIMEOUT-SECRET"
    python = _python_interpreter(tmp_path)
    hermes = _write_executable(tmp_path / "bin" / "hermes", f"#!{python}\n")
    monkeypatch.setattr(shutil, "which", lambda command: str(hermes))

    def timeout(command: object, **kwargs: object):
        assert kwargs["timeout"] <= 30
        raise subprocess.TimeoutExpired(
            command, kwargs["timeout"], output=secret, stderr=secret,
        )

    monkeypatch.setattr(subprocess, "run", timeout)

    with pytest.raises(HermesCompatibilityError) as exc_info:
        resolve_hermes_target({"HERMES_HOME": str(tmp_path / ".hermes")})

    rendered = repr(exc_info.value)
    assert secret not in rendered
    assert exc_info.value.__cause__ is None
    assert exc_info.value.__context__ is None


def test_compatibility_stage_timeout_is_bounded_and_sanitized(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "VALIDATION-TIMEOUT-SECRET"
    target = HermesTarget(
        root=tmp_path / "root",
        home=tmp_path / "profile",
        executable=tmp_path / "hermes",
        python=tmp_path / "python",
    )
    monkeypatch.setattr(
        compat, "_canonical_plugin_source", lambda path: path,
    )
    monkeypatch.setattr(
        compat, "_stage_memoryd_package",
        lambda import_root, package_root, plugin: (import_root / "memoryd", {}),
    )
    monkeypatch.setattr(compat, "_staged_package_matches", lambda *_args: True)

    def timeout(command: object, **kwargs: object):
        assert kwargs["timeout"] <= 180
        raise subprocess.TimeoutExpired(
            command, kwargs["timeout"], output=secret, stderr=secret,
        )

    monkeypatch.setattr(subprocess, "run", timeout)

    with pytest.raises(HermesCompatibilityError) as exc_info:
        compat.validate_hermes_compatibility(target, tmp_path / "plugin")

    rendered = repr(exc_info.value)
    assert secret not in rendered
    assert exc_info.value.__cause__ is None
    assert exc_info.value.__context__ is None


def test_version_mismatch_reports_pin_and_safe_remediation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sentinel = "sensitive-subprocess-output"
    python = _python_interpreter(tmp_path)
    hermes = _write_executable(tmp_path / "bin" / "hermes", f"#!{python}\n")
    monkeypatch.setattr(shutil, "which", lambda command: str(hermes))
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0], 0, stdout=f"0.15.0\n{sentinel}\n", stderr=sentinel
        ),
    )

    with pytest.raises(HermesCompatibilityError) as exc_info:
        resolve_hermes_target({"HERMES_HOME": str(tmp_path / ".hermes")})

    message = _assert_safe_error(exc_info)
    assert "detected hermes-agent version does not match" in message.lower()
    assert PINNED_HERMES_VERSION in message
    assert sentinel not in message


def test_shadow_hermes_wrapper_with_pinned_shebang_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _mkdir(tmp_path / ".hermes")
    python = _python_interpreter(tmp_path)
    shadow = _write_executable(
        tmp_path / "shadow-bin" / "hermes",
        f"#!{python}\nfrom hermes_cli.main import main\nmain()\n",
    )
    monkeypatch.setattr(shutil, "which", lambda _command: str(shadow))
    monkeypatch.setattr(
        subprocess, "run",
        lambda command, **kwargs: subprocess.CompletedProcess(
            command, 0, stdout=f"{PINNED_HERMES_VERSION}\n", stderr="",
        ),
    )

    with pytest.raises(HermesCompatibilityError, match="entry|origin|console"):
        resolve_hermes_target({"HERMES_HOME": str(root)})


def test_console_origin_rejects_tampered_script_at_runtime_entry_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    python = _python_interpreter(tmp_path)
    console = _write_executable(
        python.parent / "hermes",
        f"#!{python}\nfrom hermes_cli.main import main\nmain()\n",
    )
    monkeypatch.setattr(
        subprocess, "run",
        lambda command, **kwargs: subprocess.CompletedProcess(
            command,
            0,
            stdout=(
                '{"entry_point":"hermes_cli.main:main","scripts":'
                + json.dumps(str(python.parent))
                + "}\n"
            ),
            stderr="",
        ),
    )

    with pytest.raises(HermesCompatibilityError, match="content|console|entry"):
        compat._validate_console_origin(console, python)


def test_success_resolves_real_command_interpreter_profile_and_pinned_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _mkdir(tmp_path / ".hermes")
    profile = _mkdir(_mkdir(root / "profiles") / "work")
    _write_active_profile(root, "work")
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
    origin_calls: list[tuple[Path, Path]] = []
    monkeypatch.setattr(
        compat, "_validate_console_origin",
        lambda executable, interpreter: origin_calls.append(
            (executable, interpreter)
        ),
    )

    target = resolve_hermes_target({"HERMES_HOME": str(root)})

    assert target == HermesTarget(
        root=root,
        home=profile,
        executable=real_hermes.resolve(),
        python=python.resolve(),
        selector=root,
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
    assert kwargs["timeout"] <= 30
    assert origin_calls == [(real_hermes.resolve(), python.resolve())]


def test_symlinked_venv_python_preserves_environment_launcher(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _mkdir(tmp_path / ".hermes")
    base_python = _write_executable(tmp_path / "runtime" / "python3.11", "")
    venv_python = tmp_path / "pipx" / "venv" / "bin" / "python"
    venv_python.parent.mkdir(parents=True)
    venv_python.symlink_to(base_python)
    hermes = _write_executable(tmp_path / "bin" / "hermes", f"#!{venv_python}\n")
    commands: list[list[str]] = []
    monkeypatch.setattr(shutil, "which", lambda _command: str(hermes))

    def run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        return subprocess.CompletedProcess(
            command, 0, stdout=f"{PINNED_HERMES_VERSION}\n", stderr=""
        )

    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(compat, "_validate_console_origin", lambda *_args: None)

    target = resolve_hermes_target({"HERMES_HOME": str(tmp_path / ".hermes")})

    assert target.python == venv_python.absolute()
    assert commands[0][0] == os.fspath(venv_python.absolute())
