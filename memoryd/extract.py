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
import os
import re
from datetime import date, datetime, timedelta, timezone

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from .contracts import VALID_TYPES as CONTRACT_TYPES, get_extractor_contract
from .core import append_event, new_id, pool
from .llm import LLMError, get_client
from .model_gateway import get_model_profile
from .semantic_policies import get_semantic_policy
from .source_pack import PackedSources, pack_session_events

VALID_TYPES = set(CONTRACT_TYPES)
AUTO_ACTIVE_TYPES = set(get_semantic_policy("conservative_v1").auto_active_types)

# Unrecalled, unconfirmed candidates decay after ~1 month (microsleep);
# NULL = no decay (identity tier, ARCHITECTURE §3.3).
CANDIDATE_HALF_LIFE_D = get_semantic_policy("conservative_v1").candidate_half_life_days

HEDGES = get_semantic_policy("conservative_v1").hedges
COMMITTAL = get_semantic_policy("conservative_v1").committal

SYSTEM_PROMPT = get_extractor_contract("builtin_v1").system_prompt


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
    return pack_session_events(events, max_chars=60000).text


def _pack_transcript(events: list[dict], *, profile, contract) -> PackedSources:
    default_chars = 60000
    if contract.source_packer == "wide_context_v1" or "long_context" in profile.capabilities:
        default_chars = min(profile.max_context_tokens * 4, 200000)
    max_chars = int(os.environ.get("MEMORYD_SOURCE_PACK_CHARS", str(default_chars)))
    return pack_session_events(
        events,
        max_chars=max_chars,
        include_archived=contract.source_packer == "wide_context_v1",
    )


def _record_model_run(conn, *, profile, operation: str, contract: str | None,
                      prompt: str, output: str = "", ok: bool = True,
                      error: str | None = None, metadata: dict | None = None) -> None:
    try:
        conn.execute(
            """INSERT INTO model_runs (id, profile, provider, model, operation,
                                      contract, prompt_chars, output_chars, ok,
                                      error, metadata)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (new_id("run"), profile.name, profile.provider, profile.model,
             operation, contract, len(prompt), len(output), ok,
             error[:1000] if error else None, Jsonb(metadata or {})))
    except Exception:  # noqa: BLE001 - old DBs may not have migration 005 yet
        pass


# ------------------------------------------------------------------ validate

def _validate(cand: dict, event_ids: set[str], source_texts: dict[str, str],
              user_texts: dict[str, str]) -> tuple[str, str]:
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

    # authority cap + A1 evidence requirement (deterministic downgrade).
    # Normalize IN PLACE: an un-normalized garbage claim (e.g. "A3") would
    # pass validation, then violate the memories.authority CHECK and abort
    # the whole extraction transaction.
    claim = cand.get("authority_claim", "D1")
    if claim not in {"A1", "A2", "B1", "C1", "D1", "Q"}:
        claim = "D1"
    cand["authority_claim"] = claim
    if claim == "A1":
        # A1 = direct explicit USER statement: the quote must come from a
        # cited user_message — agent text must not launder into top authority
        quote = (cand.get("evidence_quote") or "").strip()
        cited = " \n ".join(user_texts.get(s, "") for s in srcs)
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
    if get_semantic_policy().hedge_violation(cited, text):
        return "hold", "possible hedge violation (source hedges, candidate commits)"
    return "ok", ""


# ------------------------------------------------------------------ promote

def _promote(cand: dict) -> str:
    """Quorum-lite per spec §5.3. Stingy by design (P6)."""
    return get_semantic_policy().promote(cand)


# ------------------------------------------------------------------ main

def run_extraction(session_id: str) -> dict:
    profile = get_model_profile()
    contract = get_extractor_contract(
        os.environ.get("MEMORYD_EXTRACTOR_CONTRACT")
        or profile.preferred_extractor_contract)
    # Preserve legacy capture-only behavior: an inferred mock profile is only
    # metadata unless the user explicitly selected MEMORYD_MODEL_PROFILE=mock.
    client = get_client(profile if os.environ.get("MEMORYD_MODEL_PROFILE") else None)
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
        packed = _pack_transcript(events, profile=profile, contract=contract)
        user_msg = (
            "EXISTING MEMORIES (for duplicate_of / contradicts):\n"
            + "\n".join(f"[{m['id']}] ({m['type']},{m['status']}) {m['text']}" for m in existing)
            + "\n\nTRANSCRIPT EVENTS:\n" + packed.text
        )
        try:
            raw = client.complete(
                contract.system_prompt, user_msg, max_tokens=contract.max_output_tokens)
        except LLMError as e:
            _record_model_run(
                conn, profile=profile, operation="extract",
                contract=contract.name, prompt=user_msg, ok=False,
                error=str(e), metadata=packed.to_dict())
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
            _record_model_run(
                conn, profile=profile, operation="extract",
                contract=contract.name, prompt=user_msg, output=raw,
                ok=False, error="unparseable LLM output",
                metadata=packed.to_dict())
            append_event(conn, kind="extraction_run", session_id=session_id, project=project,
                         meta=True, payload={"ok": False, "error": "unparseable LLM output"})
            conn.commit()
            return {"ok": False, "error": "unparseable LLM output"}
        _record_model_run(
            conn, profile=profile, operation="extract", contract=contract.name,
            prompt=user_msg, output=raw, ok=True, metadata=packed.to_dict())

        event_ids = {e["id"] for e in events}
        source_texts = {e["id"]: json.dumps(e["payload"]) for e in events}
        user_texts = {e["id"]: json.dumps(e["payload"]) for e in events
                      if e["kind"] == "user_message"}
        existing_ids = {m["id"] for m in existing}
        stats = {"ok": True, "proposed": len(candidates), "stored": 0, "held": 0,
                 "rejected": 0, "dedup": 0, "contradictions": 0, "statuses": {}}

        for cand in candidates:
            if not isinstance(cand, dict):
                stats["rejected"] += 1
                continue
            verdict, reason = _validate(cand, event_ids, source_texts, user_texts)
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
                    "UPDATE memories SET last_confirmed_at=now(), "
                    "useful_count = useful_count + 1 WHERE id=%s", (dup,))
                stats["dedup"] += 1
                continue

            mid = new_id("mem")
            status = "candidate" if verdict == "hold" else _promote(cand)
            valid_to = (date.today() + timedelta(days=1)) if cand["type"] == "priming" else None
            semantic_policy = get_semantic_policy()
            conn.execute(
                """INSERT INTO memories (id,type,text,struct,project,scope,sensitivity,
                                         authority,confidence,status,valid_to,half_life_d,
                                         activation)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,'candidate',%s,%s,%s)""",
                (mid, cand["type"], cand["text"].strip(), Jsonb(cand.get("struct") or {}),
                 cand.get("project") or project, cand.get("scope", "work_private"),
                 cand.get("sensitivity", "normal"), cand.get("authority_claim", "D1"),
                 float(cand["confidence"]), valid_to,
                  None if cand["type"] == "identity" else semantic_policy.candidate_half_life_days,
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
                append_event(conn,
                             kind=("promotion" if status == "active" else "quarantine"),
                             session_id=session_id, project=project,
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
                     meta=True, payload={
                         **{k: v for k, v in stats.items() if k != "statuses"},
                         "model_profile": profile.name,
                         "extractor_contract": contract.name,
                         "source_pack": packed.to_dict(),
                     })
        conn.commit()
        return stats
