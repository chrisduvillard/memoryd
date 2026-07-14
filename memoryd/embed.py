"""Embedding providers (M5). Pluggable via MEMORYD_EMBED:

  voyage  — Voyage AI (VOYAGE_API_KEY; model MEMORYD_EMBED_MODEL, default voyage-3)
  openai  — any OpenAI-compatible /v1/embeddings (OPENAI_API_KEY optional for
            local servers; MEMORYD_EMBED_BASE covers Ollama/LM Studio/OpenRouter)
  hash    — built-in deterministic feature-hash embedder. Zero deps, offline,
            adequate for lexical-overlap similarity; NOT semantic. Default when
            no provider configured, so the pipeline always works — swap to a
            real provider for paraphrase-level recall quality.

All vectors are normalized to EMBED_DIM (schema: 1024) by truncate/pad +
L2-renormalize, so provider switches never require a schema change — only an
index rebuild (/admin/rebuild-indexes), which is the S12 path by design.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import urllib.request

EMBED_DIM = int(os.environ.get("MEMORYD_EMBED_DIM", "1024"))


class EmbedError(RuntimeError):
    pass


def _fit(vec: list[float]) -> list[float]:
    """Truncate/pad to EMBED_DIM and L2-normalize (matryoshka-style truncation)."""
    v = vec[:EMBED_DIM] + [0.0] * max(0, EMBED_DIM - len(vec))
    norm = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / norm for x in v]


class HashEmbedder:
    """Deterministic feature hashing over word + char-trigram features with
    signed buckets. Same text -> identical vector; lexically overlapping
    texts -> high cosine. No external calls, no dependencies."""

    model = "hash-v1"

    def embed(self, texts: list[str]) -> list[list[float]]:
        out = []
        for t in texts:
            v = [0.0] * EMBED_DIM
            feats: list[str] = []
            words = re.findall(r"[a-z0-9_]+", t.lower())
            feats.extend(words)
            feats.extend(f"{a}_{b}" for a, b in zip(words, words[1:]))
            joined = " ".join(words)
            feats.extend(joined[i:i + 3] for i in range(len(joined) - 2))
            for f in feats:
                h = int.from_bytes(hashlib.blake2b(f.encode(), digest_size=8).digest(), "big")
                idx = h % EMBED_DIM
                sign = 1.0 if (h >> 63) & 1 else -1.0
                v[idx] += sign
            out.append(_fit(v))
        return out


class VoyageEmbedder:
    def __init__(self) -> None:
        self.model = os.environ.get("MEMORYD_EMBED_MODEL", "voyage-3")
        self.key = os.environ.get("VOYAGE_API_KEY", "")
        if not self.key:
            raise EmbedError("VOYAGE_API_KEY not set")

    def embed(self, texts: list[str]) -> list[list[float]]:
        req = urllib.request.Request(
            "https://api.voyageai.com/v1/embeddings",
            data=json.dumps({"model": self.model, "input": texts,
                             "output_dimension": EMBED_DIM}).encode(),
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {self.key}"})
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.loads(r.read())
        return [_fit(d["embedding"]) for d in data["data"]]


class OpenAIEmbedder:
    """OpenAI-compatible /v1/embeddings — also Ollama, LM Studio, OpenRouter."""

    model = os.environ.get("MEMORYD_EMBED_MODEL", "text-embedding-3-small")

    def __init__(self) -> None:
        self.base = os.environ.get("MEMORYD_EMBED_BASE", "https://api.openai.com").rstrip("/")
        self.key = os.environ.get("OPENAI_API_KEY", "local")

    def embed(self, texts: list[str]) -> list[list[float]]:
        body: dict = {"model": self.model, "input": texts}
        if "openai.com" in self.base:
            body["dimensions"] = EMBED_DIM
        req = urllib.request.Request(
            f"{self.base}/v1/embeddings", data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {self.key}"})
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.loads(r.read())
        return [_fit(d["embedding"]) for d in data["data"]]


def get_embedder():
    mode = os.environ.get("MEMORYD_EMBED", "hash").lower()
    if mode == "voyage":
        return VoyageEmbedder()
    if mode == "openai":
        return OpenAIEmbedder()
    return HashEmbedder()


def to_pgvector(v: list[float]) -> str:
    return "[" + ",".join(f"{x:.6f}" for x in v) + "]"
