"""hermes memoryd — provider CLI subcommands (status | config | miss)."""
from __future__ import annotations

import importlib.util
import json
import os
import urllib.request
from pathlib import Path


def _cfg() -> dict:
    try:
        from hermes_constants import get_hermes_home  # profile-scoped
        home = Path(get_hermes_home())
    except Exception:  # noqa: BLE001
        home = Path("~/.hermes").expanduser()
    f = home / "memoryd.json"
    if f.exists():
        try:
            return json.loads(f.read_text())
        except json.JSONDecodeError:
            pass
    return {}


def _url() -> str:
    return (_cfg().get("url") or "http://127.0.0.1:7437").rstrip("/")


def _home() -> Path:
    try:
        from hermes_constants import get_hermes_home  # profile-scoped
        return Path(get_hermes_home())
    except Exception:  # noqa: BLE001
        return Path("~/.hermes").expanduser()


def _spool_status(home: Path) -> dict:
    """Read queue health without creating state or importing the provider."""
    root = Path(home) / "spool" / "memoryd"
    result = {
        "incoming": sum(1 for _ in (root / "incoming").glob("*.json")),
        "processing": sum(1 for _ in (root / "processing").glob("*.json")),
        "dead_letter": sum(1 for _ in (root / "dead-letter").glob("*.json")),
        "fault": None,
        "power_loss_durability": (
            "posix-directory-fsync" if os.name != "nt"
            else "windows-write-through-best-effort"),
    }
    state = root / "state.json"
    if state.exists():
        try:
            value = json.loads(state.read_text(encoding="utf-8"))
            if not isinstance(value, dict):
                raise ValueError("state is not an object")
            result["fault"] = value.get("durability_fault")
        except (OSError, ValueError, json.JSONDecodeError):
            result["fault"] = "unreadable spool state"
    result["healthy"] = not result["dead_letter"] and not result["fault"]
    return result


def _durable_spool_class():
    try:
        from . import DurableSpool
        return DurableSpool
    except (ImportError, ValueError):
        # Some Hermes plugin loaders execute CLI modules outside a package.
        spec = importlib.util.spec_from_file_location(
            "hermes_memoryd_provider_for_cli", Path(__file__).with_name("__init__.py"))
        module = importlib.util.module_from_spec(spec)
        if spec.loader is None:
            raise ImportError("cannot load memoryd durable spool")
        spec.loader.exec_module(module)
        return module.DurableSpool


def memoryd_command(args) -> None:
    sub = getattr(args, "memoryd_command", None)
    if sub == "status":
        spool = _spool_status(_home())
        daemon_ok = False
        try:
            with urllib.request.urlopen(f"{_url()}/health", timeout=3) as r:
                health = json.loads(r.read())
                daemon_ok = 200 <= getattr(r, "status", r.getcode()) <= 299
                print(f"memoryd at {_url()}: {health}")
        except Exception as e:  # noqa: BLE001
            print(f"memoryd at {_url()}: UNREACHABLE ({e})")
        print("Hermes durable spool: "
              f"incoming={spool['incoming']} processing={spool['processing']} "
              f"dead-letter={spool['dead_letter']} "
              f"fault={spool['fault'] or 'none'} "
              f"power-loss={spool['power_loss_durability']}")
        if not daemon_ok or not spool["healthy"]:
            print("memoryd status: UNHEALTHY")
            raise SystemExit(1)
        print("memoryd status: healthy")
    elif sub == "config":
        print(json.dumps(_cfg() or {"url": _url()}, indent=2))
    elif sub == "miss":
        detail = " ".join(getattr(args, "detail", []) or [])
        try:
            spool = _durable_spool_class()(_home())
            request_id = spool.persist(
                "/miss", {"session_id": "cli", "signal": "manual",
                          "detail": {"note": detail}})
            print(f"miss queued durably (request_id={request_id})")
        except Exception as e:  # noqa: BLE001
            print(f"failed to queue miss durably: {e}")
            raise SystemExit(1) from e
    else:
        print("Usage: hermes memoryd <status|config|miss [text]>")


def register_cli(subparser) -> None:
    subs = subparser.add_subparsers(dest="memoryd_command")
    subs.add_parser("status", help="Check memoryd daemon health")
    subs.add_parser("config", help="Show memoryd provider config")
    m = subs.add_parser("miss", help="Record a retrieval-miss signal")
    m.add_argument("detail", nargs="*", help="What was missed")
    subparser.set_defaults(func=memoryd_command)
