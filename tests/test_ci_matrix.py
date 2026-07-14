"""Static guardrails for the production CI promotion matrix."""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
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
    assert "package = daemon.parent" in assertion
    assert "plugin = package / 'hermes_plugin'" in assertion
    assert "site.getsitepackages" in assertion
    for shipped in (
        "hermes_install.py",
        "hermes_compat.py",
        "hermes_validation/contract.py",
        "hermes_validation/installed_runtime.py",
        "hermes_validation/resources.py",
        "hermes_validation/agent/memory_provider.py",
        "hermes_plugin/plugin.yaml",
        "hermes_plugin/__init__.py",
        "hermes_plugin/spool.py",
        "migrations/001_init.sql",
        "migrations/002_extraction.sql",
        "migrations/003_multi_agent.sql",
        "migrations/004_quarantine_event.sql",
        "migrations/005_bitter_lesson.sql",
        "migrations/006_durable_capture.sql",
        "migrations/007_api_request_ledger.sql",
    ):
        assert shipped in assertion
    assert "plugin_metadata['version'] == '0.3.1'" in assertion
    assert "importlib.metadata.version('memoryd') == '0.3.1'" in assertion

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

    isolated = steps["Validate packaged preflight against isolated Hermes"]["run"]
    assert 'HERMES_TARGET_PYTHON="$RUNNER_TEMP/hermes-target/bin/python"' in isolated
    assert "test_hermes_validation_integration.py" in isolated
    assert 'cd "$RUNNER_TEMP"' in isolated

    target_install = install
    assert 'python -m venv "$RUNNER_TEMP/hermes-target"' in target_install
    assert '"$RUNNER_TEMP/hermes-target/bin/python" -m pip install' in target_install
    assert '"$HERMES_SOURCE_ROOT"' in target_install

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


def test_job_environment_does_not_use_step_only_runner_context() -> None:
    job = _workflow_model()["jobs"]["test"]
    assert all("${{ runner." not in str(value) for value in job["env"].values())

    configure = _named_steps(job)["Configure temporary runtime paths"]["run"]
    assert 'MEMORYD_HOME=$RUNNER_TEMP/memoryd-home' in configure
    assert 'MEMORYD_LLM_MOCK_FILE=$RUNNER_TEMP/mock_llm.json' in configure
    assert configure.count("GITHUB_ENV") == 2


def test_structured_source_and_installed_suites_are_separate() -> None:
    job = _workflow_model()["jobs"]["test"]
    steps = _named_steps(job)
    source = steps["Run checkout unit and fault-injection suites"]["run"]
    installed = steps["Run installed-wheel DB-backed regression matrix"]["run"]
    assert 'VENV_PYTHON="$RUNNER_TEMP/venv-test/bin/python"' in source
    assert "unset MEMORYD_DSN MEMORYD_HOME" in source
    invocations = [
        line.strip() for line in source.splitlines()
        if line.strip().startswith(('python ', '"$VENV_PYTHON" '))
    ]
    assert len(invocations) == 6
    assert all(line.startswith('"$VENV_PYTHON" ') for line in invocations)
    assert '"$VENV_PYTHON" -m pytest -q tests' in source
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
        '"$VENV_PYTHON" -m compileall',
        '"$VENV_PYTHON" -m pytest -q tests',
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


def test_release_metadata_and_live_guides_agree_on_v031() -> None:
    from memoryd import __version__
    from memoryd.server import Handler

    assert __version__ == "0.3.1"
    assert Handler.server_version == "memoryd/0.3.1"

    plugin = yaml.safe_load(
        (REPO / "hermes_plugin" / "memoryd" / "plugin.yaml").read_text(
            encoding="utf-8"
        )
    )
    assert plugin["version"] == "0.3.1"

    live_paths = [
        REPO / "README.md",
        REPO / "docs" / "HERMES_INSTALL_PROMPT.md",
        REPO / "docs" / "PRODUCTION_ROLLOUT.md",
        REPO / "docs" / "REFERENCE.md",
        REPO / "docs" / "CANARY_SCORECARD.md",
        REPO / "hermes_plugin" / "memoryd" / "README.md",
        REPO / "scripts" / "test_hermes.py",
        WORKFLOW,
    ]
    for path in live_paths:
        source = path.read_text(encoding="utf-8")
        assert "0.3." + "0" not in source, path.relative_to(REPO)


def _git_tracked_files(repo: Path) -> list[Path]:
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), "ls-files", "-z", "--"],
            check=False,
            capture_output=True,
        )
    except OSError as exc:
        raise AssertionError(f"cannot enumerate Git-tracked files: {exc}") from exc
    if result.returncode != 0:
        detail = os.fsdecode(result.stderr).strip() or "no error output"
        raise AssertionError(
            f"git ls-files failed with exit {result.returncode}: {detail}"
        )
    return [
        repo / os.fsdecode(relative)
        for relative in result.stdout.split(b"\0")
        if relative
    ]


def test_stale_release_references_are_historical_only() -> None:
    stale = "0.3." + "0"
    unexpected: list[str] = []
    source_suffixes = {
        ".json", ".md", ".ps1", ".py", ".sh", ".sql", ".toml", ".txt",
        ".yaml", ".yml",
    }
    for path in _git_tracked_files(REPO):
        relative = path.relative_to(REPO)
        if any(
            part in {".git", ".superpowers", ".venv", "__pycache__", "dist"}
            for part in relative.parts
        ):
            continue
        relative_text = relative.as_posix()
        if relative_text == "CHANGELOG.md" or relative_text.startswith(
            "docs/superpowers/"
        ):
            continue
        if not path.is_file() or path.suffix.lower() not in source_suffixes:
            continue
        try:
            source = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        if stale in source:
            unexpected.append(relative_text)
    assert unexpected == []


def _release_guard_repo(tmp_path: Path, tracked_source: str) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    tracked = repo / "tracked.py"
    tracked.write_text(tracked_source, encoding="utf-8")
    subprocess.run(
        ["git", "init", "--quiet", str(repo)],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "add", "tracked.py"],
        check=True,
        capture_output=True,
    )
    return repo


def test_stale_release_guard_ignores_untracked_generated_files(
        monkeypatch, tmp_path: Path) -> None:
    repo = _release_guard_repo(tmp_path, "VERSION = '0.3.1'\n")
    generated = repo / "_hermes-agent" / "dependency.py"
    generated.parent.mkdir()
    generated.write_text(
        "VERSION = '" + "0.3." + "0'\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(sys.modules[__name__], "REPO", repo)

    test_stale_release_references_are_historical_only()


def test_stale_release_guard_still_rejects_tracked_stale_files(
        monkeypatch, tmp_path: Path) -> None:
    repo = _release_guard_repo(tmp_path, "VERSION = '" + "0.3." + "0'\n")
    monkeypatch.setattr(sys.modules[__name__], "REPO", repo)

    with pytest.raises(AssertionError, match="tracked.py"):
        test_stale_release_references_are_historical_only()


def test_guided_quickstart_is_immutable_and_primary() -> None:
    readme = (REPO / "README.md").read_text(encoding="utf-8")
    install = "pipx install --python python3.13"
    release = "'git+https://github.com/chrisduvillard/memoryd.git@v0.3.1'"
    command = "memoryd install --hermes"
    assert install in readme
    assert release in readme
    assert command in readme
    assert readme.index(install) < readme.index("### Quickstart (evaluation")

    prompt = (REPO / "docs" / "HERMES_INSTALL_PROMPT.md").read_text(
        encoding="utf-8"
    )
    assert install in prompt
    assert release in prompt
    assert command in prompt
    assert "exit" in prompt.lower() and "normal terminal" in prompt.lower()
    for forbidden in (
        "hermes config set",
        "hermes gateway stop",
        "hermes gateway start",
    ):
        assert forbidden not in prompt


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
    assert "python -m scripts.test_hermes_contract" in blocking
    assert "python scripts/test_hermes_contract.py" not in blocking
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
        "CREATE DATABASE", "DROP DATABASE", "CREATE SCHEMA preexisting",
        "CREATE VIEW preexisting_view", "CREATE SEQUENCE preexisting_sequence",
        "CREATE FUNCTION preexisting_function", "CREATE TYPE preexisting_type",
        "CREATE EXTENSION vector", "target database already has user objects",
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
