"""Versioned recall policies and packet compilers."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Pattern

DEFAULT_LANE_BUDGETS = {
    "directives_warnings": 300,
    "hot": 350,
    "retrieved": 600,
    "candidates": 150,
    "open_loops": 100,
}

DEFAULT_RERANK_WEIGHTS = {
    "semantic": 0.35,
    "keyword": 0.20,
    "recency": 0.15,
    "useful": 0.15,
    "authority": 0.10,
    "confirmation_recency": 0.05,
}

DEFAULT_MODE_PATTERNS: tuple[tuple[str, Pattern[str]], ...] = (
    ("debug", re.compile(
        r"traceback|error|exception|stack|\.py\b|\.ts\b|/[\w./-]+\.\w{1,4}\b|undefined|null pointer",
        re.I)),
    ("decision", re.compile(r"\bshould (we|i)\b|\bdid (we|i) decide\b|\bwhy did\b|\bdecision\b", re.I)),
    ("state", re.compile(r"\bwhere were we\b|\bcontinue\b|\bstatus\b|\bnext step", re.I)),
    ("style", re.compile(r"\bwrite\b|\bemail\b|\bdraft\b|\bmessage to\b|\breply\b", re.I)),
)


@dataclass(frozen=True)
class RecallPolicy:
    name: str
    lane_budgets: dict[str, int] = field(default_factory=lambda: dict(DEFAULT_LANE_BUDGETS))
    rerank_weights: dict[str, float] = field(default_factory=lambda: dict(DEFAULT_RERANK_WEIGHTS))
    active_limit: int = 12
    candidate_limit: int = 5
    warning_limit: int = 20
    hot_limit: int = 15
    loop_limit: int = 6
    recency_half_life_days: int = 90
    mode_patterns: tuple[tuple[str, Pattern[str]], ...] = DEFAULT_MODE_PATTERNS

    def classify(self, prompt: str) -> str:
        for mode, pattern in self.mode_patterns:
            if pattern.search(prompt):
                return mode
        return "general"

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "lane_budgets": self.lane_budgets,
            "rerank_weights": self.rerank_weights,
            "active_limit": self.active_limit,
            "candidate_limit": self.candidate_limit,
            "warning_limit": self.warning_limit,
            "hot_limit": self.hot_limit,
            "loop_limit": self.loop_limit,
            "recency_half_life_days": self.recency_half_life_days,
            "modes": [m for m, _ in self.mode_patterns] + ["general"],
        }


@dataclass(frozen=True)
class PacketCompiler:
    name: str
    description: str

    def to_dict(self) -> dict:
        return {"name": self.name, "description": self.description}


def _policies() -> dict[str, RecallPolicy]:
    heuristic = RecallPolicy(name="heuristic_v1")
    oracle = RecallPolicy(
        name="oracle_v1",
        lane_budgets={**DEFAULT_LANE_BUDGETS, "retrieved": 750, "candidates": 250},
        active_limit=20,
        candidate_limit=10,
    )
    return {heuristic.name: heuristic, oracle.name: oracle}


def list_recall_policies() -> list[str]:
    return sorted(_policies())


def get_recall_policy(name: str | None = None) -> RecallPolicy:
    selected = name or os.environ.get("MEMORYD_RECALL_POLICY")
    if not selected:
        try:
            from .core import CFG
            selected = CFG.recall_policy
        except Exception:  # noqa: BLE001
            selected = "heuristic_v1"
    policies = _policies()
    return policies.get(selected or "heuristic_v1", policies["heuristic_v1"])


def _compilers() -> dict[str, PacketCompiler]:
    return {
        "lane_v1": PacketCompiler(
            name="lane_v1",
            description="Current deterministic lane renderer with fixed per-lane budgets.",
        ),
        "oracle_v1": PacketCompiler(
            name="oracle_v1",
            description="Eval-only compiler profile for broader candidate comparison.",
        ),
    }


def list_packet_compilers() -> list[str]:
    return sorted(_compilers())


def get_packet_compiler(name: str | None = None) -> PacketCompiler:
    selected = name or os.environ.get("MEMORYD_PACKET_COMPILER")
    if not selected:
        try:
            from .core import CFG
            selected = CFG.packet_compiler
        except Exception:  # noqa: BLE001
            selected = "lane_v1"
    compilers = _compilers()
    return compilers.get(selected or "lane_v1", compilers["lane_v1"])
