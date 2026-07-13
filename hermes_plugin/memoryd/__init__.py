"""Crash-durable Hermes memory provider for the memoryd daemon.

Primary-context mutations are synchronously published to a profile-scoped
disk spool before a hook returns. A background worker drains that spool;
recall remains fail-open and non-primary contexts remain read-only.
"""
from __future__ import annotations

import contextlib
import hashlib as _hashlib
import http.client
import importlib.util
import json
import logging
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional

from agent.memory_provider import MemoryProvider

DEFAULT_URL = "http://127.0.0.1:7437"
AGENT_NAME = "hermes"
logger = logging.getLogger(__name__)

try:
    from . import spool as _spool_module
except ImportError:
    _spool_path = Path(__file__).with_name("spool.py").resolve()
    _spool_name = "_hermes_memoryd_spool_" + _hashlib.sha256(
        str(_spool_path).encode("utf-8")).hexdigest()[:16]
    _spool_module = sys.modules.get(_spool_name)
    if _spool_module is None:
        _spool_spec = importlib.util.spec_from_file_location(_spool_name, _spool_path)
        if _spool_spec is None or _spool_spec.loader is None:
            raise ImportError(f"cannot load durable spool from {_spool_path}")
        _spool_module = importlib.util.module_from_spec(_spool_spec)
        sys.modules[_spool_name] = _spool_module
        _spool_spec.loader.exec_module(_spool_module)

DurableSpool = _spool_module.DurableSpool
JobCollision = _spool_module.JobCollision
HttpResult = _spool_module.HttpResult
SCHEMA_VERSION = _spool_module.SCHEMA_VERSION
STALE_PROCESSING_SECONDS = _spool_module.STALE_PROCESSING_SECONDS
MAX_BACKOFF_SECONDS = _spool_module.MAX_BACKOFF_SECONDS
MUTATION_ENDPOINTS = _spool_module.MUTATION_ENDPOINTS
_atomic_json = _spool_module._atomic_json
_canonical_json = _spool_module._canonical_json
_fsync_dir = _spool_module._fsync_dir
_private_dir = _spool_module._private_dir
_replace = _spool_module._replace
_retryable_status = _spool_module._retryable_status


class MemorydProvider(MemoryProvider):
    def __init__(self) -> None:
        self._url = DEFAULT_URL
        self._project: Optional[str] = None
        self._session_id = ""
        self._platform = "cli"
        self._primary = True
        self._prefetch_cache: Dict[str, str] = {}
        self._spool_store: Optional[DurableSpool] = None
        self._worker: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._warned_down = False
        self._lifecycle_mutation_lock = threading.RLock()
        self._shutdown_requested = threading.Event()
        self._durability_fault: Optional[str] = None
        self._durability_notice_pending = False
        self._shutdown_handback_error: Optional[str] = None

    @property
    def name(self) -> str:
        return "memoryd"

    @property
    def durability_fault(self) -> Optional[str]:
        return self._durability_fault

    def is_available(self) -> bool:
        return True

    def initialize(self, session_id: str, **kwargs) -> None:
        self._session_id = session_id
        self._platform = kwargs.get("platform", "cli")
        self._primary = kwargs.get("agent_context", "primary") == "primary"
        home_arg = kwargs.get("hermes_home")
        cfg = {}
        if home_arg:
            config_path = Path(home_arg) / "memoryd.json"
            if config_path.exists():
                try:
                    cfg = json.loads(config_path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    cfg = {}
        self._url = (cfg.get("url") or DEFAULT_URL).rstrip("/")
        self._project = cfg.get("project") or f"hermes-{self._platform}"
        if not self._primary:
            return
        if not home_arg:
            self._record_durability_fault("initialize missing required hermes_home")
            return
        try:
            self._spool_store = DurableSpool(Path(home_arg))
            self._spool_store._ensure()
            self._spool_store.rebuild_identity_reservations()
            self._spool_store.recover_stale()
            stored_fault = self._spool_store.fault()
            if stored_fault:
                self._durability_fault = stored_fault
                self._durability_notice_pending = True
            if self._spool_store.counts()["dead_letter"]:
                self._durability_notice_pending = True
            invalid_dead = self._spool_store.audit_dead_letters()
            if invalid_dead:
                self._durability_fault = (
                    f"invalid dead-letter evidence ({len(invalid_dead)} files)")
                self._durability_notice_pending = True
            self._start_worker()
            self._persist_mutation(
                "/capture-events",
                {"agent": AGENT_NAME, "session_id": self._session_id,
                 "project": self._project,
                 "events": [{"kind": "session_start",
                             "payload": {"platform": self._platform}}]})
        except (OSError, ValueError, JobCollision) as exc:
            self._record_durability_fault(f"spool initialization failed: {exc}")

    def _start_worker(self) -> None:
        if self._worker is None:
            self._worker = threading.Thread(
                target=self._drain, name="memoryd-durable-spool", daemon=True)
            self._worker.start()

    def shutdown(self) -> None:
        if not self._primary:
            return
        self._shutdown_requested.set()
        with self._lifecycle_mutation_lock:
            self._stop.set()
        self._wake.set()
        if self._worker is not None:
            self._worker.join(timeout=6.0)
            if self._worker.is_alive():
                raise RuntimeError("memoryd worker did not stop after bounded HTTP timeout")
        if self._shutdown_handback_error is not None:
            raise RuntimeError(self._shutdown_handback_error)

    def _consume_durability_notice(self) -> str:
        if not self._durability_notice_pending:
            return ""
        self._durability_notice_pending = False
        detail = self._durability_fault or "permanent dead-letter present"
        return f"[memoryd: capture durability fault — {detail}]"

    def system_prompt_block(self) -> str:
        notice = self._consume_durability_notice()
        base = (
            "External long-term memory (memoryd) is active. Recalled context "
            "is injected each turn under '## Memory'; entries cite mem_ ids "
            "and certainty lanes. Treat 'Unconfirmed candidates' as unverified. "
            "Use the memoryd_search tool for anything not already recalled; "
            "use memoryd_report_miss when the user says you forgot something.")
        return f"{base}\n{notice}" if notice else base

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        notice = self._consume_durability_notice()
        if notice:
            return notice
        sid = session_id or self._session_id
        cached = self._prefetch_cache.pop(sid, None)
        if cached is not None:
            return cached
        packet = self._recall(query, sid, timeout=1.5)
        if packet is None:
            if not self._warned_down:
                self._warned_down = True
                return "[memoryd: unavailable — proceeding without external recall]"
            return ""
        return packet

    def queue_prefetch(self, query: str, *, session_id: str = "") -> None:
        sid = session_id or self._session_id

        def background() -> None:
            packet = self._recall(query, sid, timeout=5.0)
            if packet is not None:
                self._prefetch_cache[sid] = packet
                self._warned_down = False

        threading.Thread(target=background, daemon=True).start()

    def _capture(self, events: List[dict], session_id: str = "") -> Optional[str]:
        if not self._primary or not events:
            return None
        return self._persist_mutation(
            "/capture-events",
            {"agent": AGENT_NAME, "session_id": session_id or self._session_id,
             "project": self._project, "events": events})

    def sync_turn(self, user_content: str, assistant_content: str, *,
                  session_id: str = "", messages: Optional[List[Dict[str, Any]]] = None,
                  ) -> None:
        if not self._primary:
            return
        events = []
        if user_content:
            events.append({"kind": "user_message", "payload": {"text": user_content}})
        if assistant_content:
            events.append({"kind": "agent_response", "payload": {"text": assistant_content}})
        if messages:
            tools = [message for message in messages[-10:]
                     if message.get("role") == "tool" or
                     (message.get("role") == "assistant" and message.get("tool_calls"))]
            if tools:
                events.append({"kind": "tool_call", "payload": {
                    "summary": f"{len(tools)} tool interactions this turn"}})
        self._capture(events, session_id=session_id)

    def on_pre_compress(self, messages: List[Dict[str, Any]]) -> str:
        if self._primary and messages:
            text = "\n".join(
                f"{message.get('role', '?')}: {self._text_of(message)}"
                for message in messages)[:200000]
            self._capture([{"kind": "external_note", "payload": {
                "text": text, "note": "pre_compress_snapshot"}}])
        return ""

    def on_memory_write(self, action: str, target: str, content: str,
                        metadata: Optional[Dict[str, Any]] = None) -> None:
        if self._primary:
            self._capture([{"kind": "external_note", "payload": {
                "text": content, "note": "builtin_memory_write", "action": action,
                "target": target, "meta": metadata or {}}}])

    def on_delegation(self, task: str, result: str, *,
                      child_session_id: str = "", **kwargs) -> None:
        if self._primary:
            self._capture([{"kind": "delegation", "payload": {
                "task": task[:4000], "result": result[:4000],
                "child_session_id": child_session_id}}])

    def _persist_boundary(self, session_id: str, payload: dict) -> None:
        # Synchronous calls preserve capture-before-extract publication order.
        capture_id = self._capture(
            [{"kind": "session_end", "payload": payload}], session_id)
        if capture_id is not None:
            self._persist_mutation("/extract", {"session_id": session_id})

    def on_session_switch(self, new_session_id: str, *, parent_session_id: str = "",
                          reset: bool = False, rewound: bool = False, **kwargs) -> None:
        old = self._session_id
        self._session_id = new_session_id
        self._prefetch_cache.pop(old, None)
        if reset and old and self._primary:
            self._persist_boundary(old, {"reason": "reset"})

    def on_session_end(self, messages: List[Dict[str, Any]]) -> None:
        if self._primary:
            self._persist_boundary(self._session_id, {"turns": len(messages)})

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        return [
            {"name": "memoryd_search",
             "description": "Search memoryd for relevant long-term memory.",
             "parameters": {"type": "object", "properties": {
                 "query": {"type": "string"}}, "required": ["query"]}},
            {"name": "memoryd_report_miss",
             "description": "Durably queue a report that memoryd missed context.",
             "parameters": {"type": "object", "properties": {
                 "detail": {"type": "string"}}, "required": ["detail"]}},
        ]

    def handle_tool_call(self, tool_name: str, args: Dict[str, Any], **kwargs) -> str:
        if tool_name == "memoryd_search":
            packet = self._recall(args.get("query", ""), self._session_id, timeout=5.0)
            if packet is None:
                return json.dumps({"ok": False, "error": "memoryd unreachable"})
            return json.dumps({"ok": True,
                               "memory": packet or "(nothing relevant found)"})
        if tool_name == "memoryd_report_miss":
            if not self._primary:
                return json.dumps({"ok": False, "queued": False,
                                   "error": "read-only agent context"})
            job_id = self._persist_mutation(
                "/miss", {"session_id": self._session_id,
                          "signal": "user_said_forgot",
                          "detail": {"note": args.get("detail", "")}})
            return json.dumps({"ok": job_id is not None, "queued": job_id is not None,
                               "request_id": job_id})
        raise NotImplementedError(tool_name)

    def get_config_schema(self) -> List[Dict[str, Any]]:
        return [
            {"key": "url", "description": "memoryd daemon URL",
             "default": DEFAULT_URL, "required": False},
            {"key": "project", "description":
                "Fixed project label for this profile (default: hermes-<platform>)",
             "required": False},
        ]

    def save_config(self, values: Dict[str, Any], hermes_home: str) -> None:
        path = Path(hermes_home) / "memoryd.json"
        _private_dir(path.parent)
        _atomic_json(path, {key: value for key, value in values.items() if value},
                     replace=True)

    def backup_paths(self) -> List[str]:
        # The durable spool is inside active HERMES_HOME and is already included
        # by Hermes backup; only paths outside HERMES_HOME belong in this list.
        return []

    @staticmethod
    def _text_of(message: Dict[str, Any]) -> str:
        content = message.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return " ".join(block.get("text", "") for block in content
                            if isinstance(block, dict) and block.get("type") == "text")
        return ""

    def _record_durability_fault(self, message: str) -> None:
        self._durability_fault = message
        self._durability_notice_pending = True
        warning = f"memoryd capture durability fault: {message}"
        logger.warning(warning)
        print(warning, file=sys.stderr)
        if self._spool_store is not None:
            with contextlib.suppress(OSError, JobCollision):
                self._spool_store.set_fault(message)

    def _persist_mutation(self, endpoint: str, body: dict) -> Optional[str]:
        if not self._primary:
            return None
        with self._lifecycle_mutation_lock:
            if self._shutdown_requested.is_set() or self._stop.is_set():
                self._durability_fault = "provider is shut down; mutation was not queued"
                self._durability_notice_pending = True
                logger.warning("memoryd mutation rejected after shutdown: %s", endpoint)
                return None
            if self._spool_store is None:
                self._record_durability_fault("durable spool is unavailable")
                return None
            try:
                job_id = self._spool_store.persist(endpoint, body)
            except (OSError, ValueError, TypeError, JobCollision) as exc:
                self._record_durability_fault(f"could not persist {endpoint}: {exc}")
                return None
            self._wake.set()
            return job_id

    def _recall(self, prompt: str, session_id: str, timeout: float) -> Optional[str]:
        result = self._request_json(
            "/recall", {"prompt": prompt, "session_id": session_id,
                        "project": self._project, "agent": AGENT_NAME}, timeout)
        if result.kind != "success" or result.payload is None:
            return None
        return result.payload.get("markdown", "")

    def _request_json(self, path: str, body: dict, timeout: float) -> HttpResult:
        try:
            request = urllib.request.Request(
                f"{self._url}{path}", data=_canonical_json(body),
                headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(request, timeout=timeout) as response:
                status = getattr(response, "status", response.getcode())
                raw = response.read()
            if not 200 <= status <= 299:
                kind = "retry" if _retryable_status(status) else "permanent"
                return HttpResult(kind, None, f"HTTP {status}")
            if not raw:
                return HttpResult("retry", None, "invalid JSON response: empty body")
            try:
                payload = json.loads(raw)
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                return HttpResult("retry", None, f"invalid JSON response: {exc}")
            if not isinstance(payload, dict):
                return HttpResult("retry", None, "invalid JSON response: expected object")
            return HttpResult("success", payload, "")
        except urllib.error.HTTPError as exc:
            try:
                detail = exc.read().decode("utf-8", "replace")[:2000]
            except Exception:  # noqa: BLE001
                detail = str(exc)
            kind = "retry" if _retryable_status(exc.code) else "permanent"
            return HttpResult(kind, None, f"HTTP {exc.code}: {detail}")
        except (urllib.error.URLError, TimeoutError, OSError, ValueError,
                http.client.HTTPException) as exc:
            kind = "permanent" if isinstance(exc, ValueError) else "retry"
            return HttpResult(kind, None, f"network/configuration failure: {exc}")

    def _send_mutation(self, job: dict) -> HttpResult:
        return self._request_json(job["endpoint"], job["body"], timeout=5.0)

    def _release_for_shutdown(self, path: Path, job: dict) -> bool:
        try:
            self._spool_store.release(path, job)
        except (OSError, ValueError, json.JSONDecodeError, JobCollision) as exc:
            self._shutdown_handback_error = f"shutdown handback failed: {exc}"
            return False
        return True

    def _process_one(self) -> bool:
        if self._spool_store is None:
            return False
        try:
            with self._lifecycle_mutation_lock:
                if self._shutdown_requested.is_set() or self._stop.is_set():
                    return False
                claimed = self._spool_store.claim_oldest()
                if claimed is None:
                    fault = self._spool_store.fault()
                    if fault and fault != self._durability_fault:
                        self._durability_fault = fault
                        self._durability_notice_pending = True
                    return False
            path, job = claimed
            with self._lifecycle_mutation_lock:
                if self._shutdown_requested.is_set() or self._stop.is_set():
                    return self._release_for_shutdown(path, job)
            result = self._send_mutation(job)
            with self._lifecycle_mutation_lock:
                if self._shutdown_requested.is_set() or self._stop.is_set():
                    # Replay is safe even if the response was actually accepted:
                    # the stable request_id is daemon-idempotent.
                    return self._release_for_shutdown(path, job)
                if result.kind == "success":
                    self._spool_store.complete(path)
                elif result.kind == "permanent":
                    self._spool_store.dead_letter(path, job, result.error)
                    self._durability_notice_pending = True
                    logger.warning("memoryd mutation dead-lettered: %s", result.error)
                    print(f"memoryd mutation dead-lettered: {result.error}", file=sys.stderr)
                else:
                    self._spool_store.retry(path, job, result.error)
            return True
        except (OSError, ValueError, json.JSONDecodeError, JobCollision) as exc:
            self._record_worker_fault(f"spool worker failure: {exc}")
            return False

    def _record_worker_fault(self, message: str) -> None:
        with self._lifecycle_mutation_lock:
            if self._shutdown_requested.is_set() or self._stop.is_set():
                return
            self._record_durability_fault(message)

    def _drain(self) -> None:
        while not self._stop.is_set():
            try:
                if self._process_one():
                    continue
            except Exception as exc:  # noqa: BLE001 - worker must never die silently
                self._record_worker_fault(f"unexpected spool worker failure: {exc}")
            self._wake.wait(0.25)
            self._wake.clear()


def register(ctx) -> None:
    """Entry point called by Hermes plugin discovery."""
    ctx.register_memory_provider(MemorydProvider())
