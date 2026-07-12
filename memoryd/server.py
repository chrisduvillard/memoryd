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
import hashlib
import queue
import re
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .core import (
    ArchiveOccurrenceCollision,
    CFG,
    find_archive_request_identity,
    new_id,
    pool,
)
from .ingest import drain_spool
from .recall import build_packet
from .spool import (
    JobIdentityCollision,
    enqueue_capture,
    enqueue_extraction,
    find_request_identity,
)

CAPTURE_Q: "queue.Queue[dict]" = queue.Queue()
ADMIN_POST_ENDPOINTS = {
    "/admin/eval",
    "/admin/replay",
    "/admin/policies",
    "/admin/model-profiles",
    "/admin/export-evidence",
    "/admin/rebuild-indexes",
}


def _nonempty_string(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


class RequestIdentityCollision(ValueError):
    pass


_DURABLE_REQUEST_MATCH = object()


def _request_identity(body: dict) -> tuple[str, str] | None:
    if "request_id" not in body:
        return None
    request_id = body["request_id"]
    if not _nonempty_string(request_id):
        raise ValueError("request_id must be a nonempty string")
    canonical = {key: value for key, value in body.items()
                 if key != "request_id"}
    encoded = json.dumps(
        canonical, sort_keys=True, separators=(",", ":"),
        ensure_ascii=False).encode()
    return request_id, hashlib.sha256(encoded).hexdigest()


def _start_idempotent_request(conn, *, request_id: str,
                              endpoint: str,
                              body_sha256: str) -> dict | object | None:
    # Serialize claim/check/mutation/record for one identity. The transaction
    # lock prevents concurrent first calls from both applying their mutation.
    conn.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
        (request_id,))
    row = conn.execute(
        "SELECT endpoint, body_sha256, response FROM api_request_ledger "
        "WHERE request_id=%s", (request_id,)).fetchone()
    try:
        spool_identity = find_request_identity(CFG.spool, request_id)
        archive_identity = find_archive_request_identity(
            CFG.archive, request_id)
    except (JobIdentityCollision, ArchiveOccurrenceCollision) as exc:
        raise RequestIdentityCollision("request_id collision") from exc
    durable_identities = {
        identity for identity in (spool_identity, archive_identity)
        if identity is not None}
    if len(durable_identities) > 1:
        raise RequestIdentityCollision("request_id collision")
    durable = next(iter(durable_identities), None)
    if row is None:
        if durable is None:
            return None
        if durable != (endpoint, body_sha256):
            raise RequestIdentityCollision("request_id collision")
        return _DURABLE_REQUEST_MATCH
    if isinstance(row, dict):
        saved_endpoint = row["endpoint"]
        saved_digest = row["body_sha256"]
        response = row["response"]
    else:
        saved_endpoint, saved_digest, response = row
    if saved_endpoint != endpoint or saved_digest != body_sha256:
        raise RequestIdentityCollision("request_id collision")
    if durable is not None and durable != (endpoint, body_sha256):
        raise RequestIdentityCollision("request_id collision")
    if isinstance(response, str):
        response = json.loads(response)
    return {**response, "duplicate": True}


def _record_idempotent_request(conn, *, request_id: str, endpoint: str,
                               body_sha256: str, response: dict) -> None:
    conn.execute(
        "INSERT INTO api_request_ledger "
        "(request_id, endpoint, body_sha256, response) VALUES (%s,%s,%s,%s::jsonb)",
        (request_id, endpoint, body_sha256,
         json.dumps(response, sort_keys=True, separators=(",", ":"))))


def _store_capture_events(conn, body: dict) -> int:
    from .adapters import event_to_envelope
    from .core import append_event, archive_bytes

    allowed = {"user_message", "agent_response", "tool_call", "tool_result",
               "session_start", "session_end", "external_note", "delegation"}
    agent = body.get("agent", "unknown")
    project = body.get("project")
    request_identity = _request_identity(body)
    stored = 0
    for ev in body["events"][:200]:
        envelope = event_to_envelope({
            **ev,
            "session_id": body["session_id"],
            "agent": agent,
            "project": project,
        }, runtime=agent)
        kind = envelope["event_type"]
        if (kind not in allowed and
                not re.match(r"^[a-z][a-z0-9_]{1,63}$", str(kind))):
            continue
        payload = ev.get("payload") or {}
        payload = {
            **payload,
            "_adapter": {
                "runtime": envelope["runtime"],
                "parent_session_id": envelope["parent_session_id"],
                "content_ref": envelope["content_ref"],
            },
        }
        text = payload.get("text", "")
        sha = None
        if isinstance(text, str) and len(text) > 4000:
            day = datetime.now(timezone.utc)
            sha = archive_bytes(
                text.encode(), "text/plain",
                f"{agent}/{day:%Y/%m/%d}/{body['session_id']}-{stored}.txt",
                ingest_job_id=(
                    f"{body['request_id']}:event:{stored}"
                    if request_identity is not None else None),
                request_id=(
                    request_identity[0]
                    if request_identity is not None else None),
                request_endpoint=(
                    "/capture-events"
                    if request_identity is not None else None),
                request_body_sha256=(
                    request_identity[1]
                    if request_identity is not None else None))
            payload = {**payload, "text": text[:4000], "truncated": True}
        append_event(
            conn, kind=kind, session_id=body["session_id"],
            agent=agent, project=project, raw_sha256=sha, payload=payload)
        stored += 1
    return stored


def _capture_worker() -> None:
    while True:
        job = CAPTURE_Q.get()
        try:
            if job.get("drain_spool"):
                drain_spool()
        except Exception as exc:  # noqa: BLE001 — keep the worker alive
            print(f"memoryd: capture worker failed: {exc}")
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
        if not isinstance(body, dict):
            self._json(400, {"error": "request body must be an object"})
            return
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
            trigger = body.get("trigger", "unknown")
            project = body.get("project")
            invalid = [
                field for field, value in (
                    ("transcript_path", body["transcript_path"]),
                    ("session_id", body["session_id"]),
                    ("trigger", trigger),
                ) if not _nonempty_string(value)]
            if project is not None and not _nonempty_string(project):
                invalid.append("project")
            if invalid:
                self._json(400, {"error": f"invalid fields: {sorted(invalid)}"})
                return
            try:
                enqueue_capture(
                    spool_root=CFG.spool,
                    transcript_path=Path(body["transcript_path"]),
                    session_id=body["session_id"],
                    project=project,
                    trigger=trigger,
                )
            except Exception as exc:  # noqa: BLE001 — report persistence failure
                self._json(
                    500, {"error": f"capture could not be persisted: {exc}"})
                return
            CAPTURE_Q.put({"drain_spool": True})
            self._json(202, {"queued": True})
        elif self.path == "/capture-events":
            # Direct event ingestion for agents that hand us turns in-process
            # (Hermes memory provider) instead of a transcript file. Small
            # payloads, synchronous ledger write, big bodies go to archive.
            required = {"session_id", "events"}
            if not required <= body.keys():
                self._json(400, {"error": f"missing fields: {required - body.keys()}"})
                return
            try:
                identity = _request_identity(body)
                with pool().connection() as conn:
                    if identity is not None:
                        request_id, digest = identity
                        duplicate = _start_idempotent_request(
                            conn, request_id=request_id,
                            endpoint=self.path, body_sha256=digest)
                        if isinstance(duplicate, dict):
                            self._json(200, duplicate)
                            return
                    stored = _store_capture_events(conn, body)
                    response = {"ok": True, "stored": stored}
                    if identity is not None:
                        response.update({
                            "request_id": request_id, "duplicate": False})
                        _record_idempotent_request(
                            conn, request_id=request_id, endpoint=self.path,
                            body_sha256=digest, response=response)
                    conn.commit()
                self._json(200, response)
            except (RequestIdentityCollision, ArchiveOccurrenceCollision):
                self._json(409, {"error": "request_id collision"})
            except ValueError as e:
                self._json(400, {"error": str(e)})
            except Exception as e:  # noqa: BLE001
                self._json(500, {"error": str(e)})
        elif self.path == "/extract":
            sid = body.get("session_id")
            if not _nonempty_string(sid):
                self._json(400, {"error": "session_id required"})
                return
            try:
                identity = _request_identity(body)
                if identity is None:
                    enqueue_extraction(spool_root=CFG.spool, session_id=sid)
                    response = {"queued": True}
                else:
                    request_id, digest = identity
                    with pool().connection() as conn:
                        duplicate = _start_idempotent_request(
                            conn, request_id=request_id,
                            endpoint=self.path, body_sha256=digest)
                        if isinstance(duplicate, dict):
                            self._json(202, duplicate)
                            return
                        job = enqueue_extraction(
                            spool_root=CFG.spool, session_id=sid,
                            request_id=request_id, request_endpoint=self.path,
                            request_body_sha256=digest)
                        response = {
                            "queued": True, "request_id": request_id,
                            "duplicate": False}
                        _record_idempotent_request(
                            conn, request_id=request_id, endpoint=self.path,
                            body_sha256=digest, response=response)
                        conn.commit()
                    if job["duplicate"]:
                        response["duplicate"] = True
            except (RequestIdentityCollision, JobIdentityCollision):
                self._json(409, {"error": "request_id collision"})
                return
            except ValueError as exc:
                self._json(400, {"error": str(exc)})
                return
            except Exception as exc:  # noqa: BLE001 — report persistence failure
                self._json(
                    500, {"error": f"extraction could not be persisted: {exc}"})
                return
            CAPTURE_Q.put({"drain_spool": True})
            self._json(202, response)
        elif self.path == "/miss":
            try:
                identity = _request_identity(body)
                with pool().connection() as conn:
                    if identity is not None:
                        request_id, digest = identity
                        duplicate = _start_idempotent_request(
                            conn, request_id=request_id,
                            endpoint=self.path, body_sha256=digest)
                        if isinstance(duplicate, dict):
                            self._json(200, duplicate)
                            return
                    conn.execute(
                        "INSERT INTO miss_signals (session_id, signal, detail) "
                        "VALUES (%s,%s,%s)",
                        (body.get("session_id"), body.get("signal", "manual"),
                         json.dumps(body.get("detail", {}))))
                    response = {"ok": True}
                    if identity is not None:
                        response.update({
                            "request_id": request_id, "duplicate": False})
                        _record_idempotent_request(
                            conn, request_id=request_id, endpoint=self.path,
                            body_sha256=digest, response=response)
                    conn.commit()
                self._json(200, response)
            except RequestIdentityCollision:
                self._json(409, {"error": "request_id collision"})
            except ValueError as e:
                self._json(400, {"error": str(e)})
        elif self.path == "/admin/model-profiles":
            from .model_gateway import get_model_profile, list_model_profiles
            profiles = [get_model_profile(name).to_dict() for name in list_model_profiles()]
            self._json(200, {"ok": True, "profiles": profiles})
        elif self.path == "/admin/policies":
            from .contracts import get_extractor_contract, list_extractor_contracts
            from .policies import (
                get_packet_compiler,
                get_recall_policy,
                list_packet_compilers,
                list_recall_policies,
            )
            from .semantic_policies import get_semantic_policy, list_semantic_policies
            self._json(200, {
                "ok": True,
                "recall_policies": [
                    get_recall_policy(name).to_dict() for name in list_recall_policies()
                ],
                "semantic_policies": [
                    get_semantic_policy(name).to_dict() for name in list_semantic_policies()
                ],
                "packet_compilers": [
                    get_packet_compiler(name).to_dict() for name in list_packet_compilers()
                ],
                "extractor_contracts": [
                    get_extractor_contract(name).to_dict() for name in list_extractor_contracts()
                ],
            })
        elif self.path == "/admin/eval":
            from psycopg.types.json import Jsonb
            from .evaluator import run_static_eval
            cases = body.get("cases")
            if cases is None:
                try:
                    with pool().connection() as conn:
                        rows = conn.execute(
                            "SELECT id, kind, input, expected FROM eval_cases "
                            "WHERE enabled ORDER BY created_at, id LIMIT 100").fetchall()
                    cases = [
                        {"id": r[0], "kind": r[1], "input": r[2], "expected": r[3]}
                        for r in rows
                    ]
                except Exception:  # noqa: BLE001 - eval can still run ad hoc
                    cases = []
            result = run_static_eval(cases=cases)
            try:
                with pool().connection() as conn:
                    conn.execute(
                        "INSERT INTO eval_runs (id, profile, status, summary, metrics) "
                        "VALUES (%s,%s,%s,%s,%s)",
                        (new_id("eval"), result["model_profile"],
                         "pass" if result["failed"] == 0 else "fail",
                         Jsonb(result), Jsonb({
                             "cases": result["cases"],
                             "passed": result["passed"],
                             "failed": result["failed"],
                         })))
                    conn.commit()
            except Exception:  # noqa: BLE001 - migration may not be applied yet
                result["recorded"] = False
            else:
                result["recorded"] = True
            self._json(200, {"ok": True, "eval": result})
        elif self.path == "/admin/replay":
            limit = int(body.get("limit", 20))
            try:
                with pool().connection() as conn:
                    rows = conn.execute(
                        "SELECT session_id, project, query_text, packet, latency_ms, agent "
                        "FROM recall_log ORDER BY id DESC LIMIT %s", (limit,)).fetchall()
                replay = [{
                    "session_id": r[0],
                    "project": r[1],
                    "query_text": r[2],
                    "packet": r[3],
                    "latency_ms": r[4],
                    "agent": r[5] if len(r) > 5 else None,
                } for r in rows]
                self._json(200, {"ok": True, "replay": replay})
            except Exception as e:  # noqa: BLE001
                self._json(500, {"ok": False, "error": str(e)})
        elif self.path == "/admin/export-evidence":
            bundle: dict = {"ok": True, "tables": {}}
            try:
                with pool().connection() as conn:
                    for table in ("model_runs", "policy_runs", "eval_runs", "packet_runs"):
                        try:
                            rows = conn.execute(f"SELECT row_to_json(t) FROM "
                                                f"(SELECT * FROM {table} ORDER BY ts DESC LIMIT 50) t").fetchall()
                            bundle["tables"][table] = [r[0] for r in rows]
                        except Exception as e:  # noqa: BLE001
                            bundle["tables"][table] = {"error": str(e)}
                self._json(200, bundle)
            except Exception as e:  # noqa: BLE001
                self._json(500, {"ok": False, "error": str(e)})
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
        stats = drain_spool()
        if any(stats.values()):
            print("memoryd: spool " + ", ".join(
                f"{key}={value}" for key, value in stats.items()))
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
