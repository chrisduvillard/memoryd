"""Extractor (M3): one LLM call per session -> validated, promoted memories.

Pipeline per spec §5:
  gather non-meta session events
  -> LLM proposes typed candidates (JSON contract)
  -> VALIDATOR enforces the contract deterministically (the model is not trusted)
  -> CHAPERONE shapes/holds malformed candidates
  -> PROMOTION assigns status (quorum-lite, stingy per P6)
  -> dedup bumps confirmation; contradictions open reviews, never overwrite.

Design stance: the LLM proposes, the code disposes. Every guarantee lives in
the validator, so a worse model degrades recall quality, never integrity.
"""
from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta, timezone

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from .core import append_event, new_id, pool
from .llm import LLMError, get_client

VALID_TYPES = {
    "identity", "preference", "writing_style", "project_state", "decision",
    "open_question", "commitment", "person", "company", "technical_fact",
    "workflow", "constraint", "procedure", "directive", "warning", "priming",
}
AUTO_ACTIVE_TYPES = {"directive", "decision", "constraint", "commitment"}

HEDGES = re.compile(r"\b(might|maybe|perhaps|considering|could|thinking about|"
                    r"not sure|possibly|leaning|tempted|later)\b", re.I)
COMMITTAL = re.compile(r"\b(will|decided|is going to|chose|has chosen|must|"
                       r"always|definitely)\b", re.I)

SYSTEM_PROMPT = """You extract durable memories from an agent-session transcript.

Return ONLY a JSON array. Each element:
{
 "type": one of [identity, preference, writing_style, project_state, decision,
         open_question, commitment, person, company, technical_fact, workflow,
         constraint, procedure, directive, warning, priming],
 "text": one well-formed, fully-scoped sentence or short paragraph,
 "struct": {},            // REQUIRED for directive: {"directive","condition","expires","severity"}
                          // REQUIRED for warning:   {"class","target","severity"}
                          // decision: {"options","chosen","rationale"}
 "project": string|null,  // null = global
 "scope": "work_private"|"project_shared"|"personal_private"|"public",
 "sensitivity": "public"|"normal"|"private"|"sealed",
 "authority_claim": "A1"|"A2"|"B1"|"C1"|"D1",
 "confidence": 0..1,
 "activation": {"task_type":[],"audience":[],"exclude":[]},
 "source_event_ids": [ids from the transcript — REQUIRED, must be real],
 "evidence_quote": "verbatim snippet from a cited event (REQUIRED for A1)",
 "duplicate_of": "mem_id or null",   // if it restates an EXISTING memory below
 "contradicts": ["mem_id", ...]      // existing memories this conflicts with
}

Hard rules:
- PRESERVE HEDGES. "might switch to Qdrant" extracts as *considering, no
  decision made* — never as a decision or commitment.
- A1 only for direct explicit user statements, with evidence_quote.
- Extract FEW, DURABLE items. Session chatter, one-off details, and anything
  already covered by an existing memory (use duplicate_of) should not become
  new candidates. Zero candidates is a valid answer.
- Never invent source_event_ids.
- warnings: failed attempts, fragile files, user-stated boundaries.
- directives: explicit standing instructions from the user."""


# ------------------------------------------------------------------ gather

def _session_context(conn, session_id: str) -> tuple[list[dict], str | None]:
    rows = conn.execute(
        """SELECT id, ts, kind, project, payload FROM events
           WHERE session_id=%s AND NOT meta ORDER BY ts""", (session_id,)).fetchall()
    project = next((r["project"] for r in rows if r["project"]), None)
    return rows, project


def _existing_memories(conn, project: str | None) -> list[dict]:
    return conn.execute(
        """SELECT id, type, text, status FROM memories
           WHERE status IN ('active','candidate')
             AND (project IS NULL OR project = %s)
           ORDER BY created_at DESC LIMIT 60""", (project,)).fetchall()


def _render_transcript(events: list[dict]) -> str:
    lines = []
    for e in events:
        p = e["payload"] or {}
        body = p.get("text") or p.get("summary") or json.dumps(p)[:300]
        lines.append(f"[{e['id']}] {e['kind']}: {body}")
    return "\n".join(lines)[:60000]


# ------------------------------------------------------------------ validate

def _validate(cand: dict, event_ids: set[str], source_texts: dict[str, str]) -> tuple[str, str]:
    """Return (verdict, reason): verdict in {ok, hold, reject}."""
    if cand.get("type") not in VALID_TYPES:
        return "reject", f"invalid type {cand.get('type')!r}"
    text = (cand.get("text") or "").strip()
    if not text:
        return "reject", "empty text"
    srcs = cand.get("source_event_ids") or []
    if not srcs:
        return "reject", "no source events (Ariadne rule)"
    fake = [s for s in srcs if s not in event_ids]
    if fake:
        return "reject", f"invented source ids: {fake}"
    try:
        conf = float(cand.get("confidence", -1))
    except (TypeError, ValueError):
        return "reject", "bad confidence"
    if not 0 <= conf <= 1:
        return "reject", "confidence out of range"

    # authority cap + A1 evidence requirement (deterministic downgrade)
    claim = cand.get("authority_claim", "D1")
    if claim not in {"A1", "A2", "B1", "C1", "D1"}:
        claim = "D1"
    if claim == "A1":
        quote = (cand.get("evidence_quote") or "").strip()
        cited = " \n ".join(source_texts.get(s, "") for s in srcs)
        if not quote or quote.lower() not in cited.lower():
            cand["authority_claim"] = "A2"  # downgrade, don't reject
    # struct requirements
    struct = cand.get("struct") or {}
    if cand["type"] == "directive" and not {"directive", "severity"} <= struct.keys():
        return "hold", "directive missing struct fields"
    if cand["type"] == "warning" and not {"class", "severity"} <= struct.keys():
        return "hold", "warning missing struct fields"
    # hedge preservation: cited sources hedge, candidate commits -> hold
    cited = " ".join(source_texts.get(s, "") for s in srcs)
    if HEDGES.search(cited) and COMMITTAL.search(text):
        cw = set(re.findall(r"[a-z]{4,}", text.lower()))
        hw = set(re.findall(r"[a-z]{4,}", cited.lower()))
        if len(cw & hw) >= 2:
            return "hold", "possible hedge violation (source hedges, candidate commits)"
    return "ok", ""


# ------------------------------------------------------------------ promote

def _promote(cand: dict) -> str:
    """Quorum-lite per spec §5.3. Stingy by design (P6)."""
    if cand.get("scope") == "untrusted_external" or cand.get("authority_claim") == "Q":
        return "quarantined"
    if cand["type"] == "identity" and not cand.get("project"):
        return "candidate"  # never auto-active; review row opens promotion_request
    if cand["type"] == "priming":
        return "active"  # time-boxed below
    if cand.get("authority_claim") == "A1" and cand["type"] in AUTO_ACTIVE_TYPES:
        return "active"
    return "candidate"


# ------------------------------------------------------------------ main

def run_extraction(session_id: str) -> dict:
    client = get_client()
    if client is None:
        return {"ok": False, "skipped": "no LLM configured (capture-only mode)"}

    with pool().connection() as conn:
        conn.row_factory = dict_row
        events, project = _session_context(conn, session_id)
        if not events:
            return {"ok": False, "error": "no events for session"}
        already = conn.execute(
            """SELECT 1 FROM events WHERE session_id=%s AND kind='extraction_run'
               AND payload->>'ok' = 'true' LIMIT 1""", (session_id,)).fetchone()
        if already:
            return {"ok": True, "skipped": "already extracted"}

        existing = _existing_memories(conn, project)
        user_msg = (
            "EXISTING MEMORIES (for duplicate_of / contradicts):\n"
            + "\n".join(f"[{m['id']}] ({m['type']},{m['status']}) {m['text']}" for m in existing)
            + "\n\nTRANSCRIPT EVENTS:\n" + _render_transcript(events)
        )
        try:
            raw = client.complete(SYSTEM_PROMPT, user_msg)
        except LLMError as e:
            append_event(conn, kind="extraction_run", session_id=session_id, project=project,
                         meta=True, payload={"ok": False, "error": str(e)[:500]})
            conn.commit()
            return {"ok": False, "error": str(e)}

        # tolerate fenced output
        raw = re.sub(r"^```(json)?|```$", "", raw.strip(), flags=re.M).strip()
        try:
            candidates = json.loads(raw)
            assert isinstance(candidates, list)
        except (json.JSONDecodeError, AssertionError):
            append_event(conn, kind="extraction_run", session_id=session_id, project=project,
                         meta=True, payload={"ok": False, "error": "unparseable LLM output"})
            conn.commit()
            return {"ok": False, "error": "unparseable LLM output"}

        event_ids = {e["id"] for e in events}
        source_texts = {e["id"]: json.dumps(e["payload"]) for e in events}
        existing_ids = {m["id"] for m in existing}
        stats = {"ok": True, "proposed": len(candidates), "stored": 0, "held": 0,
                 "rejected": 0, "dedup": 0, "contradictions": 0, "statuses": {}}

        for cand in candidates:
            if not isinstance(cand, dict):
                stats["rejected"] += 1
                continue
            verdict, reason = _validate(cand, event_ids, source_texts)
            if verdict == "reject":
                stats["rejected"] += 1
                continue

            # dedup: repeat mention affirms + bumps confirmation (feeds quorum)
            dup = cand.get("duplicate_of")
            if dup and dup in existing_ids:
                conn.execute(
                    "INSERT INTO treatments (memory_id, kind, by_ref, note) "
                    "VALUES (%s,'affirmed',%s,'restated in later session')",
                    (dup, session_id))
                conn.execute(
                    "UPDATE memories SET last_confirmed_at=now() WHERE id=%s", (dup,))
                stats["dedup"] += 1
                continue

            mid = new_id("mem")
            status = "candidate" if verdict == "hold" else _promote(cand)
            valid_to = (date.today() + timedelta(days=1)) if cand["type"] == "priming" else None
            conn.execute(
                """INSERT INTO memories (id,type,text,struct,project,scope,sensitivity,
                                         authority,confidence,status,valid_to,activation)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,'candidate',%s,%s)""",
                (mid, cand["type"], cand["text"].strip(), Jsonb(cand.get("struct") or {}),
                 cand.get("project") or project, cand.get("scope", "work_private"),
                 cand.get("sensitivity", "normal"), cand.get("authority_claim", "D1"),
                 float(cand["confidence"]), valid_to,
                 Jsonb(cand.get("activation") or {})))
            for s in cand["source_event_ids"]:
                conn.execute(
                    "INSERT INTO memory_sources (memory_id,event_id) VALUES (%s,%s) "
                    "ON CONFLICT DO NOTHING", (mid, s))
            # embed at write time so both active and candidate lanes are
            # vector-searchable immediately (M5)
            try:
                from .embed import get_embedder, to_pgvector
                emb = get_embedder()
                vec = emb.embed([cand["text"].strip()])[0]
                conn.execute(
                    "INSERT INTO mem_embeddings (memory_id, model, embedding) "
                    "VALUES (%s,%s,%s::vector) ON CONFLICT (memory_id) DO UPDATE "
                    "SET model=EXCLUDED.model, embedding=EXCLUDED.embedding",
                    (mid, emb.model, to_pgvector(vec)))
            except Exception:  # noqa: BLE001 — embeddings are disposable; microsleep backfills
                pass
            if status != "candidate":
                conn.execute("UPDATE memories SET status=%s::mem_status WHERE id=%s",
                             (status, mid))
                append_event(conn, kind="promotion", session_id=session_id, project=project,
                             payload={"memory_id": mid, "status": status,
                                      "type": cand["type"]})
            if verdict == "hold":
                conn.execute(
                    "INSERT INTO review_queue (kind, memory_id, detail) "
                    "VALUES ('chaperone_hold', %s, %s)",
                    (mid, Jsonb({"reason": reason})))
                stats["held"] += 1
            if cand["type"] == "identity" and not cand.get("project"):
                conn.execute(
                    "INSERT INTO review_queue (kind, memory_id, detail) "
                    "VALUES ('promotion_request', %s, %s)",
                    (mid, Jsonb({"note": "identity-tier: requires user confirmation "
                                          "(two-person rule)"})))
            # contradictions: reviews + questioned treatment, NEVER overwrite
            for old in (cand.get("contradicts") or []):
                if old in existing_ids:
                    conn.execute(
                        "INSERT INTO treatments (memory_id, kind, by_ref, note) "
                        "VALUES (%s,'questioned',%s,'contradicted by new candidate')",
                        (old, mid))
                    conn.execute(
                        "INSERT INTO review_queue (kind, memory_id, detail) "
                        "VALUES ('contradiction', %s, %s)",
                        (old, Jsonb({"new_memory": mid})))
                    stats["contradictions"] += 1
            stats["stored"] += 1
            stats["statuses"][mid] = status

        append_event(conn, kind="extraction_run", session_id=session_id, project=project,
                     meta=True, payload={k: v for k, v in stats.items() if k != "statuses"})
        conn.commit()
        return stats
