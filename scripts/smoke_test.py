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
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.stdout.reconfigure(encoding="utf-8")  # ✓/✗ on cp1252 Windows consoles

import psycopg  # noqa: E402
from memoryd.core import CFG, append_event, new_id, pool  # noqa: E402
from memoryd.ingest import ingest_transcript  # noqa: E402

DSN = CFG.dsn
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


def check_durable_transcript_replay() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        mixed_session = f"smoke-mixed-{new_id('s')}"
        mixed = Path(temp_dir) / "mixed.jsonl"
        mixed.write_text(json.dumps({
            "uuid": "stable-mixed-line",
            "type": "assistant",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "message": {"content": [
                {"type": "text", "text": "kept answer"},
                {"type": "tool_use", "name": "shell", "input": {"command": "pwd"}},
            ]},
        }) + "\n", encoding="utf-8")

        first = ingest_transcript(
            str(mixed), mixed_session, "smoketest", "session_end")
        session_replay = ingest_transcript(
            str(mixed), mixed_session, "smoketest", "session_end")
        pre_compact = ingest_transcript(
            str(mixed), mixed_session, "smoketest", "pre_compact")
        pre_compact_replay = ingest_transcript(
            str(mixed), mixed_session, "smoketest", "pre_compact")

        check("synchronous mixed ingest reports both insertions",
              first["new_events"] == 2, str(first))
        check("exact session-end replay reports zero insertions",
              session_replay["new_events"] == 0, str(session_replay))
        check("pre-compact transcript replays report zero insertions",
              pre_compact["new_events"] == 0
              and pre_compact_replay["new_events"] == 0,
              f"{pre_compact}, {pre_compact_replay}")

        with psycopg.connect(DSN) as conn:
            event_rows = conn.execute(
                """SELECT kind, source_adapter, source_event_id, source_seq
                   FROM events WHERE session_id=%s AND NOT meta
                   ORDER BY source_event_id""",
                (mixed_session,)).fetchall()
            ack_rows = conn.execute(
                """SELECT payload->>'trigger', count(*),
                          bool_and(source_adapter IS NOT NULL
                                   AND source_event_id IS NOT NULL)
                   FROM events
                   WHERE session_id=%s AND kind='capture_ack'
                   GROUP BY payload->>'trigger'""",
                (mixed_session,)).fetchall()

        kinds = [row[0] for row in event_rows]
        check("mixed transcript preserves text and tool call",
              kinds == ["agent_response", "tool_call"], str(kinds))
        expected_provenance = [
            ("agent_response", "claude-code",
             "uuid:stable-mixed-line:0:agent_response", 0),
            ("tool_call", "claude-code",
             "uuid:stable-mixed-line:1:tool_call", 0),
        ]
        check("stable source provenance is recorded",
              event_rows == expected_provenance, str(event_rows))
        acknowledgements = {
            trigger: (count, has_provenance)
            for trigger, count, has_provenance in ack_rows
        }
        check("replayed capture acknowledgements are source-idempotent",
              acknowledgements == {
                  "session_end": (1, True),
                  "pre_compact": (1, True),
              }, str(acknowledgements))


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

    capture_session = f"smoketest-s2-{int(time.time())}"
    code, resp = http("/capture", {"transcript_path": tpath, "session_id": capture_session,
                                   "project": "smoketest", "trigger": "session_end"})
    check("capture returns 202", code == 202, str(resp))
    time.sleep(1.0)  # async worker

    with pool().connection() as conn:
        n = conn.execute(
            "SELECT count(*) FROM events WHERE session_id=%s AND NOT meta",
            (capture_session,)).fetchone()[0]
        check("ledger events written from transcript", n == 3, f"got {n}")
        sha = conn.execute(
            "SELECT raw_sha256 FROM events WHERE session_id=%s AND NOT meta LIMIT 1",
            (capture_session,)).fetchone()[0]
        blob = CFG.archive / "objects" / "sha256" / sha[:2] / sha[2:4] / sha
        check("raw blob archived, content-addressed", blob.exists())

    check_durable_transcript_replay()

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
