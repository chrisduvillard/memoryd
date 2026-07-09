#!/usr/bin/env python3
"""M3 extraction test — mock LLM, real DB. Exercises every validator path:

  good directive (A1 + quote)      -> ACTIVE (auto-promote type)
  hedged preference kept hedged    -> CANDIDATE
  hedge-violating over-commitment  -> CANDIDATE + chaperone_hold review
  invented source id               -> REJECTED
  A1 claim without valid quote     -> downgraded to A2, candidate
  identity (global)                -> CANDIDATE + promotion_request review
  duplicate_of existing            -> no new row; 'affirmed' treatment + confirm bump
  contradicts existing             -> 'questioned' treatment + contradiction review,
                                      old memory NOT superseded automatically
  idempotency                      -> second run skips ('already extracted')
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.stdout.reconfigure(encoding="utf-8")  # ✓/✗ on cp1252 Windows consoles

os.environ["MEMORYD_LLM"] = "mock"
MOCK = Path(tempfile.gettempdir()) / "mock_llm.json"
os.environ["MEMORYD_LLM_MOCK_FILE"] = str(MOCK)

from psycopg.rows import dict_row  # noqa: E402
from memoryd.core import append_event, new_id, pool  # noqa: E402
from memoryd.extract import run_extraction  # noqa: E402

PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    (PASS if ok else FAIL).append(name)
    print(("  ✓ " if ok else "  ✗ ") + name + (f" — {detail}" if detail and not ok else ""))


def main() -> int:
    sid = "extract-test-" + new_id("s")[-6:]
    with pool().connection() as conn:
        conn.row_factory = dict_row
        # seed session events
        e1 = append_event(conn, kind="user_message", session_id=sid, project="acme-app",
                          payload={"text": "Never push directly to main on acme-app, always PR."})
        e2 = append_event(conn, kind="user_message", session_id=sid, project="acme-app",
                          payload={"text": "I might switch to Qdrant later for vectors, not sure yet."})
        e3 = append_event(conn, kind="user_message", session_id=sid, project="acme-app",
                          payload={"text": "By the way I am Alex, a data engineer in Berlin."})
        e4 = append_event(conn, kind="agent_response", session_id=sid, project="acme-app",
                          payload={"text": "I set up the CI pipeline to use a three-stage cache."})
        # pre-existing memories for dedup + contradiction
        dup_target = new_id("mem")
        old_belief = new_id("mem")
        seed_evt = append_event(conn, kind="user_message", session_id="seed",
                                payload={"text": "seed"})
        for mid, text in ((dup_target, "Alex prefers short factual commit messages."),
                          (old_belief, "acme-app uses a single-model review flow for /goal.")):
            conn.execute(
                """INSERT INTO memories (id,type,text,project,authority,confidence,status)
                   VALUES (%s,'preference',%s,'acme-app','A1',0.9,'candidate')""",
                (mid, text))
            conn.execute("INSERT INTO memory_sources (memory_id,event_id) VALUES (%s,%s)",
                         (mid, seed_evt))
            conn.execute("UPDATE memories SET status='active' WHERE id=%s", (mid,))
        conn.commit()

    candidates = [
        {  # 1: good A1 directive -> active
            "type": "directive",
            "text": "On acme-app, never push directly to main; always go through a PR.",
            "struct": {"directive": "no_direct_push_to_main", "condition": "repo==acme-app",
                       "expires": None, "severity": "high"},
            "project": "acme-app", "scope": "work_private", "sensitivity": "normal",
            "authority_claim": "A1", "confidence": 0.95,
            "activation": {}, "source_event_ids": [e1],
            "evidence_quote": "Never push directly to main",
            "duplicate_of": None, "contradicts": [],
        },
        {  # 2: hedged preference correctly hedged -> candidate
            "type": "preference",
            "text": "Alex is considering Qdrant as a future vector backend; no decision has been made.",
            "struct": {}, "project": None, "scope": "work_private", "sensitivity": "normal",
            "authority_claim": "A1", "confidence": 0.8,
            "activation": {"task_type": ["agent-architecture"]},
            "source_event_ids": [e2],
            "evidence_quote": "might switch to Qdrant later",
            "duplicate_of": None, "contradicts": [],
        },
        {  # 3: hedge violation -> hold
            "type": "decision",
            "text": "Alex decided he will switch to Qdrant for vectors.",
            "struct": {"options": ["pgvector", "Qdrant"], "chosen": "Qdrant", "rationale": ""},
            "project": None, "scope": "work_private", "sensitivity": "normal",
            "authority_claim": "A2", "confidence": 0.7,
            "activation": {}, "source_event_ids": [e2],
            "evidence_quote": "", "duplicate_of": None, "contradicts": [],
        },
        {  # 4: invented source -> reject
            "type": "technical_fact", "text": "Fabricated fact with fake source.",
            "struct": {}, "project": None, "scope": "work_private", "sensitivity": "normal",
            "authority_claim": "D1", "confidence": 0.5, "activation": {},
            "source_event_ids": ["evt_FAKE123"], "evidence_quote": "",
            "duplicate_of": None, "contradicts": [],
        },
        {  # 5: A1 claim, no valid quote -> downgraded A2
            "type": "commitment",
            "text": "Alex committed to reviewing the PR flow.",
            "struct": {}, "project": "acme-app", "scope": "work_private",
            "sensitivity": "normal", "authority_claim": "A1", "confidence": 0.6,
            "activation": {}, "source_event_ids": [e1],
            "evidence_quote": "this quote does not exist in the source",
            "duplicate_of": None, "contradicts": [],
        },
        {  # 6: identity -> candidate + promotion_request
            "type": "identity",
            "text": "Alex is a data engineer based in Berlin.",
            "struct": {}, "project": None, "scope": "personal_private",
            "sensitivity": "private", "authority_claim": "A1", "confidence": 0.9,
            "activation": {}, "source_event_ids": [e3],
            "evidence_quote": "I am Alex, a data engineer in Berlin",
            "duplicate_of": None, "contradicts": [],
        },
        {  # 7: duplicate -> affirm existing
            "type": "preference",
            "text": "Alex prefers short factual commit messages.",
            "struct": {}, "project": "acme-app", "scope": "work_private",
            "sensitivity": "normal", "authority_claim": "A2", "confidence": 0.9,
            "activation": {}, "source_event_ids": [e1], "evidence_quote": "",
            "duplicate_of": dup_target, "contradicts": [],
        },
        {  # 8: contradiction -> review, no auto-supersede
            "type": "technical_fact",
            "text": "acme-app's /goal command now uses a second AI model to review output.",
            "struct": {}, "project": "acme-app", "scope": "work_private",
            "sensitivity": "normal", "authority_claim": "A2", "confidence": 0.8,
            "activation": {}, "source_event_ids": [e1], "evidence_quote": "",
            "duplicate_of": None, "contradicts": [old_belief],
        },
        {  # 9: untrusted (Q) claim -> quarantined + 'quarantine' ledger event
            "type": "technical_fact",
            "text": "Claimed fact from an untrusted external source.",
            "struct": {}, "project": "acme-app", "scope": "work_private",
            "sensitivity": "normal", "authority_claim": "Q", "confidence": 0.4,
            "activation": {}, "source_event_ids": [e1], "evidence_quote": "",
            "duplicate_of": None, "contradicts": [],
        },
        {  # 10: A1 claim whose quote comes from an AGENT turn -> downgraded A2
            #     (A1 = direct explicit USER statement only)
            "type": "technical_fact",
            "text": "The acme-app CI pipeline uses a three-stage cache.",
            "struct": {}, "project": "acme-app", "scope": "work_private",
            "sensitivity": "normal", "authority_claim": "A1", "confidence": 0.8,
            "activation": {}, "source_event_ids": [e4],
            "evidence_quote": "use a three-stage cache",
            "duplicate_of": None, "contradicts": [],
        },
        {  # 11: out-of-enum scope/sensitivity from the model -> clamped, still
            #     stored. Regression: raw values violate the memories CHECK and
            #     would abort the whole extraction transaction (poison pill).
            "type": "technical_fact",
            "text": "The acme-app deploy uses a blue-green rollout across two clusters.",
            "struct": {}, "project": "acme-app", "scope": "internal-only",
            "sensitivity": "confidential", "authority_claim": "A2", "confidence": 0.7,
            "activation": {}, "source_event_ids": [e4], "evidence_quote": "",
            "duplicate_of": None, "contradicts": [],
        },
    ]
    MOCK.write_text(json.dumps(candidates))

    print("== extraction run ==")
    stats = run_extraction(sid)
    check("extraction ok", stats.get("ok") is True, str(stats))
    check("proposed 11", stats.get("proposed") == 11, str(stats))
    check("1 rejected (fake source)", stats.get("rejected") == 1, str(stats))
    check("1 dedup affirmation", stats.get("dedup") == 1, str(stats))
    check("1 held (hedge violation)", stats.get("held") == 1, str(stats))
    check("1 contradiction review", stats.get("contradictions") == 1, str(stats))
    check("9 stored", stats.get("stored") == 9, str(stats))

    with pool().connection() as conn:
        conn.row_factory = dict_row

        d = conn.execute("SELECT status, authority FROM memories WHERE type='directive' "
                         "AND text LIKE '%%never push directly%%'").fetchone()
        check("directive auto-promoted active", d and d["status"] == "active", str(d))

        h = conn.execute("SELECT status FROM memories WHERE type='preference' "
                         "AND text LIKE '%%considering Qdrant%%'").fetchone()
        check("hedged preference stays candidate", h and h["status"] == "candidate", str(h))

        v = conn.execute("SELECT m.status FROM memories m JOIN review_queue q "
                         "ON q.memory_id=m.id AND q.kind='chaperone_hold' "
                         "WHERE m.text LIKE '%%will switch to Qdrant%%'").fetchone()
        check("hedge violation held for review", v and v["status"] == "candidate", str(v))

        c5 = conn.execute("SELECT authority, status FROM memories "
                          "WHERE type='commitment'").fetchone()
        check("bogus A1 downgraded to A2", c5 and c5["authority"] == "A2", str(c5))
        check("downgraded item NOT auto-active", c5 and c5["status"] == "candidate", str(c5))

        i = conn.execute("SELECT m.status FROM memories m JOIN review_queue q "
                         "ON q.memory_id=m.id AND q.kind='promotion_request' "
                         "WHERE m.type='identity'").fetchone()
        check("identity gated behind promotion_request", i and i["status"] == "candidate", str(i))

        t = conn.execute("SELECT count(*) AS n FROM treatments WHERE memory_id=%s "
                         "AND kind='affirmed'", (dup_target,)).fetchone()
        check("duplicate affirmed existing memory", t["n"] == 1, str(t))
        lc = conn.execute("SELECT last_confirmed_at FROM memories WHERE id=%s",
                          (dup_target,)).fetchone()
        check("dedup bumped last_confirmed_at", lc["last_confirmed_at"] is not None)

        qm = conn.execute("SELECT status FROM memories "
                          "WHERE text LIKE '%%untrusted external source%%'").fetchone()
        check("Q claim quarantined", qm and qm["status"] == "quarantined", str(qm))

        ag = conn.execute("SELECT authority, status FROM memories "
                          "WHERE text LIKE '%%three-stage cache%%'").fetchone()
        check("agent-sourced A1 downgraded to A2", ag and ag["authority"] == "A2", str(ag))
        check("agent-sourced fact not auto-active", ag and ag["status"] == "candidate", str(ag))

        pp = conn.execute("SELECT scope, sensitivity, status FROM memories "
                          "WHERE text LIKE '%%blue-green rollout%%'").fetchone()
        check("out-of-enum scope/sensitivity clamped (no poison pill)",
              bool(pp) and pp["scope"] == "work_private" and pp["sensitivity"] == "normal",
              str(pp))

        qe = conn.execute("SELECT count(*) AS n FROM events WHERE session_id=%s "
                          "AND kind='quarantine'", (sid,)).fetchone()
        check("quarantine ledger event written", qe["n"] == 1, str(qe))

        ob = conn.execute("SELECT status FROM memories WHERE id=%s", (old_belief,)).fetchone()
        check("contradicted memory NOT auto-superseded", ob["status"] == "active", str(ob))
        q = conn.execute("SELECT count(*) AS n FROM review_queue WHERE kind='contradiction' "
                         "AND memory_id=%s", (old_belief,)).fetchone()
        check("contradiction review opened", q["n"] == 1, str(q))

    print("== idempotency ==")
    again = run_extraction(sid)
    check("second run skipped", again.get("skipped") == "already extracted", str(again))

    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
