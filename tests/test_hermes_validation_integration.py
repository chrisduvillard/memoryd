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


def test_packaged_preflight_runs_against_distinct_pinned_hermes(
    tmp_path: Path,
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
    home = root / "profiles" / "work"
    home.mkdir(parents=True, mode=0o700)
    home.chmod(0o700)
    (root / "active_profile").write_text("work", encoding="utf-8")
    (home / "sentinel.json").write_text(
        '{"authoritative": true}\n', encoding="utf-8"
    )
    before = _tree_manifest(root)
    executable = target_python.parent / "hermes"
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
