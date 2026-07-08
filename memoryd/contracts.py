"""Versioned extraction contracts.

Contracts are data, not hidden code constants. The current v1 prompt remains
the default so existing installs behave the same, but future models can get
new contracts and be evaluated side-by-side.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

VALID_TYPES = (
    "identity", "preference", "writing_style", "project_state", "decision",
    "open_question", "commitment", "person", "company", "technical_fact",
    "workflow", "constraint", "procedure", "directive", "warning", "priming",
)

BUILTIN_V1_SYSTEM_PROMPT = """You extract durable memories from an agent-session transcript.

Return ONLY a JSON array. Each element:
{
 "type": one of [identity, preference, writing_style, project_state, decision,
         open_question, commitment, person, company, technical_fact, workflow,
         constraint, procedure, directive, warning, priming],
 "text": one well-formed, fully-scoped sentence or short paragraph,
 "struct": {},            // REQUIRED for directive: {"directive","condition","expires","severity"}
                          // REQUIRED for warning:   {"class","target","severity"}
                          // decision: {"options","chosen","rationale"}
 "project": string|null,  // null = global
 "scope": "work_private"|"project_shared"|"personal_private"|"public",
 "sensitivity": "public"|"normal"|"private"|"sealed",
 "authority_claim": "A1"|"A2"|"B1"|"C1"|"D1",
 "confidence": 0..1,
 "activation": {"task_type":[],"audience":[],"exclude":[]},
 "source_event_ids": [ids from the transcript - REQUIRED, must be real],
 "evidence_quote": "verbatim snippet from a cited event (REQUIRED for A1)",
 "duplicate_of": "mem_id or null",   // if it restates an EXISTING memory below
 "contradicts": ["mem_id", ...]      // existing memories this conflicts with
}

Hard rules:
- PRESERVE HEDGES. "might switch to Qdrant" extracts as *considering, no
  decision made* - never as a decision or commitment.
- A1 only for direct explicit user statements, with evidence_quote.
- Extract FEW, DURABLE items. Session chatter, one-off details, and anything
  already covered by an existing memory (use duplicate_of) should not become
  new candidates. Zero candidates is a valid answer.
- Never invent source_event_ids.
- warnings: failed attempts, fragile files, user-stated boundaries.
- directives: explicit standing instructions from the user."""


@dataclass(frozen=True)
class ExtractorContract:
    name: str
    system_prompt: str
    output_schema: dict[str, Any]
    valid_types: tuple[str, ...] = VALID_TYPES
    source_packer: str = "balanced_v1"
    max_output_tokens: int = 4000

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "valid_types": list(self.valid_types),
            "source_packer": self.source_packer,
            "max_output_tokens": self.max_output_tokens,
            "output_schema": self.output_schema,
        }


def _candidate_schema() -> dict[str, Any]:
    return {
        "type": "array",
        "items": {
            "type": "object",
            "required": ["type", "text", "confidence", "source_event_ids"],
            "properties": {
                "type": {"type": "string", "enum": list(VALID_TYPES)},
                "text": {"type": "string"},
                "struct": {"type": "object"},
                "project": {"type": ["string", "null"]},
                "scope": {"type": "string"},
                "sensitivity": {"type": "string"},
                "authority_claim": {"type": "string"},
                "confidence": {"type": "number"},
                "activation": {"type": "object"},
                "source_event_ids": {"type": "array", "items": {"type": "string"}},
                "evidence_quote": {"type": "string"},
                "duplicate_of": {"type": ["string", "null"]},
                "contradicts": {"type": "array", "items": {"type": "string"}},
            },
        },
    }


def _contracts() -> dict[str, ExtractorContract]:
    return {
        "builtin_v1": ExtractorContract(
            name="builtin_v1",
            system_prompt=BUILTIN_V1_SYSTEM_PROMPT,
            output_schema=_candidate_schema(),
        ),
        "wide_context_v1": ExtractorContract(
            name="wide_context_v1",
            system_prompt=BUILTIN_V1_SYSTEM_PROMPT
            + "\n\nWhen full raw sources are supplied, prefer exact cited evidence over summaries.",
            output_schema=_candidate_schema(),
            source_packer="wide_context_v1",
            max_output_tokens=8000,
        ),
    }


def list_extractor_contracts() -> list[str]:
    return sorted(_contracts())


def get_extractor_contract(name: str | None = None) -> ExtractorContract:
    selected = name or os.environ.get("MEMORYD_EXTRACTOR_CONTRACT")
    if not selected:
        try:
            from .core import CFG
            selected = CFG.extractor_contract
        except Exception:  # noqa: BLE001
            selected = "builtin_v1"
    contracts = _contracts()
    return contracts.get(selected or "builtin_v1", contracts["builtin_v1"])
