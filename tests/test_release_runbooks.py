from __future__ import annotations

import re
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
ROLLOUT = (REPO / "docs/PRODUCTION_ROLLOUT.md").read_text(encoding="utf-8")
CANARY = (REPO / "docs/CANARY_SCORECARD.md").read_text(encoding="utf-8")
PLUGIN_README_PATH = REPO / "hermes_plugin/memoryd/README.md"
PLUGIN_README = PLUGIN_README_PATH.read_text(encoding="utf-8")


def test_rollout_refuses_a_stale_selected_hermes_profile():
    profile_section = ROLLOUT.split(
        "## 1. Preflight, select the Hermes profile, and pin Hermes", 1
    )[1].split("## 2.", 1)[0]

    assert 'if [[ "$HERMES_PROFILE" == default ]]' in profile_section
    assert 'test -d "$HERMES_HOME"' in profile_section
    assert 'install -d -m 700 "$HERMES_ROOT"' in profile_section
    assert 'install -d -m 700 "$HERMES_HOME"' not in profile_section


def test_canary_visa_uses_the_running_service_runtime_and_environment():
    visa_section = CANARY.split("## Plant ten out-of-visa memories", 1)[1].split(
        "In a controlled maintenance window", 1
    )[0]

    assert "from memoryd.core import CFG" in visa_section
    assert 'CFG.visa("hermes")' in visa_section
    assert "/proc/{pid}/cmdline" in visa_section
    assert "/proc/{pid}/environ" in visa_section
    assert "daemon_env" in visa_section
    assert "systemctl" in visa_section


def test_citation_population_is_non_canary_extraction_lineage():
    citation_section = CANARY.split("## Extraction citation gate", 1)[1].split(
        "## Real production snapshot restore gate", 1
    )[0]

    assert "NOT m.is_canary" in citation_section
    assert "kind = 'extraction_run'" in citation_section
    assert "payload->>'ok' = 'true'" in citation_section
    assert "creator provenance" in citation_section


def test_plugin_readme_root_link_resolves():
    match = re.search(r"\[memoryd\]\(([^)]+)\)", PLUGIN_README)
    assert match is not None
    target = (PLUGIN_README_PATH.parent / match.group(1)).resolve()
    assert target.is_file()
    assert target == (REPO / "README.md").resolve()
