#!/usr/bin/env python3
"""Hermes provider integration test — verbatim upstream ABC, live daemon.

Exercises the full provider lifecycle:
  ABC conformance (instantiable against the real MemoryProvider base)
  initialize (primary) -> session_start in ledger with agent='hermes'
  sync_turn -> user/agent events captured; long text archived + truncated
  queue_prefetch/prefetch -> packet served from cache; agent-visa applied
    (personal_private memory NEVER appears under the hermes visa)
  first-turn prefetch (no cache) -> bounded sync recall works
  tool calls: memoryd_search returns memory; memoryd_report_miss logs signal
  on_memory_write -> external_note mirrored (vendor cache observed)
  on_delegation -> delegation event captured
  on_pre_compress -> snapshot captured before context death
  SUBAGENT context -> writes skipped entirely
  on_session_end -> flush + extraction_run event (mock LLM)
  daemon-down fail-open -> visible marker once, spool, recovery flush
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts" / "_stubs"))   # verbatim upstream ABC
sys.path.insert(0, str(REPO / "hermes_plugin"))        # the plugin package
sys.path.insert(0, str(REPO))

os.environ["MEMORYD_LLM"] = "mock"
MOCK = Path("/tmp/mock_llm_hermes.json")
MOCK.write_text(json.dumps([]))  # extractor may legitimately find nothing
os.environ["MEMORYD_LLM_MOCK_FILE"] = str(MOCK)

from psycopg.rows import dict_row  # noqa: E402
from memoryd.core import append_event, new_id, pool  # noqa: E402
from memoryd import __init__ as _  # noqa: E402,F401

sys.path.insert(0, str(REPO / "hermes_plugin" / "memoryd"))
import importlib  # noqa: E402
plugin = importlib.import_module("memoryd")  # hermes_plugin/memoryd/__init__.py
# disambiguate: the plugin module defines MemorydProvider; the daemon pkg doesn't
if not hasattr(plugin, "MemorydProvider"):
    spec = importlib.util.spec_from_file_location(
        "hermes_memoryd_plugin", REPO / "hermes_plugin" / "memoryd" / "__init__.py")
    plugin = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(plugin)

from agent.memory_provider import MemoryProvider  # noqa: E402

PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    (PASS if ok else FAIL).append(name)
    print(("  ✓ " if ok else "  ✗ ") + name + (f" — {detail}" if detail and not ok else ""))


def wait_for(predicate, seconds: float = 5.0) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.1)
    return False


def q1(sql: str, *args):
    with pool().connection() as conn:
        conn.row_factory = dict_row
        return conn.execute(sql, args).fetchone()


def main() -> int:
    hermes_home = Path("/tmp/hermes_home_test")
    hermes_home.mkdir(exist_ok=True)
    (hermes_home / "memoryd.json").write_text(json.dumps(
        {"url": "http://127.0.0.1:7437", "project": "hermes-test"}))

    print("== ABC conformance ==")
    prov = plugin.MemorydProvider()
    check("implements upstream MemoryProvider ABC", isinstance(prov, MemoryProvider))
    check("no abstract methods left", not getattr(prov, "__abstractmethods__", None))
    check("is_available without network", prov.is_available() is True)

    print("== seed visa-test memories ==")
    with pool().connection() as conn:
        evt = append_event(conn, kind="user_message", session_id="hermes-seed",
                           payload={"text": "seed"})
        for mid, scope, text in (
            (new_id("mem"), "work_private",
             "The acme-app /goal reviewer uses OpenRouter as planned backend."),
            (new_id("mem"), "personal_private",
             "PRIVATE-FAMILY: upcoming family holiday planning details."),
        ):
            conn.execute(
                """INSERT INTO memories (id,type,text,project,scope,authority,confidence,status)
                   VALUES (%s,'technical_fact',%s,'hermes-test',%s,'A1',0.9,'candidate')""",
                (mid, text, scope))
            conn.execute("INSERT INTO memory_sources (memory_id,event_id) VALUES (%s,%s)",
                         (mid, evt))
            conn.execute("UPDATE memories SET status='active' WHERE id=%s", (mid,))
        conn.commit()

    print("== lifecycle: primary session ==")
    sid = "hermes-sess-" + new_id("s")[-6:]
    prov.initialize(sid, hermes_home=str(hermes_home), platform="cli",
                    agent_context="primary", agent_identity="default")
    ok = wait_for(lambda: q1(
        "SELECT 1 AS x FROM events WHERE session_id=%s AND kind='session_start' "
        "AND agent='hermes'", sid))
    check("session_start captured with agent='hermes'", ok)

    prov.sync_turn("What backend did we pick for the /goal reviewer?",
                   "You planned OpenRouter as the reviewer backend.",
                   session_id=sid,
                   messages=[{"role": "assistant", "tool_calls": [{"id": "t1"}]},
                             {"role": "tool", "content": "ok"}])
    ok = wait_for(lambda: q1(
        "SELECT count(*) AS n FROM events WHERE session_id=%s AND kind IN "
        "('user_message','agent_response','tool_call')", sid) is not None and q1(
        "SELECT count(*) AS n FROM events WHERE session_id=%s AND kind IN "
        "('user_message','agent_response','tool_call')", sid)["n"] >= 3)
    check("turn captured (user + assistant + tool summary)", ok)

    big = "X" * 10000
    prov.sync_turn(big, "short", session_id=sid)
    ok = wait_for(lambda: (r := q1(
        "SELECT raw_sha256, payload FROM events WHERE session_id=%s AND "
        "kind='user_message' AND payload->>'truncated'='true'", sid)) is not None
        and r["raw_sha256"])
    check("oversize text archived + truncated in ledger", ok)

    print("== prefetch + visa ==")
    prov.queue_prefetch("goal reviewer OpenRouter backend", session_id=sid)
    ok = wait_for(lambda: sid in prov._prefetch_cache, 5)
    check("background prefetch cached", ok)
    pkt = prov.prefetch("goal reviewer OpenRouter backend", session_id=sid)
    check("cached packet served", "OpenRouter" in pkt, pkt[:120])
    check("cache consumed (single use)", sid not in prov._prefetch_cache)
    check("hermes visa blocks personal_private", "PRIVATE-FAMILY" not in pkt)
    r = q1("SELECT agent FROM recall_log ORDER BY id DESC LIMIT 1")
    check("recall_log attributes agent='hermes'", r and r["agent"] == "hermes")
    pkt2 = prov.prefetch("goal reviewer backend", session_id=sid)  # no cache now
    check("first-turn sync prefetch works", "OpenRouter" in pkt2)

    print("== tools ==")
    res = json.loads(prov.handle_tool_call("memoryd_search",
                                           {"query": "OpenRouter reviewer"}))
    check("memoryd_search returns memory", res.get("ok") and "OpenRouter" in res["memory"])
    res = json.loads(prov.handle_tool_call("memoryd_report_miss",
                                           {"detail": "forgot the deployment target"}))
    ok = res.get("ok") and wait_for(lambda: q1(
        "SELECT 1 AS x FROM miss_signals WHERE signal='user_said_forgot' "
        "AND detail->>'note' LIKE '%%deployment%%'"))
    check("memoryd_report_miss logged", bool(ok))

    print("== optional hooks ==")
    prov.on_memory_write("add", "memory", "Hermes builtin: Alex timezone Europe/Zurich",
                         metadata={"write_origin": "tool"})
    ok = wait_for(lambda: q1(
        "SELECT 1 AS x FROM events WHERE session_id=%s AND kind='external_note' "
        "AND payload->>'note'='builtin_memory_write'", sid))
    check("builtin MEMORY.md write mirrored to canonical", ok)

    prov.on_delegation("summarize repo", "done: 3 modules found",
                       child_session_id="sub-1")
    ok = wait_for(lambda: q1(
        "SELECT 1 AS x FROM events WHERE session_id=%s AND kind='delegation'", sid))
    check("subagent delegation captured on parent", ok)

    prov.on_pre_compress([{"role": "user", "content": "old context about a vendor contract dispute"}])
    ok = wait_for(lambda: q1(
        "SELECT 1 AS x FROM events WHERE session_id=%s AND kind='external_note' "
        "AND payload->>'note'='pre_compress_snapshot'", sid))
    check("pre-compression snapshot captured", ok)

    print("== subagent context is read-only ==")
    sub = plugin.MemorydProvider()
    sub.initialize("hermes-subagent-1", hermes_home=str(hermes_home),
                   platform="cli", agent_context="subagent")
    sub.sync_turn("sub user", "sub answer", session_id="hermes-subagent-1")
    sub.on_memory_write("add", "memory", "subagent noise")
    time.sleep(1.0)
    r = q1("SELECT count(*) AS n FROM events WHERE session_id='hermes-subagent-1'")
    check("subagent wrote nothing", r["n"] == 0, f"n={r['n']}")

    print("== session end -> extraction ==")
    prov.on_session_end([{"role": "user", "content": "bye"}])
    ok = wait_for(lambda: q1(
        "SELECT 1 AS x FROM events WHERE session_id=%s AND kind='extraction_run'",
        sid), 8)
    check("session end triggered extraction", ok)

    print("== fail-open when daemon down ==")
    down = plugin.MemorydProvider()
    down.initialize("hermes-down-1", hermes_home=str(hermes_home), platform="cli",
                    agent_context="primary")
    down._url = "http://127.0.0.1:1"  # nothing listens here
    marker = down.prefetch("anything", session_id="hermes-down-1")
    check("fail-open visible marker (once)", "unavailable" in marker)
    check("second failure silent", down.prefetch("x", session_id="hermes-down-1") == "")
    down.sync_turn("offline turn", "offline answer", session_id="hermes-down-1")
    time.sleep(1.0)
    check("offline turns spooled in memory", len(down._spool) >= 1
          or not down._q.empty())
    down._url = "http://127.0.0.1:7437"  # recovery
    ok = wait_for(lambda: (r := q1(
        "SELECT count(*) AS n FROM events WHERE session_id='hermes-down-1' AND "
        "kind='user_message'")) and r["n"] >= 1, 6)
    check("spool flushed on recovery", ok)

    prov.shutdown()
    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
