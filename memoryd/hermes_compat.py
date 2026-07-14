from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shlex
import shutil
import stat
import subprocess
import tempfile
import unicodedata

from memoryd.hermes_validation.resources import (
    canonical_migrations_source,
    require_canonical_plugin_source,
)


PINNED_HERMES_VERSION = "0.16.0"
PINNED_HERMES_TAG = "v2026.6.5"
PINNED_HERMES_COMMIT = "3c231eb3979ab9c57d5cd6d02f1d577a3b718b43"

_PROFILE_NAME = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")
_CONSOLE_ENTRY_POINT = "hermes_cli.main:main"
_CONSOLE_BODY = """# -*- coding: utf-8 -*-
import sys
from hermes_cli.main import main
if __name__ == "__main__":
    if sys.argv[0].endswith("-script.pyw"):
        sys.argv[0] = sys.argv[0][:-11]
    elif sys.argv[0].endswith(".exe"):
        sys.argv[0] = sys.argv[0][:-4]
    sys.exit(main())
"""
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
    selector: Path | None = None


class HermesCompatibilityError(RuntimeError):
    pass


def _error(message: str) -> HermesCompatibilityError:
    return HermesCompatibilityError(f"{message}. Remediation: {_REMEDIATION}")


def _canonical_absolute(path: Path, description: str) -> Path:
    if any(
        unicodedata.category(character) in ("Cc", "Zl", "Zp")
        for character in os.fspath(path)
    ):
        raise _error(
            f"{description} must not contain control or line-separator characters"
        )
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


def _effective_uid() -> int:
    getter = getattr(os, "geteuid", None)
    return int(getter()) if getter is not None else 0


def _stable_path_stat(path: Path, description: str) -> os.stat_result:
    try:
        before = path.lstat()
        if stat.S_ISLNK(before.st_mode) or path.resolve(strict=True) != path:
            raise _error(f"{description} must have stable canonical topology")
        after = path.lstat()
    except HermesCompatibilityError:
        raise
    except (OSError, RuntimeError):
        raise _error(f"Could not inspect {description}") from None
    if (before.st_dev, before.st_ino, before.st_mode) != (
        after.st_dev, after.st_ino, after.st_mode,
    ):
        raise _error(f"{description} changed during inspection")
    if after.st_uid != _effective_uid():
        raise _error(f"{description} must be owned by the effective user")
    return after


def _validate_private_directory(path: Path, description: str) -> None:
    try:
        value = _stable_path_stat(path, description)
    except HermesCompatibilityError as error:
        if not path.exists():
            raise _error(f"{description} does not exist as a directory") from None
        raise error
    if not stat.S_ISDIR(value.st_mode):
        raise _error(f"{description} does not exist as a directory")
    if stat.S_IMODE(value.st_mode) != 0o700:
        if description == "Hermes root":
            remediation = shlex.join(["chmod", "700", "--", os.fspath(path)])
            raise _error(
                f"Hermes root must have mode 0700. Run: {remediation}"
            )
        raise _error(f"{description} must have mode 0700")


def _validate_owned_directory(path: Path, description: str) -> None:
    """Accept Hermes-owned descendants protected by the private root."""
    try:
        value = _stable_path_stat(path, description)
    except HermesCompatibilityError as error:
        if not path.exists():
            raise _error(f"{description} does not exist as a directory") from None
        raise error
    if not stat.S_ISDIR(value.st_mode):
        raise _error(f"{description} does not exist as a directory")
    if stat.S_IMODE(value.st_mode) & 0o022:
        raise _error(f"{description} must not be group- or other-writable")


def _validate_selected_home(path: Path) -> None:
    _validate_owned_directory(path, "Selected Hermes profile")


def _read_active_profile(marker: Path) -> str:
    try:
        value = marker.lstat()
    except FileNotFoundError:
        return ""
    except OSError:
        raise _error("Could not inspect the Hermes active_profile marker") from None
    if stat.S_ISLNK(value.st_mode):
        raise _error("Hermes active_profile marker must not be a symlink")
    stable = _stable_path_stat(marker, "Hermes active_profile marker")
    if not stat.S_ISREG(stable.st_mode):
        raise _error("Hermes active_profile marker must be a regular file")
    if stat.S_IMODE(stable.st_mode) & 0o022:
        raise _error(
            "Hermes active_profile marker must not be group- or other-writable"
        )
    try:
        return marker.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        raise _error("Could not read the Hermes active_profile marker") from None


def _resolve_hermes_home(
    environ: Mapping[str, str] | None = None,
) -> tuple[Path, Path, Path]:
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
        _validate_private_directory(root, "Hermes root")
        _validate_owned_directory(candidate.parent, "Hermes profiles directory")
        _validate_selected_home(candidate)
        return root, candidate, candidate

    root = candidate
    if root.exists() and not root.is_dir():
        raise _error(f"Hermes root is not a directory: {root}")

    if root.exists():
        _validate_private_directory(root, "Hermes root")
    marker = root / "active_profile"
    active_profile = _read_active_profile(marker) if root.exists() else ""

    if active_profile in ("", "default"):
        if root.exists():
            _validate_selected_home(root)
        return root, root, candidate

    if _PROFILE_NAME.fullmatch(active_profile) is None:
        raise _error("Hermes active_profile contains an invalid profile name")

    _validate_owned_directory(root / "profiles", "Hermes profiles directory")
    home = _canonical_absolute(root / "profiles" / active_profile, "Hermes profile")
    _validate_selected_home(home)
    return root, home, candidate


def resolve_hermes_home(
    environ: Mapping[str, str] | None = None,
) -> tuple[Path, Path]:
    root, home, _selector = _resolve_hermes_home(environ)
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
    result: subprocess.CompletedProcess[str] | None = None
    failed = False
    try:
        result = subprocess.run(
            command, check=True, capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        failed = True
    if failed or result is None:
        raise _error("Could not query the installed hermes-agent version")
    return result.stdout.strip()


def _validate_console_origin(executable: Path, python: Path) -> None:
    query = (
        "import importlib.metadata as m,json,sysconfig;"
        "d=m.distribution('hermes-agent');"
        "e=[x for x in d.entry_points if x.group=='console_scripts' and x.name=='hermes'];"
        "print(json.dumps({'entry_point':e[0].value if len(e)==1 else None,"
        "'scripts':sysconfig.get_path('scripts')}))"
    )
    command = [os.fspath(python), "-c", query]
    payload: object = None
    failed = False
    try:
        result = subprocess.run(
            command,
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
        payload = json.loads(result.stdout)
    except (OSError, subprocess.SubprocessError, TypeError, ValueError, RecursionError):
        failed = True
    if failed:
        raise _error("Could not resolve the Hermes console entry point")
    if not isinstance(payload, dict) or payload.get("entry_point") != _CONSOLE_ENTRY_POINT:
        raise _error("The Hermes console entry point is not the pinned runtime entry point")
    scripts = payload.get("scripts")
    if not isinstance(scripts, str):
        raise _error("The Hermes console entry point origin is malformed")
    scripts_path = Path(scripts)
    if not scripts_path.is_absolute():
        raise _error("The Hermes console entry point origin is malformed")
    expected = _canonical_absolute(scripts_path / "hermes", "Hermes console entry point")
    try:
        expected = expected.resolve(strict=True)
    except (OSError, RuntimeError):
        raise _error("The Hermes console entry point does not exist") from None
    if expected != executable:
        raise _error("The hermes command is a shadow wrapper, not the runtime console entry point")
    try:
        content = executable.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        raise _error("Could not inspect the Hermes console entry point content") from None
    if content != f"#!{python}\n{_CONSOLE_BODY}":
        raise _error("The Hermes console entry point content is not the pinned safe wrapper")


def resolve_hermes_target(
    environ: Mapping[str, str] | None = None,
) -> HermesTarget:
    if platform.system() != "Linux":
        raise _error("Hermes compatibility is supported on Linux only")

    root, home, selector = _resolve_hermes_home(environ)
    executable = _resolve_command()
    python = _resolve_python(executable)
    version = _query_version(python)
    if version != PINNED_HERMES_VERSION:
        raise _error(
            "Detected hermes-agent version does not match the required version "
            f"{PINNED_HERMES_VERSION}"
        )
    _validate_selected_home(home)
    _validate_console_origin(executable, python)
    return HermesTarget(
        root=root,
        home=home,
        executable=executable,
        python=python,
        selector=selector,
    )


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
    result: subprocess.CompletedProcess[str] | None = None
    failed = False
    try:
        result = subprocess.run(
            command,
            cwd=cwd,
            env=dict(environment),
            check=False,
            capture_output=True,
            text=True,
            timeout=180,
        )
    except (OSError, subprocess.TimeoutExpired):
        failed = True
    if failed or result is None:
        raise _error(f"Hermes {stage} validation could not start")
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
