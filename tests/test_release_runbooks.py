from __future__ import annotations

import re
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
ROLLOUT = (REPO / "docs/PRODUCTION_ROLLOUT.md").read_text(encoding="utf-8")
CANARY = (REPO / "docs/CANARY_SCORECARD.md").read_text(encoding="utf-8")
PLUGIN_README_PATH = REPO / "hermes_plugin/memoryd/README.md"
PLUGIN_README = PLUGIN_README_PATH.read_text(encoding="utf-8")


def test_rollout_makes_guided_install_primary_and_profile_selection_explicit():
    assert "guided installer is the supported production path" in ROLLOUT
    assert "memoryd install --hermes" in ROLLOUT
    assert "`active_profile`" in ROLLOUT
    assert "literal `default` selects the root" in ROLLOUT
    assert "does not invent a missing selected profile" in ROLLOUT
    assert "Manual commands" in ROLLOUT
    assert "not a second installation recipe" in ROLLOUT
    prerequisites = ROLLOUT.split("## 1. Prerequisites", 1)[1].split("## 2.", 1)[0]
    assert "`git`" in prerequisites


def test_rollout_documents_transactional_rollback_and_evidence_preservation():
    activation = ROLLOUT.split("## 3. Transactional activation", 1)[1].split(
        "## 4.", 1
    )[0]

    for status in (
        "hermes memory status",
        "hermes memoryd config",
        "memoryd status",
        "hermes memoryd status",
    ):
        assert status in activation
    assert "SIGINT" in activation and "SIGTERM" in activation
    assert "previous provider" in activation
    assert "previous gateway state" in activation
    assert "dead-letter evidence" in activation


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
