"""Transcript ingestion: Claude Code transcript JSONL -> archive + ledger.

Raw archival is step one and unconditional (spec §4.3). The parser is
deliberately tolerant: unknown line shapes are still archived inside the
raw blob; only ledger event extraction is best-effort. Re-running ingestion
on the same transcript is idempotent at the ledger level via a per-line
source identity derived from the transcript line.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from .core import CFG, append_event, archive_file, pool


def _parse_ts(entry: dict) -> datetime:
    raw = entry.get("timestamp")
    if raw:
        try:
            return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        except ValueError:
            pass
    return datetime.now(timezone.utc)


def _block_text(block: dict) -> str:
    value = block.get("text")
    if value is None:
        value = block.get("content", "")
    if isinstance(value, list):
        return _text_of(value)
    return str(value or "")


def _classify_all(entry: dict) -> list[tuple[str, dict]]:
    etype = entry.get("type")
    content = (entry.get("message") or {}).get("content")
    if not isinstance(content, list):
        text = str(content or "")[:4000]
        if not text:
            return []
        if etype == "user":
            return [("user_message", {"text": text})]
        if etype == "assistant":
            return [("agent_response", {"text": text})]
        return []

    events: list[tuple[str, dict]] = []
    for block in content:
        if isinstance(block, str):
            kind = "user_message" if etype == "user" else "agent_response"
            events.append((kind, {"text": block[:4000]}))
        elif not isinstance(block, dict):
            continue
        elif block.get("type") == "text":
            kind = "user_message" if etype == "user" else "agent_response"
            text = _block_text(block)[:4000]
            if text:
                events.append((kind, {"text": text}))
        elif etype == "assistant" and block.get("type") == "tool_use":
            events.append(("tool_call", {"tools": [{
                "name": block.get("name"),
                "input_keys": sorted((block.get("input") or {}).keys()),
            }]}))
        elif etype == "user" and block.get("type") == "tool_result":
            events.append(("tool_result", {"summary": _block_text(block)[:2000]}))
    return events


def _text_of(content: list) -> str:
    parts = []
    for b in content:
        if isinstance(b, dict) and b.get("type") == "text":
            parts.append(b.get("text", ""))
        elif isinstance(b, str):
            parts.append(b)
    return "\n".join(parts)


def ingest_transcript(transcript_path: str, session_id: str, project: str | None,
                      trigger: str, *, ingest_job_id: str | None = None,
                      source_adapter: str = "claude-code") -> dict:
    """Archive the transcript file and append ledger events for new lines."""
    path = Path(transcript_path).expanduser()
    if not path.exists():
        return {"ok": False, "error": f"transcript not found: {path}"}

    day = datetime.now(timezone.utc)
    fonds = f"claude-code/{day:%Y/%m/%d}/{session_id}.jsonl"
    sha = archive_file(path, fonds, mime="application/x-jsonl",
                       ingest_job_id=ingest_job_id)

    new_events = 0
    with pool().connection() as conn:
        for line_no, raw_line in enumerate(
                path.read_text(encoding="utf-8", errors="replace").splitlines()):
            line = raw_line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            native = entry.get("uuid")
            base_id = (f"uuid:{native}" if native else
                       f"line:{line_no}:{hashlib.sha256(raw_line.encode()).hexdigest()}")
            ts = _parse_ts(entry)
            for ordinal, (kind, payload) in enumerate(_classify_all(entry)):
                event_id = f"{base_id}:{ordinal}:{kind}"
                inserted = append_event(
                    conn, kind=kind, session_id=session_id, ts=ts,
                    project=project, raw_sha256=sha, payload=payload,
                    source_adapter=source_adapter,
                    source_event_id=event_id,
                    source_seq=line_no,
                    ingest_job_id=ingest_job_id,
                )
                new_events += int(inserted is not None)
        # ack only when there's something to acknowledge — a per-turn Stop
        # capture with zero new events would otherwise append one meta row
        # per idle turn. session_end/pre_compact always ack (microsleep's
        # extraction-retry query keys on those triggers).
        if new_events or trigger in ("session_end", "pre_compact"):
            append_event(
                conn, kind="capture_ack", session_id=session_id,
                project=project, raw_sha256=sha, meta=True,
                payload={"trigger": trigger, "new_events": new_events},
                source_adapter=source_adapter,
                source_event_id=f"capture_ack:{sha}:{trigger}",
                ingest_job_id=ingest_job_id,
            )
        conn.commit()
    return {"ok": True, "sha256": sha, "new_events": new_events}


def drain_spool() -> int:
    """Re-attempt captures spooled while the daemon or DB was down."""
    drained = 0
    for f in sorted(CFG.spool.glob("*.json")):
        try:
            job = json.loads(f.read_text())
            if job.get("extract_only"):
                # /extract jobs spooled by the capture worker carry no
                # transcript — retry the extraction itself.
                from .extract import run_extraction
                res = run_extraction(job["session_id"])
            else:
                res = ingest_transcript(job["transcript_path"], job["session_id"],
                                        job.get("project"), job.get("trigger", "spool"))
            if res.get("ok"):
                f.unlink()
                drained += 1
        except Exception:  # noqa: BLE001 — one bad job (e.g. LLM misconfig raising
            continue        # LLMError) must not abort the drain or the nightly run
    return drained
