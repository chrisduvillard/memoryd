#!/usr/bin/env python3
"""Real PostgreSQL promotion tests for idempotency and backup recovery.

The ``idempotency`` phase runs while the daemon is serving.  The
``backup-restore`` phase must run after it has been stopped.
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import time
import urllib.error
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo

from memoryd.backup import create_backup, restore_backup, verify_snapshot


DSN = os.environ.get("MEMORYD_DSN", "")
HOME = (Path(os.environ["MEMORYD_HOME"])
        if os.environ.get("MEMORYD_HOME") else None)
PORT = int(os.environ.get("MEMORYD_PORT", "7437"))
BASE_URL = f"http://127.0.0.1:{PORT}"
EXPECTED_MIGRATIONS = [
    "001_init.sql",
    "002_extraction.sql",
    "003_multi_agent.sql",
    "004_quarantine_event.sql",
    "005_bitter_lesson.sql",
    "006_durable_capture.sql",
    "007_api_request_ledger.sql",
]


def _post(path: str, body: dict) -> tuple[int, dict]:
    request = urllib.request.Request(
        BASE_URL + path,
        data=json.dumps(body, separators=(",", ":")).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


def _count(query: str, parameters: tuple[object, ...]) -> int:
    with psycopg.connect(DSN) as connection:
        return int(connection.execute(query, parameters).fetchone()[0])


def _wait_for_ledger(request_id: str, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _count(
            "SELECT count(*) FROM api_request_ledger WHERE request_id=%s",
            (request_id,),
        ) == 1:
            return
        time.sleep(0.05)
    raise AssertionError(f"request {request_id} was not committed before timeout")


def _send_without_reading_response(path: str, body: dict) -> socket.socket:
    """Send a complete request, then deliberately discard the HTTP response."""
    payload = json.dumps(body, separators=(",", ":")).encode("utf-8")
    request = (
        f"POST {path} HTTP/1.1\r\n"
        f"Host: 127.0.0.1:{PORT}\r\n"
        "Content-Type: application/json\r\n"
        f"Content-Length: {len(payload)}\r\n"
        "Connection: close\r\n\r\n"
    ).encode("ascii") + payload
    connection = socket.create_connection(("127.0.0.1", PORT), timeout=10)
    connection.sendall(request)
    # The write half remains valid, but this client can no longer receive the
    # successful response.  Polling the independent ledger connection below
    # proves the server committed before the retry.
    connection.shutdown(socket.SHUT_RD)
    return connection


def run_real_idempotency() -> None:
    token = uuid.uuid4().hex
    concurrent_request = f"ci-concurrent-{token}"
    concurrent_session = f"ci-concurrent-session-{token}"
    body = {
        "request_id": concurrent_request,
        "session_id": concurrent_session,
        "agent": "ci",
        "events": [{
            "kind": "user_message",
            "payload": {"text": "concurrent request must commit once"},
        }],
    }
    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(lambda _index: _post("/capture-events", body),
                                    range(8)))
    assert all(status == 200 for status, _response in results), results
    responses = [response for _status, response in results]
    assert sum(response["duplicate"] is False for response in responses) == 1
    assert sum(response["duplicate"] is True for response in responses) == 7
    assert _count(
        "SELECT count(*) FROM api_request_ledger WHERE request_id=%s",
        (concurrent_request,),
    ) == 1
    assert _count(
        "SELECT count(*) FROM events WHERE session_id=%s AND kind='user_message'",
        (concurrent_session,),
    ) == 1

    lost_request = f"ci-lost-response-{token}"
    lost_session = f"ci-lost-response-session-{token}"
    lost_body = {
        "request_id": lost_request,
        "session_id": lost_session,
        "agent": "ci",
        "events": [{
            "kind": "user_message",
            "payload": {"text": "the first successful response is discarded"},
        }],
    }
    discarded = _send_without_reading_response("/capture-events", lost_body)
    try:
        _wait_for_ledger(lost_request)
    finally:
        discarded.close()
    status, retry = _post("/capture-events", lost_body)
    assert status == 200, retry
    assert retry["duplicate"] is True, retry
    assert _count(
        "SELECT count(*) FROM api_request_ledger WHERE request_id=%s",
        (lost_request,),
    ) == 1
    assert _count(
        "SELECT count(*) FROM events WHERE session_id=%s AND kind='user_message'",
        (lost_session,),
    ) == 1
    print("real PostgreSQL request-id concurrency/lost-response checks passed")


def _migration_inventory(dsn: str) -> list[str]:
    with psycopg.connect(dsn) as connection:
        return [row[0] for row in connection.execute(
            "SELECT filename FROM schema_migrations ORDER BY filename")]


def _write_source_config() -> None:
    assert HOME is not None
    HOME.mkdir(parents=True, exist_ok=True)
    HOME.chmod(0o700)
    config = HOME / "config.json"
    config.write_text(json.dumps({"dsn": DSN, "home": str(HOME)}),
                      encoding="utf-8")
    config.chmod(0o600)
    for name in ("archive", "spool"):
        path = HOME / name
        path.mkdir(exist_ok=True)
        path.chmod(0o700)


def run_real_backup_restore() -> None:
    assert HOME is not None
    assert _migration_inventory(DSN) == EXPECTED_MIGRATIONS
    _write_source_config()
    source_events = _count("SELECT count(*) FROM events", ())
    source_requests = _count("SELECT count(*) FROM api_request_ledger", ())
    root = HOME.parent / f"backup-drill-{uuid.uuid4().hex}"
    target_home = HOME.parent / f"restore-home-{uuid.uuid4().hex}"
    snapshot = create_backup(output=root, home=HOME, retain=14)
    verification = verify_snapshot(snapshot)
    assert verification.ok, verification.reason

    database = f"memoryd_restore_{uuid.uuid4().hex}"
    admin_dsn = make_conninfo(DSN, dbname="postgres")
    target_dsn = make_conninfo(DSN, dbname=database)
    with psycopg.connect(admin_dsn, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database)))
    try:
        restored = restore_backup(
            snapshot, target_dsn=target_dsn, target_home=target_home)
        assert restored == target_home
        assert (target_home / "config.json").is_file()
        assert (target_home / "archive").is_dir()
        assert (target_home / "spool").is_dir()
        assert _migration_inventory(target_dsn) == EXPECTED_MIGRATIONS
        with psycopg.connect(target_dsn) as connection:
            restored_events = int(connection.execute(
                "SELECT count(*) FROM events").fetchone()[0])
            restored_requests = int(connection.execute(
                "SELECT count(*) FROM api_request_ledger").fetchone()[0])
        assert restored_events == source_events
        assert restored_requests == source_requests
    finally:
        with psycopg.connect(admin_dsn, autocommit=True) as admin:
            admin.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname=%s AND pid <> pg_backend_pid()",
                (database,),
            )
            admin.execute(sql.SQL("DROP DATABASE IF EXISTS {}").format(
                sql.Identifier(database)))
    print(f"real backup/verify/restore drill passed: {snapshot}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=("idempotency", "backup-restore"))
    phase = parser.parse_args().phase
    missing = [name for name in ("MEMORYD_DSN", "MEMORYD_HOME")
               if not os.environ.get(name)]
    if missing:
        parser.error(f"required environment variables are missing: {missing}")
    if phase == "idempotency":
        run_real_idempotency()
    else:
        run_real_backup_restore()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
