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

from .core import CFG, append_event, archive_bytes, pool


def _parse_ts(entry: dict) -> datetime:
    raw = entry.get("timestamp")
    if raw:
        try:
            return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        except ValueError:
            pass
    return datetime.now(timezone.utc)


def _block_text(block: dict) -> str | None:
    value = block.get("text")
    if value is None:
        value = block.get("content")
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return _text_of(value)
    return None


def _classify_all(entry: object) -> list[tuple[str, dict]]:
    if not isinstance(entry, dict):
        return []
    etype = entry.get("type")
    if etype not in ("user", "assistant"):
        return []
    message = entry.get("message")
    if not isinstance(message, dict):
        return []
    content = message.get("content")
    if not isinstance(content, list):
        if not isinstance(content, str) or not content:
            return []
        text = content[:4000]
        if etype == "user":
            return [("user_message", {"text": text})]
        return [("agent_response", {"text": text})]

    events: list[tuple[str, dict]] = []
    for block in content:
        if isinstance(block, str):
            kind = "user_message" if etype == "user" else "agent_response"
            events.append((kind, {"text": block[:4000]}))
        elif not isinstance(block, dict):
            continue
        elif block.get("type") == "text":
            kind = "user_message" if etype == "user" else "agent_response"
            block_text = _block_text(block)
            if block_text is None:
                continue
            text = block_text[:4000]
            if text:
                events.append((kind, {"text": text}))
        elif etype == "assistant" and block.get("type") == "tool_use":
            tool_input = block.get("input")
            tool_name = block.get("name")
            if (not isinstance(tool_input, dict) or
                    not isinstance(tool_name, str) or not tool_name or
                    any(not isinstance(key, str) for key in tool_input)):
                continue
            events.append(("tool_call", {"tools": [{
                "name": tool_name,
                "input_keys": sorted(tool_input),
            }]}))
        elif etype == "user" and block.get("type") == "tool_result":
            summary = _block_text(block)
            if summary is not None:
                events.append(("tool_result", {"summary": summary[:2000]}))
    return events


def _text_of(content: list) -> str | None:
    parts = []
    for b in content:
        if isinstance(b, dict) and b.get("type") == "text":
            text = b.get("text", "")
            if isinstance(text, str):
                parts.append(text)
        elif isinstance(b, str):
            parts.append(b)
    return "\n".join(parts) if parts else None


def capture_fonds_path(
        session_id: str, captured_at: datetime | str | None = None) -> str:
    if captured_at is None:
        captured = datetime.now(timezone.utc)
    elif isinstance(captured_at, datetime):
        captured = captured_at
    elif isinstance(captured_at, str):
        captured = datetime.fromisoformat(captured_at.replace("Z", "+00:00"))
    else:
        raise ValueError("captured_at must be a datetime, string, or null")
    if captured.tzinfo is None:
        captured = captured.replace(tzinfo=timezone.utc)
    captured = captured.astimezone(timezone.utc)
    normalized_session = session_id.replace("\\", "/")
    return f"claude-code/{captured:%Y/%m/%d}/{normalized_session}.jsonl"


def ingest_transcript(transcript_path: str, session_id: str, project: str | None,
                      trigger: str, *, ingest_job_id: str | None = None,
                      source_adapter: str = "claude-code",
                      captured_at: datetime | str | None = None,
                      transcript_bytes: bytes | None = None) -> dict:
    """Archive the transcript file and append ledger events for new lines."""
    path = Path(transcript_path).expanduser()
    if transcript_bytes is None:
        try:
            transcript_bytes = path.read_bytes()
        except OSError:
            return {"ok": False, "error": f"transcript not found: {path}"}
    elif not isinstance(transcript_bytes, bytes):
        raise ValueError("transcript_bytes must be bytes")

    fonds = capture_fonds_path(session_id, captured_at)
    sha = archive_bytes(
        transcript_bytes, "application/x-jsonl", fonds,
        ingest_job_id=ingest_job_id)

    new_events = 0
    with pool().connection() as conn:
        for line_no, raw_line in enumerate(
                transcript_bytes.decode(
                    encoding="utf-8", errors="replace").splitlines()):
            line = raw_line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            classified = _classify_all(entry)
            if not classified:
                continue
            if not isinstance(entry, dict):
                continue
            native = entry.get("uuid")
            base_id = (f"uuid:{native}" if native else
                       f"line:{line_no}:{hashlib.sha256(raw_line.encode()).hexdigest()}")
            ts = _parse_ts(entry)
            for ordinal, (kind, payload) in enumerate(classified):
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


def drain_spool() -> dict[str, int]:
    """Advance durable captures through processing, retry, or dead-letter."""
    from .spool import (
        PermanentSpoolError, claim_next, complete_job, dead_letter,
        gc_blob_if_unreferenced, load_job, release_job, requeue_stale,
        read_validated_blob, upgrade_legacy_job,
    )
    stats = {"processed": 0, "retried": 0, "dead_lettered": 0, "requeued": 0}
    stats["requeued"] = requeue_stale(CFG.spool)
    while job_path := claim_next(CFG.spool):
        raw: dict = {}
        attempts = 0
        try:
            value = json.loads(job_path.read_text(encoding="utf-8"))
            if not isinstance(value, dict):
                raise ValueError("invalid job manifest: expected object")
            raw = value
            if "schema_version" not in raw:
                if "extract_only" not in raw:
                    upgraded = upgrade_legacy_job(CFG.spool, job_path)
                    stats["dead_lettered"] += int(upgraded is None)
                    continue
                if raw["extract_only"] is not True:
                    raise PermanentSpoolError(
                        "invalid extract_only: expected boolean true")
                if (not isinstance(raw.get("session_id"), str) or
                        not raw["session_id"].strip()):
                    raise PermanentSpoolError("invalid session_id")
                if type(raw.get("attempts", 0)) is not int:
                    raise PermanentSpoolError("invalid attempts")
                attempts = raw.get("attempts", 0)
                from .extract import run_extraction
                result = run_extraction(raw["session_id"])
                if (not result.get("ok") and not result.get("skipped") and
                        result.get("error") != "no events for session"):
                    raise RuntimeError(
                        result.get("error", "extraction retry failed"))
                complete_job(job_path)
                stats["processed"] += 1
                continue
            job = load_job(job_path)
            attempts = job.get("attempts", 0)
            if job["kind"] == "extraction":
                from .extract import run_extraction
                result = run_extraction(job["session_id"])
                if (not result.get("ok") and not result.get("skipped") and
                        result.get("error") != "no events for session"):
                    raise RuntimeError(
                        result.get("error", "extraction retry failed"))
                complete_job(job_path)
                stats["processed"] += 1
                continue
            blob = read_validated_blob(CFG.spool, job)
            blob_path = CFG.spool / "blobs" / job["blob_sha256"]
            result = ingest_transcript(
                str(blob_path), job["session_id"], job.get("project"),
                job["trigger"], ingest_job_id=job["job_id"],
                source_adapter="claude-code", captured_at=job["created_at"],
                transcript_bytes=blob)
            if not result.get("ok"):
                raise RuntimeError(
                    result.get("error", "capture ingestion failed"))
            if job["trigger"] in ("session_end", "pre_compact"):
                from .extract import run_extraction
                extracted = run_extraction(job["session_id"])
                if (not extracted.get("ok") and not extracted.get("skipped") and
                        extracted.get("error") != "no events for session"):
                    raise RuntimeError(
                        extracted.get("error", "extraction failed"))
            complete_job(job_path)
            sha = result["sha256"]
            canonical = (CFG.archive / "objects" / "sha256" /
                         sha[:2] / sha[2:4] / sha)
            gc_blob_if_unreferenced(CFG.spool, sha, canonical)
            stats["processed"] += 1
        except (PermanentSpoolError, ValueError) as exc:
            dead_letter(CFG.spool, job_path, str(exc))
            stats["dead_lettered"] += 1
        except Exception as exc:  # noqa: BLE001 — transient work is retryable
            release_job(
                CFG.spool, job_path, str(exc)[:1000],
                delay_s=min(3600, 2 ** min(attempts + 1, 10)))
            stats["retried"] += 1
    return stats
