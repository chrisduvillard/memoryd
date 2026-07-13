"""Resolve validator resources without trusting adjacent installed packages."""
from __future__ import annotations

from importlib import metadata
import json
import os
from pathlib import Path
import tomllib


_PLUGIN_FILES = ("__init__.py", "plugin.yaml", "spool.py")
_SOURCE_FILES = (
    "memoryd/__init__.py",
    "memoryd/hermes_compat.py",
    "memoryd/hermes_validation/contract.py",
    "memoryd/hermes_validation/installed_runtime.py",
    "memoryd/hermes_validation/resources.py",
    "memoryd/hermes_validation/agent/memory_provider.py",
    "scripts/check_hermes_contract.py",
    "scripts/validate_installed_hermes.py",
    "migrations/001_init.sql",
    "migrations/007_api_request_ledger.sql",
)


def _valid_plugin(path: Path) -> bool:
    return path.is_dir() and all((path / name).is_file() for name in _PLUGIN_FILES)


def _installed_distribution_owns(package_root: Path) -> bool:
    try:
        distribution = metadata.distribution("memoryd")
        installed = Path(distribution.locate_file("memoryd"))
        if not os.path.samefile(installed.resolve(strict=True), package_root):
            return False
    except (metadata.PackageNotFoundError, OSError, RuntimeError):
        return False
    try:
        direct_url = json.loads(distribution.read_text("direct_url.json") or "{}")
    except (json.JSONDecodeError, UnicodeError):
        return True
    return direct_url.get("dir_info", {}).get("editable") is not True


def source_checkout_root(package_root: Path) -> Path:
    """Return a positively identified memoryd source checkout or raise."""
    package_root = package_root.resolve(strict=True)
    if _installed_distribution_owns(package_root):
        raise ValueError("installed memoryd distributions cannot use source fallbacks")
    root = package_root.parent
    pyproject = root / "pyproject.toml"
    try:
        project = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as exc:
        raise ValueError("memoryd source checkout has no valid pyproject.toml") from exc
    name = project.get("project", {}).get("name")
    packages = (
        project.get("tool", {})
        .get("hatch", {})
        .get("build", {})
        .get("targets", {})
        .get("wheel", {})
        .get("packages")
    )
    if name != "memoryd" or packages != ["memoryd"]:
        raise ValueError("memoryd source checkout project identity is invalid")
    if not all((root / relative).is_file() for relative in _SOURCE_FILES):
        raise ValueError("memoryd source checkout layout is incomplete")
    plugin = root / "hermes_plugin" / "memoryd"
    if not _valid_plugin(plugin):
        raise ValueError("memoryd source checkout plugin is incomplete")
    return root


def canonical_plugin_source(package_root: Path) -> Path:
    """Resolve only a wheel-bundled plugin or a verified source-checkout plugin."""
    package_root = package_root.resolve(strict=True)
    packaged = package_root / "hermes_plugin"
    if packaged.exists():
        if not _valid_plugin(packaged):
            raise ValueError("wheel-bundled memoryd plugin is incomplete")
        return packaged.resolve(strict=True)
    source_root = source_checkout_root(package_root)
    return (source_root / "hermes_plugin" / "memoryd").resolve(strict=True)


def canonical_migrations_source(package_root: Path) -> Path:
    """Resolve migrations paired with the selected wheel or source checkout."""
    package_root = package_root.resolve(strict=True)
    packaged = package_root / "migrations"
    required = ("001_init.sql", "007_api_request_ledger.sql")
    if packaged.exists():
        if not packaged.is_dir() or not all((packaged / name).is_file() for name in required):
            raise ValueError("wheel-bundled memoryd migrations are incomplete")
        return packaged.resolve(strict=True)
    source_root = source_checkout_root(package_root)
    return (source_root / "migrations").resolve(strict=True)


def require_canonical_plugin_source(
    plugin_source: Path, package_root: Path
) -> Path:
    expected = canonical_plugin_source(package_root)
    try:
        supplied = plugin_source.resolve(strict=True)
        matches = supplied.is_dir() and os.path.samefile(supplied, expected)
    except (OSError, RuntimeError):
        matches = False
    if not matches:
        raise ValueError("--plugin-source must be the wheel-bundled memoryd plugin")
    return expected
