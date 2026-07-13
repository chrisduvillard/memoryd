from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest


def _validator():
    from memoryd.hermes_validation import installed_runtime

    return installed_runtime


def test_prepare_isolated_home_uses_real_user_provider_layout(tmp_path):
    module = _validator()
    source = tmp_path / "source"
    source.mkdir()
    (source / "__init__.py").write_text("# provider\n", encoding="utf-8")
    (source / "plugin.yaml").write_text("name: memoryd\n", encoding="utf-8")
    home = tmp_path / "isolated-hermes"

    module.prepare_isolated_home(home, source, "http://127.0.0.1:17437")

    assert (home / "plugins" / "memoryd" / "__init__.py").is_file()
    assert json.loads((home / "memoryd.json").read_text(encoding="utf-8")) == {
        "url": "http://127.0.0.1:17437"}
    assert "provider: memoryd" in (home / "config.yaml").read_text(encoding="utf-8")
    if os.name != "nt":
        assert stat.S_IMODE(home.stat().st_mode) == 0o700
        assert stat.S_IMODE((home / "memoryd.json").stat().st_mode) == 0o600


def test_prepare_isolated_home_refuses_existing_content(tmp_path):
    module = _validator()
    source = tmp_path / "source"
    source.mkdir()
    (source / "__init__.py").write_text("# provider\n", encoding="utf-8")
    home = tmp_path / "isolated-hermes"
    home.mkdir()
    (home / "keep").write_text("evidence", encoding="utf-8")

    with pytest.raises(ValueError, match="empty"):
        module.prepare_isolated_home(home, source, "http://127.0.0.1:17437")

    assert (home / "keep").read_text(encoding="utf-8") == "evidence"


def test_exact_hermes_version_is_required(monkeypatch):
    module = _validator()
    monkeypatch.setattr(module.metadata, "version", lambda _name: "0.16.1")
    with pytest.raises(RuntimeError, match="expected hermes-agent 0.16.0"):
        module.require_hermes_version("0.16.0")


def test_plugin_source_must_be_the_wheel_copy_under_site_packages(
        monkeypatch, tmp_path):
    module = _validator()
    site_packages = tmp_path / "venv" / "lib" / "site-packages"
    package = site_packages / "memoryd"
    expected = package / "hermes_plugin"
    expected.mkdir(parents=True)
    (expected / "__init__.py").write_text("# wheel plugin\n", encoding="utf-8")
    (expected / "plugin.yaml").write_text("name: memoryd\n", encoding="utf-8")
    (expected / "spool.py").write_text("SCHEMA_VERSION = 1\n", encoding="utf-8")
    monkeypatch.setattr(module, "_memoryd_package_root", lambda: package)

    assert module.require_installed_plugin_source(
        expected, site_roots=[site_packages]) == expected.resolve()
    with pytest.raises(ValueError, match="wheel-bundled"):
        module.require_installed_plugin_source(
            tmp_path / "checkout" / "hermes_plugin" / "memoryd",
            site_roots=[site_packages])


def test_damaged_installed_package_rejects_adjacent_fake_plugin(
        monkeypatch, tmp_path):
    module = _validator()
    site_packages = tmp_path / "venv" / "lib" / "site-packages"
    package = site_packages / "memoryd"
    package.mkdir(parents=True)
    adjacent = site_packages / "hermes_plugin" / "memoryd"
    adjacent.mkdir(parents=True)
    (adjacent / "__init__.py").write_text("# adjacent fake\n", encoding="utf-8")
    monkeypatch.setattr(module, "_memoryd_package_root", lambda: package)

    with pytest.raises(ValueError, match="wheel-bundled"):
        module.require_installed_plugin_source(adjacent)


def test_isolated_plugin_copy_preserves_installed_origin(tmp_path):
    module = _validator()
    source = tmp_path / "site-packages" / "memoryd" / "hermes_plugin"
    source.mkdir(parents=True)
    (source / "__init__.py").write_text("# provider\n", encoding="utf-8")
    (source / "spool.py").write_text("SCHEMA_VERSION = 1\n", encoding="utf-8")
    home = tmp_path / "profile"

    module.prepare_isolated_home(home, source, "http://127.0.0.1:17437")
    target = home / "plugins" / "memoryd"
    module.assert_plugin_copy_origin(source, target)
    (target / "spool.py").write_text("tampered = True\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="differs from installed wheel"):
        module.assert_plugin_copy_origin(source, target)
