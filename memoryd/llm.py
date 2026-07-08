"""LLM provider abstraction for the extractor.

Two backends:
  - AnthropicClient: real Messages API call (needs ANTHROPIC_API_KEY).
  - MockClient: deterministic, reads a JSON fixture; used by tests and CI.

Selected via MEMORYD_LLM = anthropic | mock (default: anthropic if key set,
else disabled — capture still archives everything; extraction can backfill
later, which is the point of raw-first ordering).
"""
from __future__ import annotations

import json
import os
import urllib.request
from pathlib import Path


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
        parts = [b.get("text", "") for b in data.get("content", []) if b.get("type") == "text"]
        return "\n".join(parts)


class MockClient:
    """Returns the contents of MEMORYD_LLM_MOCK_FILE verbatim. Deterministic."""

    def __init__(self) -> None:
        self.fixture = Path(os.environ.get("MEMORYD_LLM_MOCK_FILE", "/tmp/mock_llm.json"))

    def complete(self, system: str, user: str, max_tokens: int = 4000) -> str:
        if not self.fixture.exists():
            raise LLMError(f"mock fixture missing: {self.fixture}")
        return self.fixture.read_text()


def get_client():
    mode = os.environ.get("MEMORYD_LLM", "").lower()
    if mode == "mock":
        return MockClient()
    if mode == "anthropic" or os.environ.get("ANTHROPIC_API_KEY"):
        return AnthropicClient()
    return None  # extraction disabled; capture-only mode
