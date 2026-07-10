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


def drain_spool() -> dict[str, int]:
    """Advance durable captures through processing, retry, or dead-letter."""
    from .spool import (
        PermanentSpoolError, claim_next, complete_job, dead_letter,
        gc_blob_if_unreferenced, load_job, release_job, requeue_stale,
        upgrade_legacy_job, validate_blob,
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
            attempts = int(raw.get("attempts", 0))
            if raw.get("extract_only"):
                from .extract import run_extraction
                result = run_extraction(raw["session_id"])
                if (not result.get("ok") and not result.get("skipped") and
                        result.get("error") != "no events for session"):
                    raise RuntimeError(
                        result.get("error", "extraction retry failed"))
                complete_job(job_path)
                stats["processed"] += 1
                continue
            if raw.get("schema_version") != 2:
                upgraded = upgrade_legacy_job(CFG.spool, job_path)
                stats["dead_lettered"] += int(upgraded is None)
                continue
            job = load_job(job_path)
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
            blob = validate_blob(CFG.spool, job)
            result = ingest_transcript(
                str(blob), job["session_id"], job.get("project"), job["trigger"],
                ingest_job_id=job["job_id"], source_adapter="claude-code")
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
