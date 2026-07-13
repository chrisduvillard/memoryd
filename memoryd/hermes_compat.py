from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import platform
import re
import shutil
import stat
import subprocess
import tempfile

from memoryd.hermes_validation.resources import (
    canonical_migrations_source,
    require_canonical_plugin_source,
)


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
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        try:
            mode = current.lstat().st_mode
        except FileNotFoundError:
            break
        except OSError as exc:
            raise _error(f"Could not inspect {description}") from exc
        if stat.S_ISLNK(mode):
            raise _error(
                f"{description} must not contain symlinks or ambiguous components"
            )
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
        resolved = interpreter.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise _error("The hermes Python interpreter does not exist") from exc
    if not _is_executable(resolved):
        raise _error("The hermes Python interpreter is not executable")
    # Keep the venv launcher path: resolving its normal ``bin/python`` symlink
    # would execute the base interpreter without the Hermes environment.
    return interpreter


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


def _memoryd_package_root() -> Path:
    return Path(__file__).resolve().parent


def _canonical_plugin_source(plugin_source: Path) -> Path:
    try:
        return require_canonical_plugin_source(plugin_source, _memoryd_package_root())
    except (OSError, RuntimeError, ValueError):
        raise _error("Plugin source must be the bundled memoryd plugin") from None


def _package_manifest(root: Path) -> dict[str, str]:
    manifest: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if "__pycache__" in relative.parts or path.suffix == ".pyc":
            continue
        if path.is_symlink():
            raise ValueError("memoryd package resources must not contain symlinks")
        if path.is_file():
            manifest[relative.as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return manifest


def _prefixed_manifest(prefix: str, root: Path) -> dict[str, str]:
    return {
        f"{prefix}/{relative}": digest
        for relative, digest in _package_manifest(root).items()
    }


def _staged_package_matches(root: Path, expected: Mapping[str, str]) -> bool:
    try:
        return _package_manifest(root) == expected
    except (OSError, RuntimeError, ValueError):
        return False


def _stage_memoryd_package(
    import_root: Path, package_root: Path, plugin_source: Path
) -> tuple[Path, dict[str, str]]:
    migrations_source = canonical_migrations_source(package_root)
    expected = _package_manifest(package_root)
    packaged_plugin = package_root / "hermes_plugin"
    packaged_migrations = package_root / "migrations"
    if not packaged_plugin.is_dir():
        expected.update(_prefixed_manifest("hermes_plugin", plugin_source))
    if not packaged_migrations.is_dir():
        expected.update(_prefixed_manifest("migrations", migrations_source))

    import_root.mkdir(mode=0o700)
    staged = import_root / "memoryd"
    shutil.copytree(
        package_root,
        staged,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    if not packaged_plugin.is_dir():
        shutil.copytree(
            plugin_source,
            staged / "hermes_plugin",
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
        )
    if not packaged_migrations.is_dir():
        shutil.copytree(
            migrations_source,
            staged / "migrations",
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
        )
    if _package_manifest(staged) != expected:
        raise ValueError("staged memoryd package differs from its verified source")
    return staged, expected


def _validation_environment(
    root: Path, hermes_home: Path, import_root: Path
) -> dict[str, str]:
    environment = {
        "HOME": os.fspath(root / "home"),
        "HERMES_HOME": os.fspath(hermes_home),
        "MEMORYD_HOME": os.fspath(root / "memoryd-home"),
        "PYTHONNOUSERSITE": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONPATH": os.fspath(import_root),
        "TEMP": os.fspath(root / "tmp"),
        "TMP": os.fspath(root / "tmp"),
        "TMPDIR": os.fspath(root / "tmp"),
        "USERPROFILE": os.fspath(root / "home"),
    }
    for name in ("SYSTEMROOT", "WINDIR"):
        if name in os.environ:
            environment[name] = os.environ[name]
    return environment


def _run_validation_stage(
    target: HermesTarget,
    stage: str,
    module: str,
    arguments: list[str],
    *,
    cwd: Path,
    environment: Mapping[str, str],
) -> None:
    command = [os.fspath(target.python), "-P", "-m", module, *arguments]
    try:
        result = subprocess.run(
            command,
            cwd=cwd,
            env=dict(environment),
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError:
        raise _error(f"Hermes {stage} validation could not start") from None
    if result.returncode != 0:
        raise _error(
            f"Hermes {stage} validation failed (exit code {result.returncode})"
        )


def validate_hermes_compatibility(
    target: HermesTarget, plugin_source: Path
) -> None:
    """Run pinned contract and isolated installed-runtime lifecycle validation."""
    canonical_plugin = _canonical_plugin_source(plugin_source)
    with tempfile.TemporaryDirectory(prefix="memoryd-hermes-validation-") as temporary:
        root = Path(temporary).resolve()
        (root / "home").mkdir(mode=0o700)
        (root / "tmp").mkdir(mode=0o700)
        hermes_home = root / "hermes-home"
        try:
            staged_package, expected_manifest = _stage_memoryd_package(
                root / "import-root", _memoryd_package_root(), canonical_plugin
            )
        except (OSError, RuntimeError, ValueError):
            raise _error("Could not stage the bundled memoryd package") from None
        environment = _validation_environment(root, hermes_home, staged_package.parent)
        plugin_argument = os.fspath(staged_package / "hermes_plugin")
        if not _staged_package_matches(staged_package, expected_manifest):
            raise _error("Staged memoryd package changed before Hermes validation")
        _run_validation_stage(
            target,
            "contract",
            "memoryd.hermes_validation.contract",
            ["--require-pinned-bytes"],
            cwd=root,
            environment=environment,
        )
        if not _staged_package_matches(staged_package, expected_manifest):
            raise _error("Staged memoryd package changed during Hermes validation")
        _run_validation_stage(
            target,
            "lifecycle",
            "memoryd.hermes_validation.installed_runtime",
            [
                "--hermes-home",
                os.fspath(hermes_home),
                "--plugin-source",
                plugin_argument,
                "--expected-version",
                PINNED_HERMES_VERSION,
            ],
            cwd=root,
            environment=environment,
        )
        if not _staged_package_matches(staged_package, expected_manifest):
            raise _error("Staged memoryd package changed during Hermes validation")
