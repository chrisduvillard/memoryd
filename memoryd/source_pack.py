"""Deterministic source packing for extraction and replay."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class PackedSources:
    text: str
    used_chars: int
    max_chars: int
    events: int
    omitted_events: int = 0
    included_blobs: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "used_chars": self.used_chars,
            "max_chars": self.max_chars,
            "events": self.events,
            "omitted_events": self.omitted_events,
            "included_blobs": self.included_blobs,
        }


def _payload_text(payload: dict[str, Any]) -> str:
    body = payload.get("text") or payload.get("summary")
    if body is None:
        body = json.dumps(payload, sort_keys=True, default=str)
    return str(body)


def pack_session_events(
    events: list[dict[str, Any]],
    *,
    max_chars: int,
    include_archived: bool = False,
) -> PackedSources:
    """Render events under a deterministic character budget.

    When include_archived is true, events with raw blobs may use archived text
    in future long-context profiles. The current implementation keeps this
    conservative and only pulls archived blobs for truncated text events.
    """
    lines: list[str] = []
    used = 0
    omitted = 0
    blobs: list[str] = []
    for event in events:
        payload = event.get("payload") or {}
        body = _payload_text(payload)
        sha = event.get("raw_sha256")
        if include_archived and payload.get("truncated") and sha:
            try:
                from .core import read_blob
                body = read_blob(sha).decode("utf-8", errors="replace")
                blobs.append(sha)
            except Exception:  # noqa: BLE001 - archive fetch failure should not kill extraction
                body = _payload_text(payload)
        line = f"[{event.get('id')}] {event.get('kind')}: {body}"
        if used + len(line) + (1 if lines else 0) > max_chars:
            omitted += 1
            continue
        lines.append(line)
        used += len(line) + (1 if len(lines) > 1 else 0)
    return PackedSources(
        text="\n".join(lines),
        used_chars=used,
        max_chars=max_chars,
        events=len(events) - omitted,
        omitted_events=omitted,
        included_blobs=blobs,
    )
