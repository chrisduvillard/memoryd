"""memoryd HTTP server — stdlib http.server, threaded, localhost-only.

Deliberately zero web-framework dependencies: the daemon must be boring,
auditable, and installable anywhere. Endpoints (spec §2):

  POST /recall          sync, budgeted -> {markdown, latency_ms, ...}
  POST /capture         ack-fast 202; ingestion queued (spool-backed)
  POST /capture-events  direct event ingestion (Hermes provider)
  POST /extract         queue extraction for a session
  POST /miss            manual/heuristic miss signal
  GET  /health
  POST /admin/rebuild-indexes   (S12: drop + regenerate disposable indexes)
"""
from __future__ import annotations

import json
import queue
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .core import CFG, new_id, pool
from .ingest import drain_spool, ingest_transcript
from .recall import build_packet

CAPTURE_Q: "queue.Queue[dict]" = queue.Queue()


def _capture_worker() -> None:
    while True:
        job = CAPTURE_Q.get()
        try:
            if job.get("extract_only"):
                from .extract import run_extraction
                run_extraction(job["session_id"])
                continue  # finally still runs task_done()
            ingest_transcript(job["transcript_path"], job["session_id"],
                              job.get("project"), job.get("trigger", "unknown"))
            if job.get("trigger") in ("session_end", "pre_compact"):
                from .extract import run_extraction
                run_extraction(job["session_id"])  # no-op if no LLM configured
        except Exception:  # noqa: BLE001 — spool for retry, never crash the worker
            spool_file = CFG.spool / f"{new_id('job')}.json"
            spool_file.write_text(json.dumps(job))
        finally:
            CAPTURE_Q.task_done()


class Handler(BaseHTTPRequestHandler):
    server_version = "memoryd/0.1"

    def _json(self, code: int, obj: dict) -> None:
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length", 0))
        if n == 0:
            return {}
        try:
            return json.loads(self.rfile.read(n))
        except json.JSONDecodeError:
            return {}

    def log_message(self, fmt: str, *args) -> None:  # quiet; ledger is the log
        pass

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/health":
            try:
                with pool().connection() as conn:
                    conn.execute("SELECT 1")
                self._json(200, {"ok": True, "ts": datetime.now(timezone.utc).isoformat()})
            except Exception as e:  # noqa: BLE001
                self._json(503, {"ok": False, "error": str(e)})
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        body = self._body()
        if self.path == "/recall":
            try:
                pkt = build_packet(
                    prompt=body.get("prompt", ""),
                    session_id=body.get("session_id", "unknown"),
                    project=body.get("project"),
                    agent=body.get("agent", "claude-code"),
                )
                self._json(200, pkt)
            except Exception as e:  # noqa: BLE001 — hook falls back to fail-open
                self._json(500, {"error": str(e)})
        elif self.path == "/capture":
            required = {"transcript_path", "session_id"}
            if not required <= body.keys():
                self._json(400, {"error": f"missing fields: {required - body.keys()}"})
                return
            CAPTURE_Q.put(body)
            self._json(202, {"queued": True})
        elif self.path == "/capture-events":
            # Direct event ingestion for agents that hand us turns in-process
            # (Hermes memory provider) instead of a transcript file. Small
            # payloads, synchronous ledger write, big bodies go to archive.
            required = {"session_id", "events"}
            if not required <= body.keys():
                self._json(400, {"error": f"missing fields: {required - body.keys()}"})
                return
            allowed = {"user_message", "agent_response", "tool_call", "tool_result",
                       "session_start", "session_end", "external_note", "delegation"}
            agent = body.get("agent", "unknown")
            project = body.get("project")
            stored = 0
            try:
                from .core import archive_bytes
                from datetime import datetime as _dt, timezone as _tz
                with pool().connection() as conn:
                    for ev in body["events"][:200]:
                        kind = ev.get("kind")
                        if kind not in allowed:
                            continue
                        payload = ev.get("payload") or {}
                        text = payload.get("text", "")
                        sha = None
                        if isinstance(text, str) and len(text) > 4000:
                            day = _dt.now(_tz.utc)
                            sha = archive_bytes(
                                text.encode(), "text/plain",
                                f"{agent}/{day:%Y/%m/%d}/{body['session_id']}-{stored}.txt")
                            payload = {**payload, "text": text[:4000],
                                       "truncated": True}
                        from .core import append_event as _ae
                        _ae(conn, kind=kind, session_id=body["session_id"],
                            agent=agent, project=project, raw_sha256=sha,
                            payload=payload)
                        stored += 1
                    conn.commit()
                self._json(200, {"ok": True, "stored": stored})
            except Exception as e:  # noqa: BLE001
                self._json(500, {"error": str(e)})
        elif self.path == "/extract":
            sid = body.get("session_id")
            if not sid:
                self._json(400, {"error": "session_id required"})
                return
            CAPTURE_Q.put({"extract_only": True, "session_id": sid})
            self._json(202, {"queued": True})
        elif self.path == "/miss":
            with pool().connection() as conn:
                conn.execute(
                    "INSERT INTO miss_signals (session_id, signal, detail) VALUES (%s,%s,%s)",
                    (body.get("session_id"), body.get("signal", "manual"),
                     json.dumps(body.get("detail", {}))))
                conn.commit()
            self._json(200, {"ok": True})
        elif self.path == "/admin/rebuild-indexes":
            try:
                from .embed import get_embedder, to_pgvector
                emb = get_embedder()
                n = 0
                with pool().connection() as conn:
                    conn.execute("TRUNCATE mem_embeddings")
                    conn.execute("REINDEX INDEX memories_fts")
                    rows = conn.execute(
                        "SELECT id, text FROM memories "
                        "WHERE status IN ('active','candidate')").fetchall()
                    for i in range(0, len(rows), 64):
                        batch = rows[i:i + 64]
                        vecs = emb.embed([r[1] for r in batch])
                        for (mid, _), v in zip(batch, vecs):
                            conn.execute(
                                "INSERT INTO mem_embeddings (memory_id, model, embedding) "
                                "VALUES (%s,%s,%s::vector)", (mid, emb.model, to_pgvector(v)))
                        n += len(batch)
                    conn.commit()
                self._json(200, {"ok": True, "reembedded": n, "model": emb.model})
            except Exception as e:  # noqa: BLE001
                self._json(500, {"ok": False, "error": str(e)})
        else:
            self._json(404, {"error": "not found"})


def _drain_spool_bg() -> None:
    try:
        drained = drain_spool()
        if drained:
            print(f"memoryd: drained {drained} spooled capture(s)")
    except Exception as e:  # noqa: BLE001 — spool retries on next start/microsleep
        print(f"memoryd: spool drain failed: {e}")


def main() -> None:
    CFG.ensure_dirs()
    threading.Thread(target=_capture_worker, daemon=True).start()
    # background: with the DB down (Docker still booting at logon), a
    # synchronous drain would block the socket bind for pool-timeout × N files
    threading.Thread(target=_drain_spool_bg, daemon=True).start()
    srv = ThreadingHTTPServer(("127.0.0.1", CFG.port), Handler)
    print(f"memoryd listening on 127.0.0.1:{CFG.port}  home={CFG.home}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        srv.shutdown()


if __name__ == "__main__":
    main()
