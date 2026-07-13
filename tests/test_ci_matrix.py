"""Static guardrails for the production CI promotion matrix."""
from __future__ import annotations

import re
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
WORKFLOW = REPO / ".github" / "workflows" / "tests.yml"


def _workflow() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


def _job(text: str, name: str, following: str | None = None) -> str:
    start = text.index(f"  {name}:")
    if following is None:
        return text[start:]
    return text[start:text.index(f"  {following}:", start)]


def test_blocking_matrix_builds_and_installs_the_exact_wheel() -> None:
    workflow = _workflow()
    job = _job(workflow, "test")

    assert 'python-version: ["3.11", "3.13"]' in job
    assert "python -m build --wheel" in job
    assert 'python -m venv "$RUNNER_TEMP/venv-test"' in job
    assert "find dist" in job
    assert '"${wheels[0]}" pytest' in job
    assert "pip install ." not in job
    assert "assert-installed-wheel" in job


def test_blocking_matrix_exercises_every_required_suite() -> None:
    workflow = _workflow()
    job = _job(workflow, "test")

    required = {
        "python -m compileall",
        "python -m pytest -q tests",
        "scripts/test_durable_capture.py",
        "scripts/test_hermes_spool.py",
        "scripts/test_bitter_lesson.py",
        "scripts/smoke_test.py",
        "scripts/test_extract.py",
        "scripts/test_vector.py",
        "scripts/test_hermes.py",
        "scripts/test_postgres_recovery.py idempotency",
        "scripts/test_postgres_recovery.py backup-restore",
        "memoryd doctor",
    }
    missing = sorted(item for item in required if item not in job)
    assert not missing, f"blocking matrix omits: {missing}"
    migrations = sorted(path.name for path in (REPO / "migrations").glob("*.sql"))
    assert migrations == [
        "001_init.sql",
        "002_extraction.sql",
        "003_multi_agent.sql",
        "004_quarantine_event.sql",
        "005_bitter_lesson.sql",
        "006_durable_capture.sql",
        "007_api_request_ledger.sql",
    ]
    assert all(name in job for name in migrations)
    assert "HERMES_SOURCE_ROOT" in job
    assert 'PYTHONPATH="$HERMES_SOURCE_ROOT' in job
    assert "check_hermes_contract.py --require-pinned-bytes" in job


def test_postgres_and_hermes_pins_are_immutable_and_correctly_scoped() -> None:
    workflow = _workflow()
    blocking = _job(workflow, "pinned-hermes-contract",
                    "upstream-hermes-advisory")
    advisory = _job(workflow, "upstream-hermes-advisory", "hermes-spool")
    matrix = _job(workflow, "test")

    assert "pgvector/pgvector:pg16" in matrix
    assert "v2026.6.5" in blocking
    assert "3c231eb3979ab9c57d5cd6d02f1d577a3b718b43" in blocking
    assert "ref: main" in advisory
    assert "schedule/manual only" in advisory
    assert "github.event_name == 'schedule'" in advisory

    uses = re.findall(r"uses:\s*([^\s#]+)", workflow)
    assert uses
    for action in uses:
        revision = action.rsplit("@", 1)[-1]
        assert re.fullmatch(r"[0-9a-f]{40}", revision), action


def test_real_postgres_recovery_harness_is_present() -> None:
    script = REPO / "scripts" / "test_postgres_recovery.py"
    assert script.is_file()
    source = script.read_text(encoding="utf-8")
    for evidence in (
        "ThreadPoolExecutor", "api_request_ledger", "socket",
        "create_backup", "verify_snapshot", "restore_backup",
        "CREATE DATABASE", "DROP DATABASE",
    ):
        assert evidence in source
