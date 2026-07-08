"""Model profiles and capability metadata.

This is the thin gateway layer between memoryd policies/contracts and concrete
LLM clients. It keeps provider defaults compatible while making capabilities
auditable and listable for eval/replay.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ModelProfile:
    name: str
    provider: str
    model: str
    base_url: str | None = None
    max_context_tokens: int = 16000
    timeout_s: float = 120.0
    capabilities: tuple[str, ...] = field(default_factory=tuple)
    preferred_extractor_contract: str = "builtin_v1"
    preferred_source_packer: str = "balanced_v1"

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "provider": self.provider,
            "model": self.model,
            "base_url": self.base_url,
            "max_context_tokens": self.max_context_tokens,
            "timeout_s": self.timeout_s,
            "capabilities": list(self.capabilities),
            "preferred_extractor_contract": self.preferred_extractor_contract,
            "preferred_source_packer": self.preferred_source_packer,
        }


def _profiles() -> dict[str, ModelProfile]:
    openrouter_model = os.environ.get("MEMORYD_LLM_MODEL", "google/gemini-3.5-flash")
    openai_model = os.environ.get("MEMORYD_LLM_MODEL", "gpt-4o-mini")
    anthropic_model = os.environ.get("MEMORYD_LLM_MODEL", "claude-haiku-4-5-20251001")
    return {
        "mock": ModelProfile(
            name="mock",
            provider="mock",
            model=os.environ.get("MEMORYD_LLM_MOCK_MODEL", "mock-json-fixture"),
            max_context_tokens=1_000_000,
            capabilities=("structured_json", "deterministic", "offline"),
        ),
        "openrouter": ModelProfile(
            name="openrouter",
            provider="openrouter",
            model=openrouter_model,
            base_url=os.environ.get("MEMORYD_LLM_BASE", "https://openrouter.ai/api/v1"),
            max_context_tokens=int(os.environ.get("MEMORYD_MODEL_CONTEXT_TOKENS", "1000000")),
            capabilities=("chat", "structured_json", "long_context"),
        ),
        "openai": ModelProfile(
            name="openai",
            provider="openai",
            model=openai_model,
            base_url=os.environ.get("MEMORYD_LLM_BASE", "https://api.openai.com/v1"),
            max_context_tokens=int(os.environ.get("MEMORYD_MODEL_CONTEXT_TOKENS", "128000")),
            capabilities=("chat", "structured_json", "tool_calling"),
        ),
        "anthropic": ModelProfile(
            name="anthropic",
            provider="anthropic",
            model=anthropic_model,
            base_url=os.environ.get("MEMORYD_LLM_BASE", "https://api.anthropic.com"),
            max_context_tokens=int(os.environ.get("MEMORYD_MODEL_CONTEXT_TOKENS", "200000")),
            capabilities=("messages", "structured_json", "long_context"),
        ),
        "local-openai": ModelProfile(
            name="local-openai",
            provider="openai",
            model=os.environ.get("MEMORYD_LLM_MODEL", "local-model"),
            base_url=os.environ.get("MEMORYD_LLM_BASE", "http://localhost:11434/v1"),
            max_context_tokens=int(os.environ.get("MEMORYD_MODEL_CONTEXT_TOKENS", "32768")),
            capabilities=("chat", "local"),
        ),
    }


def list_model_profiles() -> list[str]:
    return sorted(_profiles())


def infer_profile_name() -> str:
    explicit = os.environ.get("MEMORYD_MODEL_PROFILE")
    if explicit:
        return explicit
    try:
        from .core import CFG
        if CFG.model_profile:
            return CFG.model_profile
    except Exception:  # noqa: BLE001 - profile inference must stay lightweight
        pass
    mode = os.environ.get("MEMORYD_LLM", "").lower()
    if mode == "mock":
        return "mock"
    if mode in ("openrouter", "openai", "anthropic"):
        return mode
    if os.environ.get("ANTHROPIC_API_KEY"):
        return "anthropic"
    if os.environ.get("OPENROUTER_API_KEY"):
        return "openrouter"
    if os.environ.get("OPENAI_API_KEY"):
        return "openai"
    return "mock"


def get_model_profile(name: str | None = None) -> ModelProfile:
    profiles = _profiles()
    selected = name or infer_profile_name()
    if selected in profiles:
        return profiles[selected]
    provider = os.environ.get("MEMORYD_LLM", "openai").lower() or "openai"
    return ModelProfile(
        name=selected,
        provider=provider,
        model=os.environ.get("MEMORYD_LLM_MODEL", selected),
        base_url=os.environ.get("MEMORYD_LLM_BASE"),
        capabilities=("chat",),
    )


def profile_to_llm_mode(profile: ModelProfile) -> str:
    if profile.provider in ("mock", "openrouter", "openai", "anthropic"):
        return profile.provider
    return "openai"
