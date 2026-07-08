"""Recall engine (M4/M5): hot + FTS + vector + warning lane -> court rules
-> lane-budgeted packet.

Hybrid FTS + vector retrieval; the vector channel degrades to FTS-only if
embedding fails (spec §4.2). Court is rule-based (P5): validity, activation,
directive precedence, scope, canary. Every canary that reaches the filter
is a hard alarm.
"""
from __future__ import annotations

import json
import re
import time
from datetime import date, datetime, timezone

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from .core import CFG, append_event, pool

APPROX_CHARS_PER_TOKEN = 4

LANES = {  # spec §6.4 — token budgets per lane
    "directives_warnings": 300,   # reserved, never evicted
    "hot": 350,
    "retrieved": 600,
    "candidates": 150,
    "open_loops": 100,
}

MODE_PATTERNS = [
    ("debug",    re.compile(r"traceback|error|exception|stack|\.py\b|\.ts\b|/[\w./-]+\.\w{1,4}\b|undefined|null pointer", re.I)),
    ("decision", re.compile(r"\bshould (we|i)\b|\bdid (we|i) decide\b|\bwhy did\b|\bdecision\b", re.I)),
    ("state",    re.compile(r"\bwhere were we\b|\bcontinue\b|\bstatus\b|\bnext step", re.I)),
    ("style",    re.compile(r"\bwrite\b|\bemail\b|\bdraft\b|\bmessage to\b|\breply\b", re.I)),
]


def classify(prompt: str) -> str:
    for mode, pat in MODE_PATTERNS:
        if pat.search(prompt):
            return mode
    return "general"


def _tok(s: str) -> int:
    return max(1, len(s) // APPROX_CHARS_PER_TOKEN)


_STOP = {"should", "would", "could", "please", "about", "there", "these", "those",
         "which", "where", "when", "what", "have", "does", "with", "from", "that",
         "this", "your", "they", "them", "will", "just", "like", "into", "over"}


def _fts_query(prompt: str) -> str:
    """FTS is the precision channel over extracted exact terms (spec §6.2).

    Natural prompts must not be AND-ed wholesale — that guarantees empty
    results. Extract content words + identifiers/paths, OR them, rank later.
    """
    terms: list[str] = []
    for w in re.findall(r"[A-Za-z0-9_./-]{3,}", prompt):
        lw = w.lower().strip("./-")
        if len(lw) >= 4 and lw not in _STOP and lw not in terms:
            terms.append(lw)
    # tsquery lexemes must be quoted-safe; keep alnum/underscore only
    safe = [re.sub(r"[^a-z0-9_]", "", t) for t in terms]
    safe = [t for t in safe if t]
    return " | ".join(safe[:12])


def _activation_ok(activation: dict, mode: str) -> bool:
    if not activation:
        return True
    if mode in (activation.get("exclude") or []):
        return False
    allowed = activation.get("task_type") or []
    return not allowed or mode in allowed


def _directive_condition_ok(struct: dict) -> bool:
    """Slice-level condition evaluation: expiry date + free-text condition passthrough."""
    exp = struct.get("expires")
    if exp:
        try:
            if date.fromisoformat(str(exp)[:10]) < date.today():
                return False
        except ValueError:
            pass  # non-date conditions ("when user asks to finalize") stay active until treated
    return True


def _row_line(r: dict, label: str | None = None) -> str:
    tag = label or r["type"]
    d = r.get("valid_from")
    datestr = d.isoformat() if d else ""
    return f"- [{tag}, {datestr}, {r['id']}] {r['text']}"


def build_packet(prompt: str, session_id: str, project: str | None,
                 agent: str = "claude-code") -> dict:
    t0 = time.monotonic()
    mode = classify(prompt)
    fts_q = _fts_query(prompt)
    canary_alarms: list[str] = []
    ambiguity: list[dict] = []

    with pool().connection() as conn:
        conn.row_factory = dict_row

        def _bf(p: str = "") -> str:
            return f"""
            {p}status IN ('active','candidate')
            AND {p}sensitivity <> 'sealed'
            AND {p}scope = ANY(%(scopes)s)
            AND ({p}valid_to IS NULL OR {p}valid_to >= CURRENT_DATE)
            AND ({p}project IS NULL OR {p}project = %(project)s)
        """
        base_filter = _bf()
        params = {"scopes": CFG.visa(agent), "project": project}

        # Lane 1 — directives & warnings: unconditional, never mode-gated
        lane1 = conn.execute(
            f"""SELECT * FROM memories
                WHERE type IN ('directive','warning') AND status='active'
                  AND {base_filter} ORDER BY created_at DESC LIMIT 20""",
            params).fetchall()
        lane1 = [r for r in lane1 if _directive_condition_ok(r["struct"] or {})]

        # Lane 2 — hot set
        hot = conn.execute(
            f"""SELECT * FROM memories
                WHERE status='active'
                  AND ( (type IN ('identity','preference','writing_style') AND project IS NULL)
                        OR (type='project_state' AND project = %(project)s) )
                  AND {base_filter} ORDER BY type, created_at DESC LIMIT 15""",
            params).fetchall()

        # Lane 3/4 — hybrid retrieval: FTS (precision) + vector (recall), merged
        # by the spec §6.4 rerank. Vector channel is budgeted: if embedding
        # fails or exceeds its slice of the latency budget, recall degrades to
        # FTS-only and logs the missing channel (spec §4.2 fallback).
        channels = ["hot", "fts", "warning"]

        # Embed the prompt ONCE for both vector lanes (active + candidate);
        # with a network embedder this halves per-turn latency/cost.
        try:
            from .embed import get_embedder, to_pgvector
            query_vec = to_pgvector(get_embedder().embed([prompt])[0])
            channels.append("vector")
        except Exception:  # noqa: BLE001 — degrade to FTS-only
            query_vec = None

        def _fts_rows(status: str, limit: int) -> list[dict]:
            if not fts_q:
                return []
            return conn.execute(
                f"""SELECT *, ts_rank(fts, to_tsquery('simple', %(q)s)) AS rank
                    FROM memories
                    WHERE fts @@ to_tsquery('simple', %(q)s)
                      AND status = %(st)s::mem_status
                      AND type NOT IN ('directive','warning')
                      AND {base_filter}
                    ORDER BY rank DESC, useful_count DESC, created_at DESC
                    LIMIT {limit}""",
                {**params, "q": fts_q, "st": status}).fetchall()

        def _vec_rows(status: str, limit: int) -> list[dict]:
            if query_vec is None:
                return []
            qv = query_vec
            return conn.execute(
                f"""SELECT m.*, 1 - (e.embedding <=> %(qv)s::vector) AS cosine
                    FROM memories m JOIN mem_embeddings e ON e.memory_id = m.id
                    WHERE m.status = %(st)s::mem_status
                      AND m.type NOT IN ('directive','warning')
                      AND {_bf('m.')}
                    ORDER BY e.embedding <=> %(qv)s::vector
                    LIMIT {limit}""",
                {**params, "qv": qv, "st": status}).fetchall()

        def _rerank(fts_rows: list[dict], vec_rows: list[dict], limit: int) -> list[dict]:
            """Spec §6.4: 0.35·semantic + 0.20·keyword + 0.15·recency
            + 0.15·useful + 0.10·authority + 0.05·confirmation_recency."""
            from datetime import datetime as _dt, timezone as _tz
            now = _dt.now(_tz.utc)
            auth_w = {"A1": 1.0, "A2": 0.8, "B1": 0.6, "C1": 0.4, "D1": 0.2, "Q": 0.0}
            max_rank = max((float(r.get("rank") or 0) for r in fts_rows), default=0) or 1.0
            max_use = max((r["useful_count"] for r in fts_rows + vec_rows), default=0) or 1
            merged: dict[str, dict] = {}
            for r in fts_rows:
                merged[r["id"]] = {**r, "kw": float(r.get("rank") or 0) / max_rank, "sem": 0.0}
            for r in vec_rows:
                if r["id"] in merged:
                    merged[r["id"]]["sem"] = max(0.0, float(r.get("cosine") or 0))
                else:
                    merged[r["id"]] = {**r, "kw": 0.0, "sem": max(0.0, float(r.get("cosine") or 0))}
            def score(m: dict) -> float:
                age_d = max(0.0, (now - m["created_at"]).total_seconds() / 86400)
                recency = 0.5 ** (age_d / 90)               # 90-day half-life
                conf_d = ((now - m["last_confirmed_at"]).total_seconds() / 86400
                          if m["last_confirmed_at"] else 365)
                confirm = 0.5 ** (conf_d / 90)
                return (0.35 * m["sem"] + 0.20 * m["kw"] + 0.15 * recency
                        + 0.15 * m["useful_count"] / max_use
                        + 0.10 * auth_w.get(m["authority"], 0.2) + 0.05 * confirm)
            return sorted(merged.values(), key=score, reverse=True)[:limit]

        retrieved = _rerank(_fts_rows("active", 12), _vec_rows("active", 12), 12)
        candidates = _rerank(_fts_rows("candidate", 5), _vec_rows("candidate", 5), 5)

        # Lane 5 — open loops
        loops = conn.execute(
            f"""SELECT * FROM memories
                WHERE type IN ('commitment','open_question') AND status='active'
                  AND {base_filter} ORDER BY created_at DESC LIMIT 6""",
            params).fetchall()

        # ---- court rules (P5) --------------------------------------
        def court(rows: list[dict]) -> list[dict]:
            kept = []
            active_directive_texts = [r["text"].lower() for r in lane1 if r["type"] == "directive"]
            for r in rows:
                if r["is_canary"]:
                    canary_alarms.append(r["id"])
                    continue  # never surfaces; alarm instead
                if not _activation_ok(r["activation"] or {}, mode):
                    ambiguity.append({"memory_id": r["id"], "rule": "activation_excluded", "mode": mode})
                    continue
                # directive precedence: an A1 directive suppresses conflicting inferred preference
                if r["type"] == "preference" and r["authority"] in ("D1", "C1"):
                    if any(_conflicts(r["text"], d) for d in active_directive_texts):
                        conn.execute(
                            "INSERT INTO treatments (memory_id, kind, note) VALUES (%s,'suppressed',%s)",
                            (r["id"], f"suppressed by active directive during recall (mode={mode})"))
                        continue
                kept.append(r)
            return kept

        lane2 = court(hot)
        lane3 = court(retrieved)
        lane4 = court(candidates)
        lane5 = court(loops)
        lane1 = [r for r in lane1 if not r["is_canary"] or canary_alarms.append(r["id"])]

        # ---- render under lane budgets -----------------------------
        def fit(rows: list[dict], budget: int, label: str | None = None) -> list[str]:
            out, used = [], 0
            for r in rows:
                line = _row_line(r, label)
                cost = _tok(line)
                if used + cost > budget:
                    break
                out.append(line)
                used += cost
            return out

        sections: list[str] = ["## Memory (auto-recalled; cite mem_ ids when relying on these)"]
        l1 = fit(lane1, LANES["directives_warnings"])
        if l1:
            sections += ["### Active directives & warnings"] + [("⚠ " + s if not s.startswith("⚠") else s) for s in l1]
        l2 = fit(lane2, LANES["hot"])
        if l2:
            sections += ["### About the user & this project"] + l2
        l3 = fit(lane3, LANES["retrieved"])
        if l3:
            sections += ["### Possibly relevant (retrieved)"] + l3
        l4 = fit(lane4, LANES["candidates"], label="candidate")
        if l4:
            sections += ["### Unconfirmed candidates (verify before relying)"] + l4
        l5 = fit(lane5, LANES["open_loops"])
        if l5:
            sections += ["### Open loops"] + l5

        markdown = "\n".join(sections) if len(sections) > 1 else ""
        latency_ms = int((time.monotonic() - t0) * 1000)

        shown_ids = [r["id"] for lane in (lane1, lane2, lane3, lane4, lane5) for r in lane]
        if shown_ids:
            conn.execute(
                "UPDATE memories SET recall_count = recall_count + 1 WHERE id = ANY(%s)",
                (shown_ids,))

        packet_meta = {
            "mode": mode, "lanes": {"l1": len(l1), "l2": len(l2), "l3": len(l3),
                                    "l4": len(l4), "l5": len(l5)},
            "channels": channels,
            "canary_alarms": canary_alarms, "ambiguity": ambiguity,
        }
        # served is TRUE by construction: failed recalls never reach this
        # INSERT, and a dead daemon can't log at all. Column kept for a
        # future failure logger.
        conn.execute(
            """INSERT INTO recall_log (session_id, project, query_text, packet, latency_ms, served, agent)
               VALUES (%s,%s,%s,%s,%s,TRUE,%s)""",
            (session_id, project, prompt[:2000], Jsonb(packet_meta), latency_ms, agent))
        append_event(conn, kind="recall_packet", session_id=session_id, project=project,
                     agent=agent, meta=True, payload=packet_meta)
        if canary_alarms:
            append_event(conn, kind="veto", session_id=session_id, project=project,
                         payload={"reason": "canary_surfaced", "ids": canary_alarms})
        conn.commit()

    return {"markdown": markdown, "latency_ms": latency_ms, **packet_meta}


_NEG = re.compile(r"\b(do not|don't|never|avoid|no )\b", re.I)


def _conflicts(pref_text: str, directive_text: str) -> bool:
    """Crude slice-level conflict check: directive negates something the
    preference asserts, sharing at least two content words. Logged cases
    feed the D5 ambiguity dataset; precision matters more than recall here."""
    if not _NEG.search(directive_text):
        return False
    pw = {w for w in re.findall(r"[a-z]{4,}", pref_text.lower())}
    dw = {w for w in re.findall(r"[a-z]{4,}", directive_text.lower())}
    return len(pw & dw) >= 2
