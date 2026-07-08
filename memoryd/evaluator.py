"""Lightweight eval and replay helpers for policy/model comparisons."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .model_gateway import get_model_profile
from .policies import get_packet_compiler, get_recall_policy


def run_static_eval(cases: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Run deterministic, DB-free checks over supplied cases.

    The DB-backed admin endpoint wraps this and records eval_runs. This small
    core lets unit tests and future policy A/B runs share the same summary
    shape without depending on a live daemon database.
    """
    cases = cases or []
    policy = get_recall_policy()
    compiler = get_packet_compiler()
    profile = get_model_profile()
    passed = 0
    results = []
    for case in cases:
        kind = case.get("kind", "policy")
        expected = case.get("expected") or {}
        observed: dict[str, Any] = {}
        if kind == "policy":
            prompt = (case.get("input") or {}).get("prompt", "")
            observed["mode"] = policy.classify(prompt)
        ok = all(observed.get(k) == v for k, v in expected.items())
        passed += int(ok)
        results.append({
            "id": case.get("id"),
            "kind": kind,
            "ok": ok,
            "expected": expected,
            "observed": observed,
        })
    return {
        "ts": datetime.now(timezone.utc).isoformat(),
        "cases": len(cases),
        "passed": passed,
        "failed": len(cases) - passed,
        "model_profile": profile.name,
        "recall_policy": policy.name,
        "packet_compiler": compiler.name,
        "results": results,
    }
