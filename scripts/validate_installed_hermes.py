#!/usr/bin/env python3
"""Validate memoryd through an installed Hermes runtime and real loader."""
from __future__ import annotations

import argparse
import hashlib
import inspect
import json
import os
import shutil
import site
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import metadata
from pathlib import Path
from typing import Any


PINNED_HERMES_VERSION = "0.16.0"


def _memoryd_package_root() -> Path:
    import memoryd
    return Path(memoryd.__file__).resolve().parent


def require_installed_plugin_source(
        plugin_source: Path, *,
        site_roots: list[Path] | None = None) -> Path:
    """Require the plugin resource from the installed memoryd wheel."""
    source = plugin_source.resolve()
    expected = (_memoryd_package_root() / "hermes_plugin").resolve()
    try:
        same_source = os.path.samefile(source, expected)
    except OSError:
        same_source = False
    if not same_source or not (expected / "__init__.py").is_file():
        raise ValueError(
            f"--plugin-source must be the wheel-bundled memoryd plugin: {expected}")
    source = expected
    roots = [Path(root).resolve() for root in (
        site_roots if site_roots is not None else site.getsitepackages())]
    if not any(root == source or root in source.parents for root in roots):
        raise ValueError(
            f"wheel-bundled plugin is not under installed site-packages: {source}")
    return source


def _plugin_manifest(root: Path) -> dict[str, str]:
    manifest: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if "__pycache__" in relative.parts or path.suffix == ".pyc":
            continue
        if path.is_file():
            manifest[relative.as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return manifest


def assert_plugin_copy_origin(source: Path, target: Path) -> None:
    if _plugin_manifest(source.resolve()) != _plugin_manifest(target.resolve()):
        raise RuntimeError(
            "isolated Hermes plugin copy differs from installed wheel origin")


def _owner_write(path: Path, text: str) -> None:
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        if os.name != "nt":
            os.chmod(temporary, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        if os.name != "nt":
            os.chmod(path, 0o600)
    finally:
        temporary.unlink(missing_ok=True)


def prepare_isolated_home(home: Path, plugin_source: Path, url: str) -> None:
    home = home.resolve()
    plugin_source = plugin_source.resolve()
    if not plugin_source.is_dir() or not (plugin_source / "__init__.py").is_file():
        raise ValueError(f"plugin source is invalid: {plugin_source}")
    if home.exists() and any(home.iterdir()):
        raise ValueError(f"isolated HERMES_HOME must be empty: {home}")
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name != "nt":
        os.chmod(home, 0o700)
    plugin_target = home / "plugins" / "memoryd"
    plugin_target.parent.mkdir(parents=True, mode=0o700)
    shutil.copytree(plugin_source, plugin_target)
    assert_plugin_copy_origin(plugin_source, plugin_target)
    _owner_write(
        home / "memoryd.json",
        json.dumps({"url": url}, indent=2) + "\n",
    )
    _owner_write(home / "config.yaml", "memory:\n  provider: memoryd\n")


def require_hermes_version(expected: str = PINNED_HERMES_VERSION) -> str:
    actual = metadata.version("hermes-agent")
    if actual != expected:
        raise RuntimeError(
            f"expected hermes-agent {expected}, got {actual}")
    return actual


class _ProbeServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), _ProbeHandler)
        self.records: list[tuple[str, dict[str, Any]]] = []
        self.records_lock = threading.Lock()


class _ProbeHandler(BaseHTTPRequestHandler):
    server: _ProbeServer

    def log_message(self, _format: str, *_args: Any) -> None:
        return

    def _json(self, status: int, body: dict[str, Any]) -> None:
        encoded = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        if self.path == "/health":
            self._json(200, {"ok": True})
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        try:
            length = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(length))
            if not isinstance(body, dict):
                raise ValueError("body must be an object")
        except (ValueError, json.JSONDecodeError):
            self._json(400, {"error": "invalid JSON"})
            return
        with self.server.records_lock:
            self.server.records.append((self.path, body))
        if self.path == "/recall":
            self._json(200, {
                "markdown": "installed-hermes-runtime-ok",
                "latency_ms": 1,
            })
        elif self.path == "/capture-events":
            self._json(200, {
                "ok": True,
                "stored": len(body.get("events") or []),
                "request_id": body.get("request_id"),
                "duplicate": False,
            })
        elif self.path == "/extract":
            self._json(200, {
                "queued": True,
                "request_id": body.get("request_id"),
                "duplicate": False,
            })
        elif self.path == "/miss":
            self._json(200, {
                "ok": True,
                "request_id": body.get("request_id"),
                "duplicate": False,
            })
        else:
            self._json(404, {"error": "not found"})


def _wait_until(predicate, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return bool(predicate())


def validate_installed_runtime(
        home: Path, expected_version: str,
        plugin_source: Path) -> dict[str, Any]:
    actual_version = require_hermes_version(expected_version)
    os.environ["HERMES_HOME"] = str(home.resolve())

    # Imports deliberately happen only after HERMES_HOME is fixed. Hermes
    # caches profile paths at module import time.
    from agent.memory_manager import MemoryManager
    from agent.memory_provider import MemoryProvider
    from plugins.memory import (
        discover_memory_providers,
        discover_plugin_cli_commands,
        load_memory_provider,
    )

    discovered = {name: available for name, _description, available
                  in discover_memory_providers()}
    if discovered.get("memoryd") is not True:
        raise RuntimeError(f"installed Hermes did not discover memoryd: {discovered}")
    provider = load_memory_provider("memoryd")
    if not isinstance(provider, MemoryProvider):
        raise RuntimeError("installed Hermes loader did not return a MemoryProvider")
    plugin_target = (home / "plugins" / "memoryd").resolve()
    provider_path = Path(inspect.getfile(provider.__class__)).resolve()
    if plugin_target not in provider_path.parents:
        raise RuntimeError(
            f"Hermes loaded memoryd outside the isolated profile: {provider_path}")
    assert_plugin_copy_origin(plugin_source, plugin_target)
    commands = discover_plugin_cli_commands()
    if not any(command.get("name") == "memoryd" for command in commands):
        raise RuntimeError("installed Hermes did not register the memoryd CLI")

    manager = MemoryManager()
    manager.add_provider(provider)
    session_id = "memoryd-installed-hermes-validation"
    manager.initialize_all(
        session_id,
        platform="cli",
        agent_context="primary",
        agent_identity="validation",
    )
    compression_result = "not-called"
    try:
        prompt = manager.build_system_prompt()
        if "memoryd" not in prompt.lower():
            raise RuntimeError("MemoryManager omitted the provider system prompt")
        recalled = manager.prefetch_all(
            "installed loader lifecycle probe", session_id=session_id)
        if "installed-hermes-runtime-ok" not in recalled:
            raise RuntimeError("MemoryManager prefetch did not reach the probe daemon")
        manager.sync_all(
            "installed lifecycle user turn",
            "installed lifecycle assistant turn",
            session_id=session_id,
            messages=[{"role": "user", "content": "loader probe"}],
        )
        compression_messages = [
            {"role": "user", "content": "compression lifecycle probe"},
            {"role": "assistant", "content": "preserve this snapshot"},
        ]
        compression_result = manager.on_pre_compress(compression_messages)
        if compression_result != "":
            raise RuntimeError(
                f"unexpected memoryd compression summary: {compression_result!r}")
        manager.on_session_end([
            {"role": "user", "content": "loader probe"},
            {"role": "assistant", "content": "loader ok"},
        ])
        spool = getattr(provider, "_spool_store", None)
        if spool is None:
            raise RuntimeError("provider did not initialize its durable spool")
        if not _wait_until(lambda: sum(spool.counts().values()) == 0):
            raise RuntimeError(f"provider spool did not drain: {spool.counts()}")
        counts = spool.counts()
        if counts["dead_letter"] or spool.fault():
            raise RuntimeError(
                f"provider durability failure: counts={counts}, fault={spool.fault()}")
    finally:
        manager.shutdown_all()
    worker = getattr(provider, "_worker", None)
    if worker is not None and worker.is_alive():
        raise RuntimeError("MemoryManager shutdown left the provider worker alive")
    assert_plugin_copy_origin(plugin_source, plugin_target)

    return {
        "hermes_version": actual_version,
        "provider": provider.name,
        "provider_path": str(provider_path),
        "plugin_source": str(plugin_source),
        "cli_registered": True,
        "pre_compress_result": compression_result,
        "spool": counts,
    }


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hermes-home", required=True, type=Path)
    parser.add_argument("--plugin-source", required=True, type=Path)
    parser.add_argument("--expected-version", default=PINNED_HERMES_VERSION)
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    home = args.hermes_home.expanduser()
    if not home.is_absolute():
        raise SystemExit("--hermes-home must be absolute")
    try:
        plugin_source = require_installed_plugin_source(args.plugin_source)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    server = _ProbeServer()
    url = f"http://127.0.0.1:{server.server_address[1]}"
    prepare_isolated_home(home, plugin_source, url)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        result = validate_installed_runtime(
            home, args.expected_version, plugin_source)
        with server.records_lock:
            records = list(server.records)
        endpoints = [endpoint for endpoint, _body in records]
        for required in ("/recall", "/capture-events", "/extract"):
            if required not in endpoints:
                raise RuntimeError(
                    f"installed lifecycle did not call {required}: {endpoints}")
        pre_compress_captured = any(
            endpoint == "/capture-events" and any(
                event.get("payload", {}).get("note") == "pre_compress_snapshot"
                for event in body.get("events", []))
            for endpoint, body in records)
        if not pre_compress_captured:
            raise RuntimeError(
                "MemoryManager.on_pre_compress did not queue its capture event")
        result["endpoints"] = endpoints
        result["pre_compress_captured"] = True
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)


if __name__ == "__main__":
    raise SystemExit(main())
