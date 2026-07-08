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
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import psycopg
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

# ----------------------------------------------------------------- config

def _file_cfg() -> dict:
    """~/memory/config.json, written by `memoryd install`.

    Precedence everywhere is env > config.json > default — scheduled tasks
    (schtasks/systemd/launchd) inherit no shell exports, so the file is what
    makes autostarted daemons find the right DB. The file's *location* honors
    MEMORYD_HOME env only; a `home` key inside it relocates data, not the file.
    """
    p = Path(os.environ.get("MEMORYD_HOME", "~/memory")).expanduser() / "config.json"
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


_FILE_CFG = _file_cfg()
# persisted env (e.g. ANTHROPIC_API_KEY) for scheduled runs; real env wins
for _k, _v in (_FILE_CFG.get("env") or {}).items():
    os.environ.setdefault(_k, str(_v))


def _get(env: str, key: str, default: str) -> str:
    return os.environ.get(env) or str(_FILE_CFG.get(key) or "") or default


@dataclass
class Config:
    dsn: str = _get("MEMORYD_DSN", "dsn", "postgresql://memoryd@localhost/memoryd")
    home: Path = field(default_factory=lambda: Path(
        os.environ.get("MEMORYD_HOME") or _FILE_CFG.get("home") or "~/memory").expanduser())
    port: int = int(_get("MEMORYD_PORT", "port", "7437"))
    packet_token_budget: int = int(_get("MEMORYD_PACKET_TOKENS", "packet_tokens", "1500"))
    model_profile: str = _get("MEMORYD_MODEL_PROFILE", "model_profile", "")
    extractor_contract: str = _get("MEMORYD_EXTRACTOR_CONTRACT", "extractor_contract", "builtin_v1")
    semantic_policy: str = _get("MEMORYD_SEMANTIC_POLICY", "semantic_policy", "conservative_v1")
    recall_policy: str = _get("MEMORYD_RECALL_POLICY", "recall_policy", "heuristic_v1")
    packet_compiler: str = _get("MEMORYD_PACKET_COMPILER", "packet_compiler", "lane_v1")
    eval_profile: str = _get("MEMORYD_EVAL_PROFILE", "eval_profile", "default_v1")
    # per-agent memory visas (spec §6, governance). Override with
    # MEMORYD_VISAS='{"hermes": ["work_private","public"], ...}' or a
    # "visas" object in config.json.
    default_scopes: tuple[str, ...] = ("work_private", "project_shared", "public")

    def visa(self, agent: str) -> list[str]:
        visas = None
        raw = os.environ.get("MEMORYD_VISAS", "")
        if raw:
            try:
                visas = json.loads(raw)
            except json.JSONDecodeError:
                visas = None
        if visas is None:
            visas = _FILE_CFG.get("visas")
        if isinstance(visas, dict):
            if agent in visas:
                return list(visas[agent])
            if "*" in visas:
                return list(visas["*"])
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
_POOL_LOCK = threading.Lock()


def pool() -> ConnectionPool:
    global POOL
    if POOL is None:
        with _POOL_LOCK:  # HTTP handler threads race the capture worker on first use
            if POOL is None:
                from psycopg.rows import tuple_row

                def _reset(conn: psycopg.Connection) -> None:
                    # handlers may set dict_row for their checkout; never let that
                    # leak to the next borrower of the pooled connection
                    conn.row_factory = tuple_row

                # timeout=5: while the DB is down (e.g. Docker still booting),
                # fail requests fast instead of parking threads for 30s — the
                # recall hook gave up at 1.5s anyway.
                POOL = ConnectionPool(CFG.dsn, min_size=1, max_size=8, open=True,
                                      timeout=5, reset=_reset)
                # close at exit: Python 3.14 raises PythonFinalizationError
                # when joining the pool's worker threads at shutdown
                import atexit
                atexit.register(POOL.close)
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
