from __future__ import annotations

import hashlib
import os
from pathlib import Path
import subprocess

import pytest

import memoryd
from memoryd.hermes_compat import (
    PINNED_HERMES_VERSION,
    HermesTarget,
    resolve_hermes_target,
    validate_hermes_compatibility,
)


def _tree_manifest(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _target_python() -> Path:
    configured = os.environ.get("HERMES_TARGET_PYTHON")
    if not configured:
        pytest.skip("HERMES_TARGET_PYTHON is required for cross-environment validation")
    target = Path(configured).absolute()
    assert target.is_file()
    return target


@pytest.mark.parametrize("root_marker", ["other", "../invalid-profile"])
def test_packaged_preflight_runs_against_distinct_pinned_explicit_profile(
    tmp_path: Path, root_marker: str,
) -> None:
    target_python = _target_python()
    probe = subprocess.run(
        [
            os.fspath(target_python),
            "-P",
            "-c",
            "import importlib.metadata as m; "
            "assert m.version('hermes-agent') == '0.16.0'; "
            "assert m.packages_distributions().get('memoryd') is None",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert probe.returncode == 0, probe.stdout + probe.stderr

    root = tmp_path / "authoritative-hermes"
    root.mkdir(mode=0o700)
    root.chmod(0o700)
    profile_environment = dict(os.environ)
    profile_environment["HERMES_HOME"] = os.fspath(root)
    profile_environment["HOME"] = os.fspath(tmp_path)
    created = subprocess.run(
        [
            os.fspath(target_python),
            "-P",
            "-c",
            "import os; os.umask(0o022); "
            "from hermes_cli.profiles import create_profile,set_active_profile; "
            "create_profile('work', no_alias=True, no_skills=True); "
            "set_active_profile('work')",
        ],
        env=profile_environment,
        check=False,
        capture_output=True,
        text=True,
    )
    assert created.returncode == 0, created.stdout + created.stderr
    home = root / "profiles" / "work"
    assert (root / "profiles").stat().st_mode & 0o777 == 0o755
    assert home.stat().st_mode & 0o777 == 0o755
    assert (root / "active_profile").stat().st_mode & 0o777 == 0o644
    other = root / "profiles" / "other"
    other.mkdir(mode=0o755)
    (other / "must-not-change").write_bytes(b"other profile evidence")
    (root / "active_profile").write_text(root_marker, encoding="utf-8")
    os.chmod(root / "active_profile", 0o644)
    (home / "sentinel.json").write_text(
        '{"authoritative": true}\n', encoding="utf-8"
    )
    before = _tree_manifest(root)
    executable = target_python.parent / "hermes"
    previous_path = os.environ.get("PATH", "")
    os.environ["PATH"] = os.pathsep.join((os.fspath(target_python.parent), previous_path))
    try:
        resolved = resolve_hermes_target({"HERMES_HOME": os.fspath(home)})
    finally:
        os.environ["PATH"] = previous_path
    assert resolved == HermesTarget(
        root=root,
        home=home,
        executable=executable.resolve(),
        python=target_python,
        selector=home,
    )
    package = Path(memoryd.__file__).resolve().parent
    plugin = package / "hermes_plugin"
    assert plugin.is_dir(), "integration must run from an installed memoryd wheel"

    validate_hermes_compatibility(
        HermesTarget(
            root=root,
            home=home,
            executable=executable,
            python=target_python,
        ),
        plugin,
    )

    assert _tree_manifest(root) == before
    assert PINNED_HERMES_VERSION == "0.16.0"
