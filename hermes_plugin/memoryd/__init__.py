"""memoryd — Hermes memory provider plugin.

Bridges Hermes Agent to a running memoryd daemon (the agent-agnostic memory
substrate). Hermes is a tenant: recalled context is injected before each
turn (prefetch), every turn is captured to the canonical event ledger
(sync_turn), built-in MEMORY.md writes are mirrored (on_memory_write, i.e.
vendor memory observed as cache), messages about to be compressed are
captured first (on_pre_compress), subagent delegations are recorded
(on_delegation), and session end triggers extraction (on_session_end).

Design rules (mirroring the memoryd slice spec):
- FAIL-OPEN: if the daemon is unreachable, Hermes proceeds; recall returns
  a visible marker once, captures spool in-memory and flush on recovery.
- NON-BLOCKING: all writes go through a background queue; prefetch serves
  a cached packet refreshed by queue_prefetch after each turn.
- SUBAGENT/CRON SAFETY: non-primary agent_context skips writes entirely
  (a cron system prompt must never become a durable memory).

Install: copy this directory to <hermes>/plugins/memory/memoryd/ (or
~/.hermes/plugins/memory/memoryd/), then:
    hermes config set memory.provider memoryd
Config lives in <HERMES_HOME>/memoryd.json (written by `hermes memory setup`).
"""
from __future__ import annotations

import json
import queue
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional

from agent.memory_provider import MemoryProvider

DEFAULT_URL = "http://127.0.0.1:7437"
AGENT_NAME = "hermes"
SPOOL_MAX = 500  # in-memory event spool cap while daemon is down


class MemorydProvider(MemoryProvider):

    def __init__(self) -> None:
        self._url = DEFAULT_URL
        self._project: Optional[str] = None
        self._session_id = ""
        self._platform = "cli"
        self._primary = True
        self._prefetch_cache: Dict[str, str] = {}
        self._q: "queue.Queue[dict]" = queue.Queue()
        self._spool: List[dict] = []
        self._worker: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._warned_down = False

    # ------------------------------------------------------------ identity

    @property
    def name(self) -> str:
        return "memoryd"

    def is_available(self) -> bool:
        # Config/deps check only — no network calls (per ABC contract).
        return True  # stdlib-only; URL has a localhost default

    # ------------------------------------------------------------ lifecycle

    def initialize(self, session_id: str, **kwargs) -> None:
        self._session_id = session_id
        self._platform = kwargs.get("platform", "cli")
        ctx = kwargs.get("agent_context", "primary")
        self._primary = ctx == "primary"
        home = kwargs.get("hermes_home")
        cfg = {}
        if home:
            f = Path(home) / "memoryd.json"
            if f.exists():
                try:
                    cfg = json.loads(f.read_text())
                except json.JSONDecodeError:
                    cfg = {}
        self._url = (cfg.get("url") or DEFAULT_URL).rstrip("/")
        self._project = cfg.get("project") or f"hermes-{self._platform}"
        if self._worker is None:
            self._worker = threading.Thread(target=self._drain, daemon=True)
            self._worker.start()
        if self._primary:
            self._enqueue([{"kind": "session_start",
                            "payload": {"platform": self._platform}}])

    def shutdown(self) -> None:
        self._stop.set()
        deadline = time.monotonic() + 3.0
        while not self._q.empty() and time.monotonic() < deadline:
            time.sleep(0.05)

    # ------------------------------------------------------------ recall

    def system_prompt_block(self) -> str:
        return (
            "External long-term memory (memoryd) is active. Recalled context "
            "is injected each turn under '## Memory'; entries cite mem_ ids "
            "and certainty lanes. Treat 'Unconfirmed candidates' as unverified. "
            "Use the memoryd_search tool for anything not already recalled; "
            "use memoryd_report_miss when the user says you forgot something."
        )

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        sid = session_id or self._session_id
        cached = self._prefetch_cache.pop(sid, None)
        if cached is not None:
            return cached
        # first turn of a session: one bounded synchronous attempt, fail-open
        pkt = self._recall(query, sid, timeout=1.5)
        if pkt is None:
            if not self._warned_down:
                self._warned_down = True
                return "[memoryd: unavailable — proceeding without external recall]"
            return ""
        return pkt

    def queue_prefetch(self, query: str, *, session_id: str = "") -> None:
        sid = session_id or self._session_id

        def _bg() -> None:
            pkt = self._recall(query, sid, timeout=5.0)
            if pkt is not None:
                self._prefetch_cache[sid] = pkt
                self._warned_down = False

        threading.Thread(target=_bg, daemon=True).start()

    # ------------------------------------------------------------ capture

    def sync_turn(self, user_content: str, assistant_content: str, *,
                  session_id: str = "", messages: Optional[List[Dict[str, Any]]] = None,
                  ) -> None:
        if not self._primary:
            return  # never let cron/subagent contexts write user memory
        evs = []
        if user_content:
            evs.append({"kind": "user_message", "payload": {"text": user_content}})
        if assistant_content:
            evs.append({"kind": "agent_response", "payload": {"text": assistant_content}})
        if messages:
            tools = [m for m in messages[-10:] if m.get("role") == "tool"
                     or (m.get("role") == "assistant" and m.get("tool_calls"))]
            if tools:
                evs.append({"kind": "tool_call",
                            "payload": {"summary": f"{len(tools)} tool interactions this turn"}})
        self._enqueue(evs, session_id=session_id or self._session_id)

    def on_pre_compress(self, messages: List[Dict[str, Any]]) -> str:
        # our PreCompact equivalent: capture BEFORE context dies. Raw first.
        if self._primary and messages:
            text = "\n".join(
                f"{m.get('role','?')}: {self._text_of(m)}" for m in messages)[:200000]
            self._enqueue([{"kind": "external_note",
                            "payload": {"text": text, "note": "pre_compress_snapshot"}}])
        return ""  # no contribution to the compressor prompt needed

    def on_memory_write(self, action: str, target: str, content: str,
                        metadata: Optional[Dict[str, Any]] = None) -> None:
        # vendor memory as cache, observed: mirror built-in MEMORY.md/USER.md
        # writes into canonical so nothing lives only in the vendor layer
        if not self._primary:
            return
        self._enqueue([{"kind": "external_note",
                        "payload": {"text": content, "note": "builtin_memory_write",
                                    "action": action, "target": target,
                                    "meta": metadata or {}}}])

    def on_delegation(self, task: str, result: str, *,
                      child_session_id: str = "", **kwargs) -> None:
        if not self._primary:
            return
        self._enqueue([{"kind": "delegation",
                        "payload": {"task": task[:4000], "result": result[:4000],
                                    "child_session_id": child_session_id}}])

    def on_session_switch(self, new_session_id: str, *, parent_session_id: str = "",
                          reset: bool = False, rewound: bool = False, **kwargs) -> None:
        old = self._session_id
        self._session_id = new_session_id
        self._prefetch_cache.pop(old, None)
        if reset and old and self._primary:
            # genuine conversation boundary: extract what the old session taught us
            self._enqueue([{"kind": "session_end", "payload": {"reason": "reset"}}],
                          session_id=old)
            self._post("/extract", {"session_id": old}, timeout=2.0)

    def on_session_end(self, messages: List[Dict[str, Any]]) -> None:
        if not self._primary:
            return
        self._enqueue([{"kind": "session_end", "payload": {"turns": len(messages)}}])
        self._flush(3.0)
        self._post("/extract", {"session_id": self._session_id}, timeout=2.0)

    # ------------------------------------------------------------ tools

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        return [
            {"name": "memoryd_search",
             "description": ("Search long-term external memory (memoryd) for facts, "
                             "decisions, preferences, warnings, and project state from "
                             "past sessions across all agents. Use when the recalled "
                             "'## Memory' block doesn't already answer the question."),
             "parameters": {"type": "object",
                            "properties": {"query": {"type": "string",
                                                     "description": "what to look for"}},
                            "required": ["query"]}},
            {"name": "memoryd_report_miss",
             "description": ("Report that external memory failed to recall something "
                             "it should have known (e.g. the user says 'you forgot' or "
                             "re-explains context). Improves future retrieval."),
             "parameters": {"type": "object",
                            "properties": {"detail": {"type": "string"}},
                            "required": ["detail"]}},
        ]

    def handle_tool_call(self, tool_name: str, args: Dict[str, Any], **kwargs) -> str:
        if tool_name == "memoryd_search":
            pkt = self._recall(args.get("query", ""), self._session_id, timeout=5.0)
            if pkt is None:
                return json.dumps({"ok": False, "error": "memoryd unreachable"})
            return json.dumps({"ok": True, "memory": pkt or "(nothing relevant found)"})
        if tool_name == "memoryd_report_miss":
            ok = self._post("/miss", {"session_id": self._session_id,
                                      "signal": "user_said_forgot",
                                      "detail": {"note": args.get("detail", "")}},
                            timeout=3.0)
            return json.dumps({"ok": bool(ok)})
        raise NotImplementedError(tool_name)

    # ------------------------------------------------------------ config

    def get_config_schema(self) -> List[Dict[str, Any]]:
        return [
            {"key": "url", "description": "memoryd daemon URL",
             "default": DEFAULT_URL, "required": False},
            {"key": "project", "description":
                "Fixed project label for this profile (default: hermes-<platform>)",
             "required": False},
        ]

    def save_config(self, values: Dict[str, Any], hermes_home: str) -> None:
        f = Path(hermes_home) / "memoryd.json"
        f.write_text(json.dumps({k: v for k, v in values.items() if v}, indent=2))

    def backup_paths(self) -> List[str]:
        return []  # memoryd's store is a server-side DB + archive; backed up there

    # ------------------------------------------------------------ plumbing

    @staticmethod
    def _text_of(m: Dict[str, Any]) -> str:
        c = m.get("content")
        if isinstance(c, str):
            return c
        if isinstance(c, list):
            return " ".join(b.get("text", "") for b in c
                            if isinstance(b, dict) and b.get("type") == "text")
        return ""

    def _recall(self, prompt: str, session_id: str, timeout: float) -> Optional[str]:
        r = self._post("/recall", {"prompt": prompt, "session_id": session_id,
                                   "project": self._project, "agent": AGENT_NAME},
                       timeout=timeout)
        if r is None:
            return None
        return r.get("markdown", "")

    def _enqueue(self, events: List[dict], session_id: str = "") -> None:
        if events:
            self._q.put({"session_id": session_id or self._session_id,
                         "events": events})

    def _drain(self) -> None:
        while not self._stop.is_set():
            try:
                job = self._q.get(timeout=0.25)
            except queue.Empty:
                if self._spool and self._flush_spool():
                    continue
                continue
            body = {"agent": AGENT_NAME, "session_id": job["session_id"],
                    "project": self._project, "events": job["events"]}
            if self._post("/capture-events", body, timeout=5.0) is None:
                if len(self._spool) < SPOOL_MAX:
                    self._spool.append(body)
            else:
                self._flush_spool()
            self._q.task_done()

    def _flush_spool(self) -> bool:
        while self._spool:
            if self._post("/capture-events", self._spool[0], timeout=5.0) is None:
                return False
            self._spool.pop(0)
        return True

    def _flush(self, seconds: float) -> None:
        deadline = time.monotonic() + seconds
        while not self._q.empty() and time.monotonic() < deadline:
            time.sleep(0.05)

    def _post(self, path: str, body: dict, timeout: float) -> Optional[dict]:
        try:
            req = urllib.request.Request(
                f"{self._url}{path}", data=json.dumps(body).encode(),
                headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read() or b"{}")
        except Exception:  # noqa: BLE001 — fail-open everywhere
            return None


def register(ctx) -> None:
    """Entry point called by Hermes plugin discovery."""
    ctx.register_memory_provider(MemorydProvider())
