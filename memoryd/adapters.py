"""Stable adapter event envelope.

Runtime adapters should translate their native event shapes here, then the
daemon can treat Claude, Hermes, Codex, MCP, or future runtimes uniformly.
"""
from __future__ import annotations

from typing import Any


def _preview(payload: dict[str, Any]) -> str:
    text = payload.get("text") or payload.get("summary") or ""
    if not isinstance(text, str):
        text = str(text)
    return text[:4000]


def event_to_envelope(event: dict[str, Any], *, runtime: str, parent_session_id: str = "") -> dict[str, Any]:
    payload = event.get("payload") or {}
    event_type = event.get("event_type") or event.get("kind") or "external_note"
    return {
        "agent": event.get("agent") or runtime,
        "runtime": runtime,
        "session_id": event.get("session_id", ""),
        "project": event.get("project"),
        "event_type": event_type,
        "content_ref": event.get("raw_sha256") or event.get("content_ref"),
        "inline_preview": event.get("inline_preview") or _preview(payload),
        "metadata": {
            **(event.get("metadata") or {}),
            "payload": payload,
            "source_kind": event.get("kind"),
        },
        "parent_session_id": event.get("parent_session_id") or parent_session_id,
    }
