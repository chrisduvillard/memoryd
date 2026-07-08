"""hermes memoryd — provider CLI subcommands (status | config | miss)."""
from __future__ import annotations

import json
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


def memoryd_command(args) -> None:
    sub = getattr(args, "memoryd_command", None)
    if sub == "status":
        try:
            with urllib.request.urlopen(f"{_url()}/health", timeout=3) as r:
                print(f"memoryd at {_url()}: {json.loads(r.read())}")
        except Exception as e:  # noqa: BLE001
            print(f"memoryd at {_url()}: UNREACHABLE ({e})")
    elif sub == "config":
        print(json.dumps(_cfg() or {"url": _url()}, indent=2))
    elif sub == "miss":
        detail = " ".join(getattr(args, "detail", []) or [])
        body = json.dumps({"session_id": "cli", "signal": "manual",
                           "detail": {"note": detail}}).encode()
        req = urllib.request.Request(f"{_url()}/miss", data=body,
                                     headers={"Content-Type": "application/json"})
        try:
            urllib.request.urlopen(req, timeout=3)
            print("miss recorded")
        except Exception as e:  # noqa: BLE001
            print(f"failed: {e}")
    else:
        print("Usage: hermes memoryd <status|config|miss [text]>")


def register_cli(subparser) -> None:
    subs = subparser.add_subparsers(dest="memoryd_command")
    subs.add_parser("status", help="Check memoryd daemon health")
    subs.add_parser("config", help="Show memoryd provider config")
    m = subs.add_parser("miss", help="Record a retrieval-miss signal")
    m.add_argument("detail", nargs="*", help="What was missed")
    subparser.set_defaults(func=memoryd_command)
