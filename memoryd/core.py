"""memoryd core: config, ids, content-addressed archive, ledger writer.

Slice architecture v1 — M1/M2. Raw archival is unconditional and never
blocks on anything downstream (spec §4.3).
"""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import shutil
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import psycopg
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

# ----------------------------------------------------------------- config

@dataclass
class Config:
    dsn: str = os.environ.get("MEMORYD_DSN", "postgresql://memoryd@localhost/memoryd")
    home: Path = field(default_factory=lambda: Path(os.environ.get("MEMORYD_HOME", "~/memory")).expanduser())
    port: int = int(os.environ.get("MEMORYD_PORT", "7437"))
    packet_token_budget: int = int(os.environ.get("MEMORYD_PACKET_TOKENS", "1500"))
    # per-agent memory visas (spec §6, governance). Override with
    # MEMORYD_VISAS='{"hermes": ["work_private","public"], ...}'
    default_scopes: tuple[str, ...] = ("work_private", "project_shared", "public")

    def visa(self, agent: str) -> list[str]:
        raw = os.environ.get("MEMORYD_VISAS", "")
        if raw:
            try:
                visas = json.loads(raw)
                if agent in visas:
                    return list(visas[agent])
                if "*" in visas:
                    return list(visas["*"])
            except json.JSONDecodeError:
                pass
        return list(self.default_scopes)

    @property
    def archive(self) -> Path:
        return self.home / "archive"

    @property
    def spool(self) -> Path:
        return self.home / "spool"

    def ensure_dirs(self) -> None:
        (self.archive / "objects" / "sha256").mkdir(parents=True, exist_ok=True)
        (self.archive / "fonds").mkdir(parents=True, exist_ok=True)
        self.spool.mkdir(parents=True, exist_ok=True)
        (self.home / "digest").mkdir(parents=True, exist_ok=True)


CFG = Config()
POOL: ConnectionPool | None = None


def pool() -> ConnectionPool:
    global POOL
    if POOL is None:
        from psycopg.rows import tuple_row

        def _reset(conn: psycopg.Connection) -> None:
            # handlers may set dict_row for their checkout; never let that
            # leak to the next borrower of the pooled connection
            conn.row_factory = tuple_row

        POOL = ConnectionPool(CFG.dsn, min_size=1, max_size=8, open=True, reset=_reset)
    return POOL

# ----------------------------------------------------------------- ids

_ULID_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"  # Crockford base32


def ulid() -> str:
    """Compact dependency-free ULID: 48-bit ms timestamp + 80-bit randomness."""
    ts = int(time.time() * 1000)
    rand = secrets.randbits(80)
    n = (ts << 80) | rand
    chars = []
    for _ in range(26):
        chars.append(_ULID_ALPHABET[n & 0x1F])
        n >>= 5
    return "".join(reversed(chars))


def new_id(prefix: str) -> str:
    return f"{prefix}_{ulid()}"


def barcode(ts: datetime, session_id: str, kind: str, content_hash: str) -> str:
    """Episodic barcode: 'this exact episode', distinct from semantic hash (spec A3)."""
    return f"{ts.strftime('%Y%m%dT%H%M%S')}|{session_id[:8]}|{kind}|{content_hash[:8]}"

# ----------------------------------------------------------------- archive (Fonds Keeper)

def archive_bytes(data: bytes, mime: str, fonds_path: str) -> str:
    """Store blob content-addressed; append manifest; symlink into fonds.

    Returns sha256. Idempotent: re-archiving identical bytes is a no-op
    except for the fonds link, preserving original order per source.
    """
    sha = hashlib.sha256(data).hexdigest()
    obj_dir = CFG.archive / "objects" / "sha256" / sha[:2] / sha[2:4]
    obj_path = obj_dir / sha
    if not obj_path.exists():
        obj_dir.mkdir(parents=True, exist_ok=True)
        tmp = obj_path.with_suffix(".tmp")
        tmp.write_bytes(data)
        os.replace(tmp, obj_path)  # atomic; blob is immutable from here on
        manifest = CFG.archive / "manifest.jsonl"
        with manifest.open("a", encoding="utf-8") as f:
            f.write(json.dumps({
                "sha256": sha,
                "bytes": len(data),
                "mime": mime,
                "first_seen": datetime.now(timezone.utc).isoformat(),
                "fonds_path": fonds_path,
            }) + "\n")
    # fonds symlink (original-order view); best effort, never fatal
    try:
        link = CFG.archive / "fonds" / fonds_path
        link.parent.mkdir(parents=True, exist_ok=True)
        if not link.exists():
            rel = os.path.relpath(obj_path, link.parent)
            os.symlink(rel, link)
    except OSError:
        pass
    return sha


def archive_file(path: Path, fonds_path: str, mime: str = "application/octet-stream") -> str:
    return archive_bytes(path.read_bytes(), mime, fonds_path)


def read_blob(sha: str) -> bytes:
    return (CFG.archive / "objects" / "sha256" / sha[:2] / sha[2:4] / sha).read_bytes()

# ----------------------------------------------------------------- ledger

def append_event(
    conn: psycopg.Connection,
    *,
    kind: str,
    session_id: str,
    ts: datetime | None = None,
    agent: str = "claude-code",
    project: str | None = None,
    raw_sha256: str | None = None,
    payload: dict | None = None,
    meta: bool = False,
) -> str:
    ts = ts or datetime.now(timezone.utc)
    payload = payload or {}
    content_hash = hashlib.sha256(
        json.dumps(payload, sort_keys=True, default=str).encode()
    ).hexdigest()
    eid = new_id("evt")
    conn.execute(
        """INSERT INTO events (id, ts, kind, session_id, agent, project,
                               raw_sha256, payload, meta, barcode)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
        (eid, ts, kind, session_id, agent, project, raw_sha256,
         Jsonb(payload), meta, barcode(ts, session_id, kind, content_hash)),
    )
    return eid
