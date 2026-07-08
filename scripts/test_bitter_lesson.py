#!/usr/bin/env python3
"""Bitter-Lesson upgrade tests.

These checks avoid the live database by design. They pin the new extension
points that let better models and policies improve memoryd without rewriting
the safety substrate.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    (PASS if ok else FAIL).append(name)
    marker = "PASS" if ok else "FAIL"
    print(f"  {marker} {name}" + (f" -- {detail}" if detail and not ok else ""))


def main() -> int:
    os.environ["MEMORYD_LLM"] = "mock"
    os.environ["MEMORYD_MODEL_PROFILE"] = "mock"
    os.environ["MEMORYD_RECALL_POLICY"] = "heuristic_v1"
    os.environ["MEMORYD_PACKET_COMPILER"] = "lane_v1"
    os.environ["MEMORYD_EXTRACTOR_CONTRACT"] = "builtin_v1"

    print("== model gateway ==")
    from memoryd.model_gateway import get_model_profile, list_model_profiles

    profile = get_model_profile()
    check("default profile comes from MEMORYD_MODEL_PROFILE", profile.name == "mock", str(profile))
    check("profile exposes capability metadata", "structured_json" in profile.capabilities)
    check("profiles are listable for admin endpoint", "mock" in list_model_profiles())
    check("mock profile is capture-safe", profile.provider == "mock" and profile.model)

    print("== extractor contracts ==")
    from memoryd.contracts import get_extractor_contract, list_extractor_contracts

    contract = get_extractor_contract()
    check("default extractor contract selected", contract.name == "builtin_v1")
    check("contract exposes system prompt", "Return ONLY a JSON array" in contract.system_prompt)
    check("contract exposes candidate schema", "source_event_ids" in json.dumps(contract.output_schema))
    check("contracts are listable", "builtin_v1" in list_extractor_contracts())

    print("== recall policies ==")
    from memoryd.policies import get_packet_compiler, get_recall_policy, list_recall_policies

    policy = get_recall_policy()
    check("default recall policy selected", policy.name == "heuristic_v1")
    check("heuristic policy preserves current lane budgets",
          policy.lane_budgets["directives_warnings"] == 300
          and policy.lane_budgets["retrieved"] == 600)
    check("policy classification remains compatible", policy.classify("traceback in app.py") == "debug")
    check("oracle policy exists for A/B evaluation", "oracle_v1" in list_recall_policies())
    compiler = get_packet_compiler()
    check("packet compiler selected by env", compiler.name == "lane_v1")

    print("== semantic policies ==")
    from memoryd.semantic_policies import get_semantic_policy, list_semantic_policies

    semantic = get_semantic_policy()
    check("default semantic policy selected", semantic.name == "conservative_v1")
    check("semantic policies are listable", "conservative_v1" in list_semantic_policies())
    check("semantic policy catches hedge overcommit",
          semantic.hedge_violation("I might switch to Qdrant later", "Alex decided to switch to Qdrant"))

    print("== adapter envelope and source packing ==")
    from memoryd.adapters import event_to_envelope
    from memoryd.source_pack import pack_session_events

    env = event_to_envelope({
        "kind": "user_message",
        "payload": {"text": "Remember this durable fact."},
        "session_id": "s1",
        "agent": "claude-code",
        "project": "repo",
    }, runtime="claude-code")
    check("adapter envelope normalizes event type", env["event_type"] == "user_message")
    check("adapter envelope carries inline preview", env["inline_preview"] == "Remember this durable fact.")
    packed = pack_session_events([{
        "id": "evt_1", "kind": "user_message", "payload": {"text": "hello"}, "raw_sha256": None
    }], max_chars=100)
    check("source packer records deterministic budget", packed.used_chars <= 100)
    check("source packer renders event ids", "[evt_1] user_message: hello" in packed.text)

    print("== eval harness and admin surfaces ==")
    from memoryd.evaluator import run_static_eval
    from memoryd.server import ADMIN_POST_ENDPOINTS

    result = run_static_eval(cases=[{
        "id": "case_1",
        "kind": "policy",
        "input": {"prompt": "continue the repo work"},
        "expected": {"mode": "state"},
    }])
    check("static eval records case count", result["cases"] == 1)
    check("static eval records policy profile", result["recall_policy"] == "heuristic_v1")
    for endpoint in (
        "/admin/eval",
        "/admin/replay",
        "/admin/policies",
        "/admin/model-profiles",
        "/admin/export-evidence",
    ):
        check(f"{endpoint} registered", endpoint in ADMIN_POST_ENDPOINTS)

    print("== migration shape ==")
    mig = REPO / "migrations" / "005_bitter_lesson.sql"
    sql = mig.read_text(encoding="utf-8") if mig.exists() else ""
    check("migration 005 exists", bool(sql))
    check("migration opens event kind constraint", "DROP CONSTRAINT IF EXISTS events_kind_check" in sql)
    check("migration opens memory type column", "ALTER COLUMN type TYPE TEXT" in sql)
    for table in (
        "memory_type_registry", "event_type_registry", "model_runs",
        "policy_runs", "eval_cases", "eval_runs", "packet_runs",
    ):
        check(f"{table} table declared", f"CREATE TABLE IF NOT EXISTS {table}" in sql)

    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
