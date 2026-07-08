"""Transcript ingestion: Claude Code transcript JSONL -> archive + ledger.

Raw archival is step one and unconditional (spec §4.3). The parser is
deliberately tolerant: unknown line shapes are still archived inside the
raw blob; only ledger event extraction is best-effort. Re-running ingestion
on the same transcript is idempotent at the ledger level via a per-line
content check against already-ingested barcodes for the session.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import psycopg

from .core import CFG, append_event, archive_file, pool


def _parse_ts(entry: dict) -> datetime:
    raw = entry.get("timestamp")
    if raw:
        try:
            return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        except ValueError:
            pass
    return datetime.now(timezone.utc)


def _classify(entry: dict) -> tuple[str, dict] | None:
    """Map a transcript line to (event_kind, small_payload) or None to skip."""
    etype = entry.get("type")
    msg = entry.get("message") or {}
    if etype == "user":
        content = msg.get("content")
        if isinstance(content, list):
            # tool_result blocks come back on user turns
            if any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
                return "tool_result", {"summary": _text_of(content)[:2000]}
            return "user_message", {"text": _text_of(content)[:4000]}
        return "user_message", {"text": str(content)[:4000]}
    if etype == "assistant":
        content = msg.get("content")
        if isinstance(content, list):
            tools = [b for b in content if isinstance(b, dict) and b.get("type") == "tool_use"]
            if tools:
                return "tool_call", {
                    "tools": [{"name": t.get("name"), "input_keys": sorted((t.get("input") or {}).keys())}
                              for t in tools][:10]
                }
            return "agent_response", {"text": _text_of(content)[:4000]}
        return "agent_response", {"text": str(content)[:4000]}
    return None  # summaries, meta lines etc. live in the raw blob only


def _text_of(content: list) -> str:
    parts = []
    for b in content:
        if isinstance(b, dict) and b.get("type") == "text":
            parts.append(b.get("text", ""))
        elif isinstance(b, str):
            parts.append(b)
    return "\n".join(parts)


def ingest_transcript(transcript_path: str, session_id: str, project: str | None,
                      trigger: str) -> dict:
    """Archive the transcript file and append ledger events for new lines."""
    path = Path(transcript_path).expanduser()
    if not path.exists():
        return {"ok": False, "error": f"transcript not found: {path}"}

    day = datetime.now(timezone.utc)
    fonds = f"claude-code/{day:%Y/%m/%d}/{session_id}.jsonl"
    sha = archive_file(path, fonds, mime="application/x-jsonl")

    new_events = 0
    with pool().connection() as conn:
        seen: set[str] = {
            r[0] for r in conn.execute(
                "SELECT barcode FROM events WHERE session_id = %s", (session_id,)
            )
        }
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            mapped = _classify(entry)
            if mapped is None:
                continue
            kind, payload = mapped
            ts = _parse_ts(entry)
            # cheap idempotency probe: same ts+kind+payload hash -> same barcode
            import hashlib
            content_hash = hashlib.sha256(
                json.dumps(payload, sort_keys=True, default=str).encode()
            ).hexdigest()
            probe = f"{ts.strftime('%Y%m%dT%H%M%S')}|{session_id[:8]}|{kind}|{content_hash[:8]}"
            if probe in seen:
                continue
            append_event(conn, kind=kind, session_id=session_id, ts=ts,
                         project=project, raw_sha256=sha, payload=payload)
            seen.add(probe)
            new_events += 1
        append_event(conn, kind="capture_ack", session_id=session_id,
                     project=project, raw_sha256=sha, meta=True,
                     payload={"trigger": trigger, "new_events": new_events})
        conn.commit()
    return {"ok": True, "sha256": sha, "new_events": new_events}


def drain_spool() -> int:
    """Re-attempt captures spooled while the daemon or DB was down."""
    drained = 0
    for f in sorted(CFG.spool.glob("*.json")):
        try:
            job = json.loads(f.read_text())
            res = ingest_transcript(job["transcript_path"], job["session_id"],
                                    job.get("project"), job.get("trigger", "spool"))
            if res.get("ok"):
                f.unlink()
                drained += 1
        except (json.JSONDecodeError, KeyError, psycopg.Error):
            continue  # leave in spool for next drain
    return drained
