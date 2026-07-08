"""Versioned semantic validation and promotion policies."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Pattern


@dataclass(frozen=True)
class SemanticPolicy:
    name: str
    hedges: Pattern[str]
    committal: Pattern[str]
    auto_active_types: frozenset[str]
    candidate_half_life_days: int = 30

    def hedge_violation(self, cited_text: str, candidate_text: str) -> bool:
        if not self.hedges.search(cited_text) or not self.committal.search(candidate_text):
            return False
        cw = set(re.findall(r"[a-z]{4,}", candidate_text.lower()))
        hw = set(re.findall(r"[a-z]{4,}", cited_text.lower()))
        return len(cw & hw) >= 2

    def promote(self, cand: dict) -> str:
        if cand.get("scope") == "untrusted_external" or cand.get("authority_claim") == "Q":
            return "quarantined"
        if cand["type"] == "identity" and not cand.get("project"):
            return "candidate"
        if cand["type"] == "priming":
            return "active"
        if cand.get("authority_claim") == "A1" and cand["type"] in self.auto_active_types:
            return "active"
        return "candidate"

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "auto_active_types": sorted(self.auto_active_types),
            "candidate_half_life_days": self.candidate_half_life_days,
        }


def _policies() -> dict[str, SemanticPolicy]:
    conservative = SemanticPolicy(
        name="conservative_v1",
        hedges=re.compile(r"\b(might|maybe|perhaps|considering|could|thinking about|"
                          r"not sure|possibly|leaning|tempted|later)\b", re.I),
        committal=re.compile(r"\b(will|decided|is going to|chose|has chosen|must|"
                             r"always|definitely)\b", re.I),
        auto_active_types=frozenset({"directive", "decision", "constraint", "commitment"}),
    )
    return {conservative.name: conservative}


def list_semantic_policies() -> list[str]:
    return sorted(_policies())


def get_semantic_policy(name: str | None = None) -> SemanticPolicy:
    selected = name or os.environ.get("MEMORYD_SEMANTIC_POLICY")
    if not selected:
        try:
            from .core import CFG
            selected = CFG.semantic_policy
        except Exception:  # noqa: BLE001
            selected = "conservative_v1"
    policies = _policies()
    return policies.get(selected or "conservative_v1", policies["conservative_v1"])
