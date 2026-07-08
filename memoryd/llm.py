"""LLM provider abstraction for the extractor.

Three backends:
  - AnthropicClient: Anthropic Messages API (ANTHROPIC_API_KEY).
  - OpenAIChatClient: any OpenAI-compatible /chat/completions — OpenRouter
    (pick any vendor's model by slug), Ollama, LM Studio, vLLM.
  - MockClient: deterministic, reads a JSON fixture; used by tests and CI.

Selected via MEMORYD_LLM = anthropic | openrouter | openai | mock. Unset:
anthropic if ANTHROPIC_API_KEY is set, else openrouter if OPENROUTER_API_KEY
is set, else disabled — capture still archives everything; extraction
backfills later, which is the point of raw-first ordering.
"""
from __future__ import annotations

import json
import os
import tempfile
import urllib.request
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .model_gateway import ModelProfile


class LLMError(RuntimeError):
    pass


class AnthropicClient:
    def __init__(self) -> None:
        self.api_key = os.environ.get("ANTHROPIC_API_KEY", "")
        self.base = os.environ.get("MEMORYD_LLM_BASE", "https://api.anthropic.com")
        self.model = os.environ.get("MEMORYD_LLM_MODEL", "claude-haiku-4-5-20251001")
        if not self.api_key:
            raise LLMError("ANTHROPIC_API_KEY not set")

    def complete(self, system: str, user: str, max_tokens: int = 4000) -> str:
        req = urllib.request.Request(
            f"{self.base}/v1/messages",
            data=json.dumps({
                "model": self.model,
                "max_tokens": max_tokens,
                "system": system,
                "messages": [{"role": "user", "content": user}],
            }).encode(),
            headers={
                "Content-Type": "application/json",
                "x-api-key": self.api_key,
                "anthropic-version": "2023-06-01",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                data = json.loads(r.read())
        except urllib.error.HTTPError as e:
            raise LLMError(f"anthropic api {e.code}: {e.read()[:500]!r}") from e
        except Exception as e:  # noqa: BLE001 — URLError/timeout/socket/bad JSON: same contract
            raise LLMError(f"anthropic api request failed: {e!r}") from e
        parts = [b.get("text", "") for b in data.get("content", []) if b.get("type") == "text"]
        return "\n".join(parts)


class OpenAIChatClient:
    """OpenAI-compatible /chat/completions — OpenRouter, Ollama, LM Studio, vLLM.

    MEMORYD_LLM=openrouter -> base https://openrouter.ai/api/v1 (OPENROUTER_API_KEY);
    MEMORYD_LLM=openai     -> base https://api.openai.com/v1   (OPENAI_API_KEY).
    MEMORYD_LLM_BASE overrides either — e.g. http://localhost:11434/v1 for a
    keyless local Ollama. MEMORYD_LLM_MODEL picks the model (OpenRouter slugs
    like "anthropic/claude-haiku-4.5" reach any vendor).
    """

    def __init__(self, flavor: str = "openrouter") -> None:
        default_base = ("https://openrouter.ai/api/v1" if flavor == "openrouter"
                        else "https://api.openai.com/v1")
        self.base = os.environ.get("MEMORYD_LLM_BASE", default_base).rstrip("/")
        key = os.environ.get("OPENROUTER_API_KEY") if flavor == "openrouter" else None
        self.api_key = key or os.environ.get("OPENAI_API_KEY") or "local"
        # openrouter default chosen by benching the real extraction pipeline
        # (validator-scored) across 6 small models, 2026-07: gemini-3.5-flash
        # extracted standing rules most reliably with zero malformed outputs.
        self.model = os.environ.get(
            "MEMORYD_LLM_MODEL",
            "google/gemini-3.5-flash" if flavor == "openrouter" else "gpt-4o-mini")

    def complete(self, system: str, user: str, max_tokens: int = 4000) -> str:
        req = urllib.request.Request(
            f"{self.base}/chat/completions",
            data=json.dumps({
                "model": self.model,
                "max_tokens": max_tokens,
                "messages": [{"role": "system", "content": system},
                             {"role": "user", "content": user}],
            }).encode(),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                data = json.loads(r.read())
        except urllib.error.HTTPError as e:
            raise LLMError(f"chat api {e.code}: {e.read()[:500]!r}") from e
        except Exception as e:  # noqa: BLE001 — URLError/timeout/socket/bad JSON: same contract
            raise LLMError(f"chat api request failed: {e!r}") from e
        try:
            return data["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError) as e:
            raise LLMError(f"chat api unexpected response: {str(data)[:300]}") from e


class MockClient:
    """Returns the contents of MEMORYD_LLM_MOCK_FILE verbatim. Deterministic."""

    def __init__(self) -> None:
        self.fixture = Path(os.environ.get("MEMORYD_LLM_MOCK_FILE")
                            or Path(tempfile.gettempdir()) / "mock_llm.json")

    def complete(self, system: str, user: str, max_tokens: int = 4000) -> str:
        if not self.fixture.exists():
            raise LLMError(f"mock fixture missing: {self.fixture}")
        return self.fixture.read_text(encoding="utf-8")


def get_client(profile: "ModelProfile | None" = None):
    if profile is not None:
        from .model_gateway import profile_to_llm_mode
        mode = profile_to_llm_mode(profile)
    else:
        mode = os.environ.get("MEMORYD_LLM", "").lower()
    if mode == "mock":
        return MockClient()
    if mode in ("openrouter", "openai"):
        return OpenAIChatClient(mode)
    if mode == "anthropic" or os.environ.get("ANTHROPIC_API_KEY"):
        return AnthropicClient()
    if os.environ.get("OPENROUTER_API_KEY"):
        return OpenAIChatClient("openrouter")
    return None  # extraction disabled; capture-only mode
