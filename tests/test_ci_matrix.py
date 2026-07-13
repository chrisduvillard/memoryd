"""Static guardrails for the production CI promotion matrix."""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import yaml


REPO = Path(__file__).resolve().parents[1]
WORKFLOW = REPO / ".github" / "workflows" / "tests.yml"


def _workflow() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


def _job(text: str, name: str, following: str | None = None) -> str:
    start = text.index(f"  {name}:")
    if following is None:
        return text[start:]
    return text[start:text.index(f"  {following}:", start)]


def _workflow_model() -> dict:
    value = yaml.safe_load(_workflow())
    assert isinstance(value, dict) and isinstance(value.get("jobs"), dict)
    return value


def _named_steps(job: dict) -> dict[str, dict]:
    steps = job.get("steps")
    assert isinstance(steps, list)
    named = {step.get("name"): step for step in steps if step.get("name")}
    assert len(named) == len([step for step in steps if step.get("name")])
    return named


def test_structured_matrix_enforces_installed_artifact_boundaries() -> None:
    job = _workflow_model()["jobs"]["test"]
    assert job["strategy"]["matrix"]["python-version"] == ["3.11", "3.13"]
    assert job["services"]["postgres"]["image"] == "pgvector/pgvector:pg16"
    steps = _named_steps(job)
    ordered_names = [step.get("name") for step in job["steps"]]
    required_order = [
        "Verify pinned Hermes commit and PostgreSQL client",
        "Build wheel in an isolated environment",
        "Install exact memoryd wheel and pinned Hermes runtime",
        "Assert installed package and plugin origins",
        "Stage installed-artifact harnesses outside checkout",
        "Validate installed Hermes loader and lifecycle",
        "Apply packaged migrations 001 through 007",
        "Run installed-wheel DB-backed regression matrix",
        "Run installed-wheel offline backup and disposable restore",
    ]
    positions = [ordered_names.index(name) for name in required_order]
    assert positions == sorted(positions)

    install = steps["Install exact memoryd wheel and pinned Hermes runtime"]["run"]
    assert 'python -m venv "$RUNNER_TEMP/venv-test"' in install
    assert '"${wheels[0]}"' in install
    assert '"$HERMES_SOURCE_ROOT"' in install
    assert "--no-deps" not in install and "pip install ." not in install

    assertion = steps["Assert installed package and plugin origins"]["run"]
    assert 'cd "$RUNNER_TEMP"' in assertion
    assert "importlib.metadata" in assertion
    assert "agent.memory_provider" in assertion
    assert "plugin = daemon.parent / 'hermes_plugin'" in assertion
    assert "site.getsitepackages" in assertion

    staging = steps["Stage installed-artifact harnesses outside checkout"]["run"]
    assert 'INSTALLED_HARNESS="$RUNNER_TEMP/installed-harness"' in staging
    assert "scripts/test_hermes.py" in staging
    assert "plugin = package / 'hermes_plugin'" in staging
    assert "GITHUB_ENV" in staging

    hermes = steps["Validate installed Hermes loader and lifecycle"]["run"]
    assert 'cd "$INSTALLED_HARNESS"' in hermes
    assert "validate_installed_hermes.py" in hermes
    assert '"$MEMORYD_PLUGIN_SOURCE"' in hermes
    assert "check_hermes_contract.py --require-pinned-bytes" in hermes
    assert "--source-root" not in hermes and "PYTHONPATH" not in hermes

    database = steps["Run installed-wheel DB-backed regression matrix"]["run"]
    assert 'cd "$INSTALLED_HARNESS"' in database
    assert all(f"scripts/{name}" in database for name in (
        "smoke_test.py", "test_extract.py", "test_vector.py", "test_hermes.py",
        "test_postgres_recovery.py"))
    assert "scripts/test_hermes.py --installed" in database
    assert '--hermes-home "$RUNNER_TEMP/hermes-live-profile"' in database
    assert '--plugin-source "$MEMORYD_PLUGIN_SOURCE"' in database
    assert "GITHUB_WORKSPACE/scripts" not in database
    assert "PYTHONPATH" not in database


def test_structured_source_and_installed_suites_are_separate() -> None:
    job = _workflow_model()["jobs"]["test"]
    steps = _named_steps(job)
    source = steps["Run checkout unit and fault-injection suites"]["run"]
    installed = steps["Run installed-wheel DB-backed regression matrix"]["run"]
    assert "python -m pytest -q tests" in source
    assert "test_durable_capture.py" in source
    assert "$INSTALLED_HARNESS" not in source
    assert "$INSTALLED_HARNESS" in installed
    assert "test_durable_capture.py" not in installed


def test_blocking_matrix_builds_and_installs_the_exact_wheel() -> None:
    workflow = _workflow()
    job = _job(workflow, "test")

    assert 'python-version: ["3.11", "3.13"]' in job
    assert "python -m build --wheel" in job
    assert 'python -m venv "$RUNNER_TEMP/venv-test"' in job
    assert "find dist" in job
    assert '"${wheels[0]}" pytest' in job
    assert "pip install ." not in job
    assert "Assert installed package and plugin origins" in job


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
        "scripts/test_hermes.py --installed",
        "scripts/validate_installed_hermes.py",
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
    assert "PYTHONPATH=" not in job
    assert "check_hermes_contract.py --require-pinned-bytes" in job


def test_live_hermes_harness_has_strict_installed_mode_and_full_lifecycle() -> None:
    source = (REPO / "scripts" / "test_hermes.py").read_text(encoding="utf-8")
    for evidence in (
        'parser.add_argument("--installed", action="store_true")',
        'parser.add_argument("--hermes-home", type=Path)',
        'parser.add_argument("--plugin-source", type=Path)',
        'metadata.version("hermes-agent")',
        'from plugins.memory import discover_memory_providers, load_memory_provider',
        'load_memory_provider("memoryd")',
        'plugin_target not in provider_path.parents',
        '"hermes visa blocks personal_private"',
        '"memoryd_search returns memory"',
        '"memoryd_report_miss logged"',
        '"builtin MEMORY.md write mirrored to canonical"',
        '"subagent delegation captured on parent"',
        '"subagent wrote nothing"',
        '"session end triggered extraction"',
        '"offline turns durably spooled"',
        '"spool flushed on recovery"',
    ):
        assert evidence in source


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


def test_real_postgres_recovery_harness_is_collection_safe() -> None:
    script = REPO / "scripts" / "test_postgres_recovery.py"
    source = script.read_text(encoding="utf-8")
    assert "def test_" not in source
    environment = os.environ.copy()
    environment.pop("MEMORYD_DSN", None)
    environment.pop("MEMORYD_HOME", None)
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import runpy; runpy.run_path(" + repr(str(script)) + ", "
            "run_name='_collection_probe')",
        ],
        cwd=REPO,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
