from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import os
from pathlib import Path
import platform
import re
import shutil
import stat
import subprocess


PINNED_HERMES_VERSION = "0.16.0"
PINNED_HERMES_TAG = "v2026.6.5"
PINNED_HERMES_COMMIT = "3c231eb3979ab9c57d5cd6d02f1d577a3b718b43"

_PROFILE_NAME = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")
_REMEDIATION = (
    "pipx install --force --python python3.13 "
    "'git+https://github.com/NousResearch/hermes-agent.git@"
    f"{PINNED_HERMES_COMMIT}'"
)


@dataclass(frozen=True)
class HermesTarget:
    root: Path
    home: Path
    executable: Path
    python: Path


class HermesCompatibilityError(RuntimeError):
    pass


def _error(message: str) -> HermesCompatibilityError:
    return HermesCompatibilityError(f"{message}. Remediation: {_REMEDIATION}")


def _canonical_absolute(path: Path, description: str) -> Path:
    if not path.is_absolute():
        raise _error(f"{description} must be absolute")
    try:
        resolved = path.resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        raise _error(f"Could not resolve {description}") from exc
    if resolved != path:
        raise _error(f"{description} must not contain symlinks or ambiguous components")
    return path


def _validate_selected_home(path: Path) -> None:
    if not path.is_dir():
        raise _error(f"Selected Hermes profile does not exist as a directory: {path}")
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
    except OSError as exc:
        raise _error(f"Could not inspect selected Hermes profile: {path}") from exc
    if mode != 0o700:
        raise _error(f"Selected Hermes profile must have mode 0700: {path}")


def resolve_hermes_home(
    environ: Mapping[str, str] | None = None,
) -> tuple[Path, Path]:
    env = os.environ if environ is None else environ
    configured = env.get("HERMES_HOME")
    if configured is None:
        user_home = Path(env["HOME"]) if "HOME" in env else Path.home()
        candidate = user_home / ".hermes"
    else:
        candidate = Path(configured)

    candidate = _canonical_absolute(candidate, "Hermes home")
    if candidate.parent.name == "profiles":
        root = _canonical_absolute(candidate.parent.parent, "Hermes root")
        _validate_selected_home(candidate)
        return root, candidate

    root = candidate
    if root.exists() and not root.is_dir():
        raise _error(f"Hermes root is not a directory: {root}")

    marker = root / "active_profile"
    try:
        if marker.is_symlink():
            raise _error("Hermes active_profile marker must not be a symlink")
        active_profile = marker.read_text(encoding="utf-8") if marker.exists() else ""
    except HermesCompatibilityError:
        raise
    except (OSError, UnicodeError) as exc:
        raise _error("Could not read the Hermes active_profile marker") from exc

    if active_profile in ("", "default"):
        if root.exists():
            _validate_selected_home(root)
        return root, root

    if _PROFILE_NAME.fullmatch(active_profile) is None:
        raise _error("Hermes active_profile contains an invalid profile name")

    home = _canonical_absolute(root / "profiles" / active_profile, "Hermes profile")
    _validate_selected_home(home)
    return root, home


def _is_executable(path: Path) -> bool:
    try:
        mode = path.stat().st_mode
    except OSError:
        return False
    return stat.S_ISREG(mode) and os.access(path, os.X_OK)


def _resolve_command() -> Path:
    command = shutil.which("hermes")
    if command is None:
        raise _error("The hermes command was not found")
    try:
        executable = Path(command).resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise _error("Could not resolve the hermes command") from exc
    if not _is_executable(executable):
        raise _error("The hermes command is not executable")
    return executable


def _resolve_python(executable: Path) -> Path:
    try:
        with executable.open("r", encoding="utf-8") as script:
            first_line = script.readline().rstrip("\r\n")
    except (OSError, UnicodeError) as exc:
        raise _error("Could not read the hermes command shebang") from exc

    if not first_line.startswith("#!"):
        raise _error("The hermes command has no Python shebang")
    interpreter_text = first_line[2:]
    if not interpreter_text or any(character.isspace() for character in interpreter_text):
        raise _error("The hermes command has a malformed Python shebang")

    interpreter = Path(interpreter_text)
    if not interpreter.is_absolute() or not interpreter.name.lower().startswith("python"):
        raise _error("The hermes command must use an absolute Python shebang")
    try:
        python = interpreter.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise _error("The hermes Python interpreter does not exist") from exc
    if not _is_executable(python):
        raise _error("The hermes Python interpreter is not executable")
    return python


def _query_version(python: Path) -> str:
    command = [
        os.fspath(python),
        "-c",
        "import importlib.metadata; print(importlib.metadata.version('hermes-agent'))",
    ]
    try:
        result = subprocess.run(command, check=True, capture_output=True, text=True)
    except (OSError, subprocess.SubprocessError) as exc:
        raise _error("Could not query the installed hermes-agent version") from exc
    return result.stdout.strip()


def resolve_hermes_target(
    environ: Mapping[str, str] | None = None,
) -> HermesTarget:
    if platform.system() != "Linux":
        raise _error("Hermes compatibility is supported on Linux only")

    root, home = resolve_hermes_home(environ)
    executable = _resolve_command()
    python = _resolve_python(executable)
    version = _query_version(python)
    if version != PINNED_HERMES_VERSION:
        raise _error(
            "Detected hermes-agent version does not match the required version "
            f"{PINNED_HERMES_VERSION}"
        )
    return HermesTarget(root=root, home=home, executable=executable, python=python)
