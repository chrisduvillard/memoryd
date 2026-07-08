"""Claude Code hooks — stdlib only, cross-platform (replaces the old bash hooks).

Registered by `memoryd install` in ~/.claude/settings.json as:
  <python> -m memoryd.hook recall             (UserPromptSubmit)
  <python> -m memoryd.hook capture <trigger>  (Stop / SessionEnd / PreCompact)

Fail-open contract (spec P9): recall failures — daemon unreachable OR
daemon erroring — emit a VISIBLE marker, never silent degradation; capture
failures spool to disk for the daemon/microsleep to drain. Deliberately does
not import memoryd.core: no psycopg import on every prompt.
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.request
from pathlib import Path

UNAVAILABLE = "[memory: unavailable this turn — proceeding without recall]"


def _cfg() -> tuple[int, Path]:
    """(port, home) with precedence env > ~/memory/config.json > defaults."""
    home = Path(os.environ.get("MEMORYD_HOME", "~/memory")).expanduser()
    try:
        file = json.loads((home / "config.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        file = {}
    port = int(os.environ.get("MEMORYD_PORT") or file.get("port") or 7437)
    home = Path(os.environ.get("MEMORYD_HOME") or file.get("home") or home).expanduser()
    return port, home


def _post(port: int, path: str, body: dict, timeout: float) -> dict:
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}", data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def _emit_context(md: str) -> None:
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "UserPromptSubmit", "additionalContext": md}}))


def recall(stdin: dict, port: int) -> None:
    body = {
        "prompt": stdin.get("prompt", ""),
        "session_id": stdin.get("session_id", "unknown"),
        "project": os.path.basename(stdin.get("cwd", "") or "") or None,
    }
    try:
        pkt = _post(port, "/recall", body, timeout=1.5)
    except Exception:  # noqa: BLE001 — connection error, timeout, or HTTP 5xx
        _emit_context(UNAVAILABLE)
        return
    md = pkt.get("markdown", "")
    if md:
        _emit_context(md)
    # empty markdown = daemon healthy, nothing relevant: silence is correct


def capture(stdin: dict, trigger: str, port: int, home: Path) -> None:
    transcript = stdin.get("transcript_path")
    if not transcript:
        return
    body = {
        "transcript_path": transcript,
        "session_id": stdin.get("session_id", "unknown"),
        "project": os.path.basename(stdin.get("cwd", "") or "") or None,
        "trigger": trigger,
    }
    try:
        _post(port, "/capture", body, timeout=2)
    except Exception:  # noqa: BLE001 — spool; drained on daemon start / microsleep
        try:
            spool = home / "spool"
            spool.mkdir(parents=True, exist_ok=True)
            # time_ns+pid is collision-safe across concurrent hook processes
            (spool / f"cap-{time.time_ns()}-{os.getpid()}.json").write_text(
                json.dumps(body), encoding="utf-8")
        except OSError:
            pass


def main() -> None:
    try:
        stdin = json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError:
        stdin = {}
    port, home = _cfg()
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    if mode == "recall":
        recall(stdin, port)
    elif mode == "capture":
        capture(stdin, sys.argv[2] if len(sys.argv) > 2 else "unknown", port, home)


if __name__ == "__main__":
    try:
        main()
    except Exception:  # noqa: BLE001 — a crashing hook must never block the agent
        pass
    sys.exit(0)
