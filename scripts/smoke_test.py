#!/usr/bin/env python3
"""Smoke test: schema firebreaks + capture->ledger + recall packet + canary.

Run against a fresh memoryd database with the daemon running on :7437.
Exit 0 only if every check passes.
"""
from __future__ import annotations

import json
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.stdout.reconfigure(encoding="utf-8")  # ✓/✗ on cp1252 Windows consoles

import psycopg  # noqa: E402
from memoryd.core import CFG, append_event, new_id, pool  # noqa: E402

PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    (PASS if ok else FAIL).append(name + (f" — {detail}" if detail and not ok else ""))
    print(("  ✓ " if ok else "  ✗ ") + name + (f" — {detail}" if detail and not ok else ""))


def http(path: str, body: dict | None = None, method: str = "POST") -> tuple[int, dict]:
    req = urllib.request.Request(
        f"http://127.0.0.1:{CFG.port}{path}",
        data=json.dumps(body or {}).encode() if method == "POST" else None,
        headers={"Content-Type": "application/json"}, method=method)
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def main() -> int:
    print("== 1. schema firebreaks ==")
    with pool().connection() as conn:
        sid = "smoketest-" + new_id("s")[-8:]
        evt = append_event(conn, kind="user_message", session_id=sid,
                           project="smoketest", payload={"text": "hello"})
        conn.commit()

        # ledger append-only
        try:
            conn.execute("UPDATE events SET kind='veto' WHERE id=%s", (evt,))
            conn.commit()
            check("events append-only (UPDATE rejected)", False, "update succeeded!")
        except psycopg.Error:
            conn.rollback()
            check("events append-only (UPDATE rejected)", True)
        try:
            conn.execute("DELETE FROM events WHERE id=%s", (evt,))
            conn.commit()
            check("events append-only (DELETE rejected)", False, "delete succeeded!")
        except psycopg.Error:
            conn.rollback()
            check("events append-only (DELETE rejected)", True)

        # Ariadne gate: active without source must fail
        mid = new_id("mem")
        try:
            conn.execute(
                """INSERT INTO memories (id,type,text,authority,confidence,status)
                   VALUES (%s,'preference','sourceless active','A1',0.9,'active')""", (mid,))
            conn.commit()
            check("Ariadne gate (active w/o source rejected)", False, "insert succeeded!")
        except psycopg.Error:
            conn.rollback()
            check("Ariadne gate (active w/o source rejected)", True)

        # proper path: candidate -> add source -> promote to active
        conn.execute(
            """INSERT INTO memories (id,type,text,project,authority,confidence,status)
               VALUES (%s,'directive','Do not modify the indicator backfill cron without asking — it broke prod twice.',
                       'smoketest','A1',0.95,'candidate')""", (mid,))
        conn.execute("INSERT INTO memory_sources (memory_id,event_id) VALUES (%s,%s)", (mid, evt))
        conn.execute("UPDATE memories SET status='active' WHERE id=%s", (mid,))
        conn.commit()
        check("promotion with source succeeds", True)

        # text immutability once active
        try:
            conn.execute("UPDATE memories SET text='edited!' WHERE id=%s", (mid,))
            conn.commit()
            check("active text immutable", False, "edit succeeded!")
        except psycopg.Error:
            conn.rollback()
            check("active text immutable", True)

        # supersession side-effect
        old = new_id("mem")
        new = new_id("mem")
        for m, txt in ((old, "Alex prefers Hermes for coding agents."),
                       (new, "Alex distrusts Hermes as a memory layer; executor only.")):
            conn.execute(
                """INSERT INTO memories (id,type,text,project,authority,confidence,status)
                   VALUES (%s,'preference',%s,'smoketest','A1',0.9,'candidate')""", (m, txt))
            conn.execute("INSERT INTO memory_sources (memory_id,event_id) VALUES (%s,%s)", (m, evt))
        conn.execute("UPDATE memories SET status='active' WHERE id IN (%s,%s)", (old, new))
        conn.execute("INSERT INTO supersessions (old_id,new_id,reason) VALUES (%s,%s,'memory distrust')",
                     (old, new))
        row = conn.execute("SELECT status, valid_to FROM memories WHERE id=%s", (old,)).fetchone()
        conn.commit()
        check("supersession auto-marks old memory", row[0] == "superseded" and row[1] is not None,
              f"got {row}")

        # seed hot + canary for recall test
        for mtype, txt, canary in (
            ("preference", "Alex prefers short factual commit messages.", False),
            ("project_state", "smoketest project: recall pipeline under verification.", False),
            # canary deliberately shares terms with the recall prompt so it IS
            # retrieved by FTS — proving the court catches it, not luck
            ("technical_fact", "CANARY-SEALED backfill cron indicator: must never surface.", True),
        ):
            m = new_id("mem")
            conn.execute(
                """INSERT INTO memories (id,type,text,project,authority,confidence,status,is_canary,sensitivity)
                   VALUES (%s,%s,%s,%s,'A1',0.9,'candidate',%s,%s)""",
                (m, mtype, txt, None if mtype == "preference" else "smoketest",
                 canary, "normal"))
            conn.execute("INSERT INTO memory_sources (memory_id,event_id) VALUES (%s,%s)", (m, evt))
            conn.execute("UPDATE memories SET status='active' WHERE id=%s", (m,))
        conn.commit()

    print("== 2. capture -> archive + ledger ==")
    transcript = [
        {"type": "user", "timestamp": "2026-07-07T10:00:00Z",
         "message": {"content": [{"type": "text", "text": "fix the backfill cron please"}]}},
        {"type": "assistant", "timestamp": "2026-07-07T10:00:05Z",
         "message": {"content": [{"type": "tool_use", "name": "Bash", "input": {"command": "crontab -l"}}]}},
        {"type": "assistant", "timestamp": "2026-07-07T10:00:12Z",
         "message": {"content": [{"type": "text", "text": "I checked but did not modify the cron, per your directive."}]}},
    ]
    with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as f:
        for line in transcript:
            f.write(json.dumps(line) + "\n")
        tpath = f.name

    code, resp = http("/capture", {"transcript_path": tpath, "session_id": "smoketest-s2",
                                   "project": "smoketest", "trigger": "session_end"})
    check("capture returns 202", code == 202, str(resp))
    time.sleep(1.0)  # async worker

    with pool().connection() as conn:
        n = conn.execute("SELECT count(*) FROM events WHERE session_id='smoketest-s2' AND NOT meta").fetchone()[0]
        check("ledger events written from transcript", n == 3, f"got {n}")
        # idempotency: capture same transcript again
        http("/capture", {"transcript_path": tpath, "session_id": "smoketest-s2",
                          "project": "smoketest", "trigger": "session_end"})
        time.sleep(1.0)
        n2 = conn.execute("SELECT count(*) FROM events WHERE session_id='smoketest-s2' AND NOT meta").fetchone()[0]
        check("re-ingestion is idempotent", n2 == n, f"{n} -> {n2}")
        sha = conn.execute(
            "SELECT raw_sha256 FROM events WHERE session_id='smoketest-s2' AND NOT meta LIMIT 1").fetchone()[0]
        blob = CFG.archive / "objects" / "sha256" / sha[:2] / sha[2:4] / sha
        check("raw blob archived, content-addressed", blob.exists())

    print("== 3. recall packet ==")
    code, pkt = http("/recall", {"prompt": "should I modify the backfill cron for the indicator job?",
                                 "session_id": "smoketest-s3", "project": "smoketest"})
    check("recall returns 200", code == 200, str(pkt)[:200])
    md = pkt.get("markdown", "")
    check("packet includes directive lane", "backfill cron" in md and "directives" in md.lower())
    check("packet includes hot prefs", "commit messages" in md)
    check("packet cites mem_ ids", "mem_" in md)
    check("canary NEVER surfaces", "CANARY" not in md)
    check("canary alarm raised", pkt.get("canary_alarms"), "no alarm recorded")
    check("recall latency < 700ms", pkt.get("latency_ms", 9999) < 700,
          f"{pkt.get('latency_ms')}ms")

    print("== 4. health + miss signal ==")
    code, h = http("/health", method="GET")
    check("health ok", code == 200 and h.get("ok") is True)
    code, _ = http("/miss", {"session_id": "smoketest-s3", "signal": "manual",
                             "detail": {"note": "test"}})
    check("miss signal accepted", code == 200)

    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
