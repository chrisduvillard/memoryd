from __future__ import annotations

import contextlib
import errno
import hashlib
import io
import json
import multiprocessing
import os
import subprocess
import sys
import tempfile
import threading
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from memoryd import core, doctor, ingest, server, spool
from memoryd.cli import _spool_counts, status
from memoryd.doctor import (
    inspect_archive,
    inspect_spool,
    main as doctor_main,
    repair_archive,
    repair_spool,
)
from memoryd.hook import capture
from memoryd.ingest import _classify_all
from memoryd.spool import (
    dead_letter_reason_path,
    enqueue_capture,
    ensure_layout,
    validate_blob,
)


def _write_extraction_job(root: Path, name: str, session_id: str) -> Path:
    path = ensure_layout(root)["incoming"] / name
    path.write_text(json.dumps({
        "schema_version": 2,
        "job_id": name.removesuffix(".json"),
        "kind": "extraction",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "session_id": session_id,
        "attempts": 0,
        "last_error": None,
        "next_attempt_at": None,
    }), encoding="utf-8")
    return path


def _create_directory_link(target: Path, link: Path) -> None:
    try:
        os.symlink(target, link, target_is_directory=True)
    except OSError:
        if os.name != "nt":
            raise
        import _winapi
        _winapi.CreateJunction(str(target), str(link))


def test_status_counts_spool_states() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "spool"
        paths = ensure_layout(root)
        created_at = "2026-07-10T00:00:00+00:00"

        def extraction_job(job_id: str) -> dict:
            return {
                "schema_version": 2,
                "job_id": job_id,
                "kind": "extraction",
                "created_at": created_at,
                "session_id": "status-session",
                "attempts": 0,
                "last_error": None,
                "next_attempt_at": None,
            }

        (root / "legacy-flat.json").write_text(
            json.dumps({"transcript_path": "C:/legacy.jsonl"}),
            encoding="utf-8")
        (paths["incoming"] / "incoming.json").write_text(
            json.dumps(extraction_job("incoming")), encoding="utf-8")
        (paths["processing"] / "processing.json").write_text(
            json.dumps(extraction_job("processing")), encoding="utf-8")
        (paths["dead-letter"] / "genuine.reason.json").write_text(
            json.dumps(extraction_job("genuine-reason-name")), encoding="utf-8")
        dead_manifest = paths["dead-letter"] / "dead.json"
        dead_manifest.write_text(
            json.dumps(extraction_job("dead")), encoding="utf-8")
        dead_letter_reason_path(dead_manifest).write_text(json.dumps({
            "dead_lettered_at": created_at,
            "reason": "permanent failure",
            "manifest": dead_manifest.name,
        }), encoding="utf-8")

        assert _spool_counts(root) == {
            "incoming": 2,
            "processing": 1,
            "dead_letter": 2,
        }

        class StatusResult:
            def __init__(self, rows: list[tuple]) -> None:
                self.rows = rows

            def fetchall(self) -> list[tuple]:
                return self.rows

            def fetchone(self) -> tuple:
                return self.rows[0]

        class StatusConnection:
            def __enter__(self):
                return self

            def __exit__(self, *_args) -> None:
                return None

            def execute(self, sql: str) -> StatusResult:
                if "schema_migrations" in sql:
                    migration_count = len(list(
                        (Path(__file__).parents[1] / "migrations").glob("*.sql")))
                    return StatusResult([(str(i),) for i in range(migration_count)])
                if "GROUP BY status" in sql:
                    return StatusResult([("active", 1)])
                if "review_queue" in sql:
                    return StatusResult([(0,)])
                raise AssertionError(f"unexpected status query: {sql}")

        output = io.StringIO()
        with patch.dict(os.environ, {
                "MEMORYD_HOME": td,
                "MEMORYD_DSN": "postgresql://status:test@127.0.0.1/status",
             }), patch(
                "psycopg.connect", return_value=StatusConnection()), patch(
                "memoryd.cli._docker", return_value=(1, "")), patch(
                "memoryd.cli._health", return_value={"ok": True}), patch(
                "memoryd.cli._run", return_value=(1, "")), \
                contextlib.redirect_stdout(output):
            result = status()

        spool_line = (
            "  spool      incoming=2 processing=1 dead-letter=2"
            "  <- run `memoryd doctor`")
        assert spool_line in output.getvalue().splitlines()
        assert result == 1


def _archive_with_synchronized_publication(
        home: str, job_id: str, winner: bool,
        ready: multiprocessing.synchronize.Barrier) -> None:
    core.CFG.home = Path(home)
    core.CFG.ensure_dirs()
    real_link = core.os.link
    real_replace = core.os.replace

    def synchronized_publish(real_publish):
        def publish(source: Path, target: Path) -> None:
            ready.wait(timeout=10)
            if winner:
                core.time.sleep(0.05)
                real_publish(source, target)
            else:
                raise PermissionError("simulated Windows publication loser")
        return publish

    core.os.link = synchronized_publish(real_link)
    core.os.replace = synchronized_publish(real_replace)
    core.archive_bytes(b"concurrent", "text/plain", f"parallel/{job_id}.txt",
                       ingest_job_id=job_id)


def _claim_with_synchronized_move(
        root: str, ready: multiprocessing.synchronize.Barrier,
        results: multiprocessing.queues.Queue) -> None:
    real_replace = spool.os.replace

    def synchronized_replace(source: Path, target: Path) -> None:
        try:
            ready.wait(timeout=0.5)
        except threading.BrokenBarrierError:
            pass
        real_replace(source, target)

    spool.os.replace = synchronized_replace
    try:
        claimed = spool.claim_next(Path(root))
        results.put(None if claimed is None else claimed.name)
    except Exception as exc:  # noqa: BLE001 — parent asserts no claim error
        results.put(f"error:{type(exc).__name__}:{exc}")


def _assert_manifest_permission_contention_is_bounded(manifest: Path) -> None:
    real_open = core.os.open
    attempts = 0

    def transient_permission(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise PermissionError("transient lock contention")
        return real_open(*args, **kwargs)

    with patch.object(core.os, "open", side_effect=transient_permission):
        with core._manifest_file_lock(manifest):
            pass
    assert attempts == 2

    with patch.object(core.os, "open", side_effect=PermissionError("denied")), \
         patch.object(core.time, "monotonic", side_effect=(0, 6)):
        try:
            with core._manifest_file_lock(manifest):
                pass
        except TimeoutError:
            pass
        else:
            raise AssertionError("persistent manifest lock denial did not time out")


def _manifest_lock_contender(manifest: str,
                             acquired: multiprocessing.synchronize.Event) -> None:
    with core._manifest_file_lock(Path(manifest)):
        acquired.set()


def _assert_manifest_lock_ownership_is_safe(manifest: Path) -> None:
    context = multiprocessing.get_context("spawn")
    acquired = context.Event()
    contender = context.Process(
        target=_manifest_lock_contender,
        args=(str(manifest), acquired),
    )
    lock = manifest.with_suffix(".lock")
    with core._manifest_file_lock(manifest):
        os.utime(lock, (0, 0))
        contender.start()
        entered_while_owned = acquired.wait(timeout=0.5)

    acquired_after_release = acquired.wait(timeout=10)
    contender.join(timeout=10)
    if contender.is_alive():
        contender.terminate()
        contender.join()
    assert not entered_while_owned
    assert acquired_after_release
    assert contender.exitcode == 0
    assert lock.exists()


def _join_processes(processes: list[multiprocessing.Process]) -> None:
    timed_out = False
    for process in processes:
        process.join(timeout=15)
        timed_out = process.is_alive() or timed_out
    if timed_out:
        for process in processes:
            if process.is_alive():
                process.terminate()
        for process in processes:
            process.join()
        raise AssertionError("archive publication process timed out")


def _assert_corrupt_objects_are_rejected() -> None:
    manifest = core.CFG.archive / "manifest.jsonl"
    before = manifest.read_text().splitlines()
    cases = (
        (b"size-good", b"x", "corrupt/size.txt"),
        (b"hash-good", b"hash-baad", "corrupt/hash.txt"),
    )
    for data, corrupt, fonds_path in cases:
        sha = hashlib.sha256(data).hexdigest()
        obj_path = (core.CFG.archive / "objects" / "sha256" /
                    sha[:2] / sha[2:4] / sha)
        obj_path.parent.mkdir(parents=True, exist_ok=True)
        obj_path.write_bytes(corrupt)
        try:
            core.archive_bytes(data, "text/plain", fonds_path)
        except ValueError as error:
            assert "integrity" in str(error)
        else:
            raise AssertionError("corrupt canonical object accepted")
        assert obj_path.read_bytes() == corrupt
        assert manifest.read_text().splitlines() == before

    data = b"converged-good"
    sha = hashlib.sha256(data).hexdigest()
    obj_path = (core.CFG.archive / "objects" / "sha256" /
                sha[:2] / sha[2:4] / sha)

    def publish_corrupt_then_lose(source: Path, target: Path) -> None:
        Path(target).write_bytes(b"X" * len(data))
        raise PermissionError("simulated corrupt publication winner")

    with patch.object(core.os, "link", side_effect=publish_corrupt_then_lose), \
         patch.object(core.os, "replace", side_effect=publish_corrupt_then_lose):
        try:
            core.archive_bytes(data, "text/plain", "corrupt/converged.txt")
        except ValueError as error:
            assert "integrity" in str(error)
        else:
            raise AssertionError("corrupt converged object accepted")
    assert obj_path.read_bytes() == b"X" * len(data)
    assert manifest.read_text().splitlines() == before


def test_snapshot_survives_original_deletion() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "spool"
        transcript = Path(td) / "session.jsonl"
        transcript.write_bytes(b'{"type":"user"}\n')

        job = enqueue_capture(
            spool_root=root,
            transcript_path=transcript,
            session_id="session-1",
            project="memoryd",
            trigger="stop",
        )
        transcript.unlink()

        blob = validate_blob(root, job)
        assert blob.read_bytes() == b'{"type":"user"}\n'
        manifests = list((root / "incoming").glob("*.json"))
        assert len(manifests) == 1
        assert json.loads(manifests[0].read_text())["schema_version"] == 2

        blocker = r'''
import importlib.abc
import sys
from pathlib import Path

class BlockCoreAndPsycopg(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "memoryd.core" or fullname.startswith("psycopg"):
            raise ModuleNotFoundError(f"blocked dependency: {fullname}")
        return None

sys.meta_path.insert(0, BlockCoreAndPsycopg())
from memoryd.spool import enqueue_capture
base = Path(sys.argv[1])
base.mkdir(parents=True)
source = base / "stdlib.jsonl"
source.write_text("stdlib", encoding="utf-8")
enqueue_capture(spool_root=base / "spool", transcript_path=source,
                session_id="stdlib", project=None, trigger="stop")
'''
        isolated = subprocess.run(
            [sys.executable, "-c", blocker, str(Path(td) / "isolated")],
            capture_output=True, text=True, timeout=15,
            env=os.environ.copy(), check=False)
        assert isolated.returncode == 0, isolated.stderr


def test_identical_snapshots_share_one_blob() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "spool"
        transcript = Path(td) / "session.jsonl"
        transcript.write_text("same", encoding="utf-8")
        jobs = []

        def write() -> None:
            jobs.append(enqueue_capture(
                spool_root=root,
                transcript_path=transcript,
                session_id="session-1",
                project=None,
                trigger="stop",
            ))

        threads = [threading.Thread(target=write) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert len(jobs) == 4
        assert len({job["blob_sha256"] for job in jobs}) == 1
        assert len(list((root / "blobs").glob("[0-9a-f]" * 64))) == 1
        assert len(list((root / "incoming").glob("*.json"))) == 4


def test_snapshot_rejects_invalid_blob_collision_without_losing_bytes() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "spool"
        source = Path(td) / "session.jsonl"
        expected = b'durable collision evidence\n'
        source.write_bytes(expected)
        sha = hashlib.sha256(expected).hexdigest()
        paths = ensure_layout(root)
        incumbent = paths["blobs"] / sha

        for corrupt in (b"X" * len(expected), None):
            if os.path.lexists(incumbent):
                if incumbent.is_dir():
                    incumbent.rmdir()
                else:
                    incumbent.unlink()
            if corrupt is None:
                incumbent.mkdir()
            else:
                incumbent.write_bytes(corrupt)

            before_manifests = set(paths["incoming"].glob("*.json"))
            try:
                enqueue_capture(
                    spool_root=root, transcript_path=source,
                    session_id="collision", project=None, trigger="stop")
            except spool.PermanentSpoolError as exc:
                assert "collision" in str(exc)
            else:
                raise AssertionError("invalid blob collision was acknowledged")

            assert set(paths["incoming"].glob("*.json")) == before_manifests
            evidence = list(paths["blobs"].glob(f".collision.{sha}.*"))
            assert evidence
            assert evidence[-1].read_bytes() == expected

        incumbent.rmdir()
        outside = Path(td) / "outside-collision"
        outside.write_bytes(b"outside")
        try:
            os.symlink(outside, incumbent)
        except OSError:
            symlink_supported = False
        else:
            symlink_supported = True
            try:
                enqueue_capture(
                    spool_root=root, transcript_path=source,
                    session_id="redirected-collision", project=None,
                    trigger="stop")
            except spool.PermanentSpoolError:
                pass
            else:
                raise AssertionError("redirected blob collision was acknowledged")
            assert outside.read_bytes() == b"outside"
            incumbent.unlink()
        if not symlink_supported:
            assert not os.path.lexists(incumbent)

        incumbent.mkdir()
        with patch.object(spool.os, "replace", side_effect=OSError("denied")):
            try:
                enqueue_capture(
                    spool_root=root, transcript_path=source,
                    session_id="preserve-temp", project=None, trigger="stop")
            except spool.PermanentSpoolError:
                pass
            else:
                raise AssertionError("unpreserved collision was acknowledged")
        retained_temps = list(paths["blobs"].glob(".job_*.tmp"))
        assert retained_temps
        assert retained_temps[-1].read_bytes() == expected


def test_publication_swap_preserves_known_good_temp() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "spool"
        source = Path(td) / "session.jsonl"
        expected = b"known-good publication bytes"
        source.write_bytes(expected)
        sha = hashlib.sha256(expected).hexdigest()
        blob = root / "blobs" / sha
        real_read = spool._read_verified_file
        swapped = False

        def swap_before_final_validation(
                path: Path, expected_sha: str,
                expected_bytes: int | None) -> bytes:
            nonlocal swapped
            if Path(path) == blob and not swapped:
                swapped = True
                blob.unlink()
                blob.write_bytes(b"X" * len(expected))
            return real_read(path, expected_sha, expected_bytes)

        with patch.object(
                spool, "_read_verified_file",
                side_effect=swap_before_final_validation):
            try:
                enqueue_capture(
                    spool_root=root, transcript_path=source,
                    session_id="publication-swap", project=None,
                    trigger="stop")
            except spool.PermanentSpoolError:
                pass
            else:
                raise AssertionError("swapped publication was acknowledged")

        assert swapped
        assert not list((root / "incoming").glob("*.json"))
        evidence = [
            path for path in (root / "blobs").iterdir()
            if path.name.startswith(".collision.") or
            path.name.endswith(".tmp")]
        assert evidence
        assert any(path.read_bytes() == expected for path in evidence)

        redirected_root = Path(td) / "redirected-spool"
        redirected_paths = ensure_layout(redirected_root)
        redirected_paths["blobs"].rmdir()
        outside = Path(td) / "redirected-spool-outside"
        outside.mkdir()
        _create_directory_link(outside, redirected_paths["blobs"])
        outside_before = _tree_snapshot(outside)
        try:
            enqueue_capture(
                spool_root=redirected_root, transcript_path=source,
                session_id="redirected-spool", project=None, trigger="stop")
        except spool.PermanentSpoolError:
            pass
        else:
            raise AssertionError("redirected spool namespace accepted capture")
        assert _tree_snapshot(outside) == outside_before


def test_publication_fsyncs_blob_json_and_archive_namespaces() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "spool"
        source = Path(td) / "session.jsonl"
        source.write_text("fsync me", encoding="utf-8")
        spool_fsyncs: list[Path] = []
        with patch.object(
                spool, "_fsync_directory",
                side_effect=lambda path: spool_fsyncs.append(Path(path))):
            enqueue_capture(
                spool_root=root, transcript_path=source,
                session_id="fsync", project=None, trigger="stop")
            spool_fsyncs.clear()
            enqueue_capture(
                spool_root=root, transcript_path=source,
                session_id="fsync-winner", project=None, trigger="stop")
        assert root / "blobs" in spool_fsyncs
        assert root / "incoming" in spool_fsyncs

        old_home = core.CFG.home
        core.CFG.home = Path(td) / "memory"
        archive_fsyncs: list[Path] = []
        try:
            core.CFG.ensure_dirs()
            with patch.object(
                    core, "_fsync_directory",
                    side_effect=lambda path: archive_fsyncs.append(Path(path))):
                sha = core.archive_bytes(
                    b"archive fsync", "text/plain", "safe/fsync.txt")
                archive_fsyncs.clear()
                core.archive_bytes(
                    b"archive fsync", "text/plain", "safe/fsync-retry.txt")
            obj_dir = (core.CFG.archive / "objects" / "sha256" /
                       sha[:2] / sha[2:4])
            assert obj_dir in archive_fsyncs
            assert core.CFG.archive in archive_fsyncs
        finally:
            core.CFG.home = old_home

        unsupported = getattr(errno, "ENOTSUP", errno.EINVAL)
        with patch.object(
                spool.os, "open",
                side_effect=OSError(unsupported, "unsupported")):
            spool._fsync_directory(root)
        with patch.object(
                core.os, "open",
                side_effect=OSError(unsupported, "unsupported")):
            core._fsync_directory(Path(td) / "memory")


def test_first_use_namespaces_sync_parents_and_eio_propagates() -> None:
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        root = base / "nested" / "spool"
        spool_syncs: list[Path] = []
        with patch.object(
                spool, "_fsync_directory",
                side_effect=lambda path: spool_syncs.append(Path(path))):
            ensure_layout(root)
        assert base in spool_syncs
        assert root.parent in spool_syncs
        assert root in spool_syncs

        raced = base / "raced-directory"
        real_mkdir = Path.mkdir
        injected = False
        raced_syncs: list[Path] = []

        def concurrent_mkdir(path: Path, *args, **kwargs) -> None:
            nonlocal injected
            if Path(path) == raced and not injected:
                injected = True
                real_mkdir(path)
                raise FileExistsError(str(path))
            real_mkdir(path, *args, **kwargs)

        with patch.object(Path, "mkdir", concurrent_mkdir), \
             patch.object(
                 spool, "_fsync_directory",
                 side_effect=lambda path: raced_syncs.append(Path(path))):
            spool._mkdir_durable(raced)
        assert raced.parent in raced_syncs

        existing_spool = base / "existing-spool-directory"
        existing_spool.mkdir()
        observed_spool_syncs: list[Path] = []
        with patch.object(
                spool, "_fsync_directory",
                side_effect=lambda path: observed_spool_syncs.append(Path(path))):
            spool._mkdir_durable(existing_spool)
        assert existing_spool.parent in observed_spool_syncs

        old_home = core.CFG.home
        core.CFG.home = base / "fresh" / "memory"
        archive_syncs: list[Path] = []
        try:
            data = b"first archive namespace"
            sha = hashlib.sha256(data).hexdigest()
            with patch.object(
                    core, "_fsync_directory",
                    side_effect=lambda path: archive_syncs.append(Path(path))):
                core.archive_bytes(data, "text/plain", "first/capture.txt")
            archive = core.CFG.archive
            expected = {
                core.CFG.home.parent,
                core.CFG.home,
                archive,
                archive / "objects",
                archive / "objects" / "sha256",
                archive / "objects" / "sha256" / sha[:2],
                archive / "objects" / "sha256" / sha[:2] / sha[2:4],
            }
            assert expected.issubset(set(archive_syncs))

            observed_archive_syncs: list[Path] = []
            with patch.object(
                    core, "_fsync_directory",
                    side_effect=lambda path: observed_archive_syncs.append(
                        Path(path))):
                core._archive_object_namespace(
                    archive, sha, create=True)
            assert {
                archive.parent,
                archive,
                archive / "objects",
                archive / "objects" / "sha256",
                archive / "objects" / "sha256" / sha[:2],
            }.issubset(set(observed_archive_syncs))
        finally:
            core.CFG.home = old_home

        unsupported = getattr(errno, "ENOTSUP", errno.EINVAL)
        for module in (spool, core):
            with patch.object(
                    module.os, "open",
                    side_effect=OSError(unsupported, "unsupported")):
                module._fsync_directory(base)
            with patch.object(module.os, "open", return_value=123), \
                 patch.object(module.os, "fsync",
                              side_effect=OSError(errno.EIO, "I/O failure")), \
                 patch.object(module.os, "close"):
                try:
                    module._fsync_directory(base)
                except OSError as exc:
                    assert exc.errno == errno.EIO
                else:
                    raise AssertionError("directory fsync EIO was suppressed")


def test_hook_spools_bytes_when_daemon_is_down() -> None:
    with tempfile.TemporaryDirectory() as td:
        home = Path(td) / "memory"
        transcript = Path(td) / "session.jsonl"
        transcript.write_text("durable", encoding="utf-8")
        stdin = {"transcript_path": str(transcript), "session_id": "s1", "cwd": td}
        with patch("memoryd.hook._post", side_effect=OSError("down")):
            capture(stdin, "stop", 7437, home)
        transcript.unlink()
        jobs = list((home / "spool" / "incoming").glob("*.json"))
        assert len(jobs) == 1
        job = json.loads(jobs[0].read_text())
        assert validate_blob(home / "spool", job).read_text() == "durable"


def test_hook_warns_when_delivery_and_spooling_fail() -> None:
    with tempfile.TemporaryDirectory() as td:
        stderr = io.StringIO()
        stdin = {
            "transcript_path": str(Path(td) / "missing.jsonl"),
            "session_id": "s1", "cwd": ""}
        with patch("memoryd.hook._post", side_effect=OSError("down")), \
             contextlib.redirect_stderr(stderr):
            capture(stdin, "stop", 7437, Path(td) / "memory")
        assert "capture not durably saved" in stderr.getvalue()


def test_capture_persists_snapshot_before_acknowledgement() -> None:
    with tempfile.TemporaryDirectory() as td:
        old_home = core.CFG.home
        httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        thread = threading.Thread(target=httpd.serve_forever)
        try:
            core.CFG.home = Path(td) / "memory"
            transcript = Path(td) / "session.jsonl"
            transcript.write_text("durable before ack", encoding="utf-8")
            request = urllib.request.Request(
                f"http://127.0.0.1:{httpd.server_port}/capture",
                data=json.dumps({
                    "transcript_path": str(transcript),
                    "session_id": "http-durable",
                    "project": None,
                    "trigger": "stop",
                }).encode(),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            thread.start()
            with urllib.request.urlopen(request, timeout=5) as response:
                assert response.status == 202
                response_body = response.read()
            transcript.unlink()

            assert response_body == b'{"queued": true}'
            assert json.loads(response_body) == {"queued": True}
            manifests = list((core.CFG.spool / "incoming").glob("*.json"))
            assert len(manifests) == 1
            job = json.loads(manifests[0].read_text(encoding="utf-8"))
            assert validate_blob(core.CFG.spool, job).read_text() == "durable before ack"

            extract_request = urllib.request.Request(
                f"http://127.0.0.1:{httpd.server_port}/extract",
                data=json.dumps({"session_id": "extract-durable"}).encode(),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(extract_request, timeout=5) as response:
                assert response.status == 202
                extract_response = response.read()
            assert extract_response == b'{"queued": true}'
            jobs = [json.loads(path.read_text(encoding="utf-8"))
                    for path in (core.CFG.spool / "incoming").glob("*.json")]
            extraction_jobs = [value for value in jobs
                               if value.get("kind") == "extraction"]
            assert len(extraction_jobs) == 1
            assert extraction_jobs[0]["schema_version"] == 2
            assert extraction_jobs[0]["session_id"] == "extract-durable"

            validation_source = Path(td) / "validation.jsonl"
            validation_source.write_text("validation", encoding="utf-8")
            before_invalid = {
                path.name for path in
                (core.CFG.spool / "incoming").glob("*.json")}

            def invalid_post(path: str, payload: object) -> int:
                invalid_request = urllib.request.Request(
                    f"http://127.0.0.1:{httpd.server_port}{path}",
                    data=json.dumps(payload).encode(),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                try:
                    urllib.request.urlopen(invalid_request, timeout=5)
                except urllib.error.HTTPError as exc:
                    exc.read()
                    return exc.code
                raise AssertionError(f"invalid request accepted: {path} {payload!r}")

            invalid_values = (None, ["bad"], {"bad": 1}, "", "   ", 7)
            for field in ("transcript_path", "session_id", "trigger"):
                for invalid_value in invalid_values:
                    body = {
                        "transcript_path": str(validation_source),
                        "session_id": "valid-session",
                        "trigger": "stop",
                    }
                    body[field] = invalid_value
                    assert invalid_post("/capture", body) == 400
            for invalid_value in (["bad"], {"bad": 1}, 7):
                assert invalid_post("/capture", {
                    "transcript_path": str(validation_source),
                    "session_id": "valid-session",
                    "trigger": "stop",
                    "project": invalid_value,
                }) == 400
            assert invalid_post("/capture", []) == 400

            for invalid_value in invalid_values:
                assert invalid_post(
                    "/extract", {"session_id": invalid_value}) == 400
            assert invalid_post("/extract", []) == 400
            after_invalid = {
                path.name for path in
                (core.CFG.spool / "incoming").glob("*.json")}
            assert after_invalid == before_invalid
        finally:
            httpd.shutdown()
            httpd.server_close()
            if thread.is_alive():
                thread.join(timeout=5)
            while True:
                try:
                    server.CAPTURE_Q.get_nowait()
                except server.queue.Empty:
                    break
                else:
                    server.CAPTURE_Q.task_done()
            core.CFG.home = old_home


def test_claim_retry_and_dead_letter_preserve_manifest() -> None:
    with tempfile.TemporaryDirectory() as td:
        gc_root = Path(td) / "gc-spool"
        gc_source = Path(td) / "gc.jsonl"
        gc_source.write_text("x", encoding="utf-8")
        gc_job = enqueue_capture(
            spool_root=gc_root, transcript_path=gc_source,
            session_id="gc", project=None, trigger="stop")
        next((gc_root / "incoming").glob("*.json")).unlink()
        canonical = Path(td) / "canonical"
        canonical.write_text("wrong", encoding="utf-8")
        assert not spool.gc_blob_if_unreferenced(
            gc_root, gc_job["blob_sha256"], canonical)
        assert (gc_root / "blobs" / gc_job["blob_sha256"]).exists()
        canonical.write_text("x", encoding="utf-8")
        assert spool.gc_blob_if_unreferenced(
            gc_root, gc_job["blob_sha256"], canonical)

        uncertain_root = Path(td) / "uncertain-spool"
        uncertain_job = enqueue_capture(
            spool_root=uncertain_root, transcript_path=gc_source,
            session_id="uncertain", project=None, trigger="stop")
        next((uncertain_root / "incoming").glob("*.json")).unlink()
        malformed = uncertain_root / "incoming" / "malformed.json"
        malformed.write_text("{broken", encoding="utf-8")
        assert not spool.gc_blob_if_unreferenced(
            uncertain_root, uncertain_job["blob_sha256"], canonical)
        assert (uncertain_root / "blobs" / uncertain_job["blob_sha256"]).exists()

        malformed.unlink()
        unreadable = uncertain_root / "incoming" / "unreadable.json"
        unreadable.write_text("{}", encoding="utf-8")
        real_read_text = Path.read_text

        def unreadable_manifest(path: Path, *args, **kwargs) -> str:
            if Path(path) == unreadable:
                raise OSError("manifest unreadable")
            return real_read_text(path, *args, **kwargs)

        with patch.object(Path, "read_text", unreadable_manifest):
            assert not spool.gc_blob_if_unreferenced(
                uncertain_root, uncertain_job["blob_sha256"], canonical)
        assert (uncertain_root / "blobs" / uncertain_job["blob_sha256"]).exists()

        reason_named_root = Path(td) / "reason-named-spool"
        reason_named_job = enqueue_capture(
            spool_root=reason_named_root, transcript_path=gc_source,
            session_id="reason-named", project=None, trigger="stop")
        next((reason_named_root / "incoming").glob("*.json")).unlink()
        dead = reason_named_root / "dead-letter"
        reason_named_manifest = dead / "job.reason.json"
        reason_named_manifest.write_text(
            json.dumps(reason_named_job), encoding="utf-8")
        reason_named_sidecar = dead / "job.reason.reason.json"
        reason_named_sidecar.write_text(json.dumps({
            "dead_lettered_at": datetime.now(timezone.utc).isoformat(),
            "reason": "genuine reason-named manifest",
            "manifest": reason_named_manifest.name,
        }), encoding="utf-8")
        assert not spool.gc_blob_if_unreferenced(
            reason_named_root, reason_named_job["blob_sha256"], canonical)
        assert (reason_named_root / "blobs" /
                reason_named_job["blob_sha256"]).exists()
        assert spool.is_dead_letter_sidecar(reason_named_sidecar)
        assert not spool.is_dead_letter_sidecar(reason_named_manifest)

        ambiguous_root = Path(td) / "ambiguous-sidecar-spool"
        ambiguous_job = enqueue_capture(
            spool_root=ambiguous_root, transcript_path=gc_source,
            session_id="ambiguous-evidence", project=None, trigger="stop")
        next((ambiguous_root / "incoming").glob("*.json")).unlink()
        ambiguous_dead = ambiguous_root / "dead-letter"
        named_manifest = ambiguous_dead / "job.json"
        named_manifest.write_text(json.dumps({
            "schema_version": 2,
            "job_id": "named-job",
            "kind": "extraction",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "session_id": "named-session",
            "attempts": 0,
            "last_error": None,
            "next_attempt_at": None,
        }), encoding="utf-8")
        ambiguous_manifest = ambiguous_dead / "job.reason.json"
        ambiguous_manifest.write_text(json.dumps({
            **ambiguous_job,
            "dead_lettered_at": datetime.now(timezone.utc).isoformat(),
            "reason": "capture evidence, not a sidecar",
            "manifest": named_manifest.name,
        }), encoding="utf-8")
        ordinary_manifest = ambiguous_dead / "ordinary.json"
        ordinary_manifest.write_text(json.dumps({
            "schema_version": 2,
            "job_id": "ordinary-job",
            "kind": "extraction",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "session_id": "ordinary-session",
            "attempts": 0,
            "last_error": None,
            "next_attempt_at": None,
        }), encoding="utf-8")
        ordinary_sidecar = ambiguous_dead / "ordinary.reason.json"
        ordinary_sidecar.write_text(json.dumps({
            "dead_lettered_at": datetime.now(timezone.utc).isoformat(),
            "reason": "ordinary exact sidecar",
            "manifest": ordinary_manifest.name,
        }), encoding="utf-8")
        assert spool.is_dead_letter_sidecar(ordinary_sidecar)
        assert not spool.is_dead_letter_sidecar(ambiguous_manifest)
        assert not spool.gc_blob_if_unreferenced(
            ambiguous_root, ambiguous_job["blob_sha256"], canonical)
        assert (ambiguous_root / "blobs" /
                ambiguous_job["blob_sha256"]).exists()

        gc_race_root = Path(td) / "gc-race-spool"
        gc_race_source = Path(td) / "gc-race.jsonl"
        gc_race_source.write_text("x", encoding="utf-8")
        gc_race_job = enqueue_capture(
            spool_root=gc_race_root, transcript_path=gc_race_source,
            session_id="gc-race-old", project=None, trigger="stop")
        next((gc_race_root / "incoming").glob("*.json")).unlink()
        gc_race_blob = gc_race_root / "blobs" / gc_race_job["blob_sha256"]
        gc_at_delete = threading.Event()
        allow_gc = threading.Event()
        manifest_written = threading.Event()
        real_unlink = Path.unlink
        real_atomic_json = spool._atomic_json

        def delayed_gc_unlink(path: Path, *args, **kwargs) -> None:
            if (Path(path) == gc_race_blob and
                    threading.current_thread().name == "gc-race"):
                gc_at_delete.set()
                assert allow_gc.wait(timeout=5)
            real_unlink(path, *args, **kwargs)

        def observed_atomic_json(path: Path, value: dict) -> None:
            real_atomic_json(path, value)
            if path.parent.name == "incoming":
                manifest_written.set()

        gc_result: list[bool] = []
        enqueue_errors: list[Exception] = []

        def collect_gc() -> None:
            gc_result.append(spool.gc_blob_if_unreferenced(
                gc_race_root, gc_race_job["blob_sha256"], canonical))

        def enqueue_during_gc() -> None:
            try:
                enqueue_capture(
                    spool_root=gc_race_root, transcript_path=gc_race_source,
                    session_id="gc-race-new", project=None, trigger="stop")
            except Exception as exc:  # noqa: BLE001 — assertion records race
                enqueue_errors.append(exc)

        with patch.object(Path, "unlink", delayed_gc_unlink), \
             patch.object(spool, "_atomic_json", side_effect=observed_atomic_json):
            gc_thread = threading.Thread(target=collect_gc, name="gc-race")
            gc_thread.start()
            assert gc_at_delete.wait(timeout=5)
            enqueue_thread = threading.Thread(target=enqueue_during_gc)
            enqueue_thread.start()
            manifest_written.wait(timeout=0.5)
            allow_gc.set()
            gc_thread.join(timeout=5)
            enqueue_thread.join(timeout=5)
        assert gc_result == [True]
        assert not enqueue_errors
        gc_race_manifest = next((gc_race_root / "incoming").glob("*.json"))
        validate_blob(
            gc_race_root,
            json.loads(gc_race_manifest.read_text(encoding="utf-8")))

        release_race_root = Path(td) / "release-race-spool"
        release_race_job = enqueue_capture(
            spool_root=release_race_root, transcript_path=gc_race_source,
            session_id="release-race", project=None, trigger="stop")
        release_claim = spool.claim_next(release_race_root)
        assert release_claim
        incoming_scanned = threading.Event()
        allow_processing_scan = threading.Event()
        real_glob = Path.glob

        def delayed_incoming_glob(path: Path, pattern: str):
            values = list(real_glob(path, pattern))
            if (Path(path) == release_race_root / "incoming" and
                    threading.current_thread().name == "release-race-gc"):
                incoming_scanned.set()
                assert allow_processing_scan.wait(timeout=5)
            return iter(values)

        release_gc_result: list[bool] = []
        release_errors: list[Exception] = []

        def collect_release_gc() -> None:
            release_gc_result.append(spool.gc_blob_if_unreferenced(
                release_race_root, release_race_job["blob_sha256"], canonical))

        def release_during_gc() -> None:
            try:
                spool.release_job(
                    release_race_root, release_claim, "retry", delay_s=1)
            except Exception as exc:  # noqa: BLE001 — assertion records race
                release_errors.append(exc)

        with patch.object(Path, "glob", delayed_incoming_glob):
            release_gc_thread = threading.Thread(
                target=collect_release_gc, name="release-race-gc")
            release_gc_thread.start()
            assert incoming_scanned.wait(timeout=5)
            release_thread = threading.Thread(target=release_during_gc)
            release_thread.start()
            release_thread.join(timeout=0.2)
            allow_processing_scan.set()
            release_gc_thread.join(timeout=5)
            release_thread.join(timeout=5)
        assert release_gc_result == [False]
        assert not release_errors
        release_manifest = next(
            (release_race_root / "incoming").glob("*.json"))
        validate_blob(
            release_race_root,
            json.loads(release_manifest.read_text(encoding="utf-8")))

        claim_root = Path(td) / "claim-spool"
        claim_source = Path(td) / "claim.jsonl"
        claim_source.write_text("x", encoding="utf-8")
        enqueue_capture(
            spool_root=claim_root, transcript_path=claim_source,
            session_id="claim", project=None, trigger="stop")
        real_replace = spool.os.replace
        active = 0
        max_active = 0
        active_lock = threading.Lock()
        both_entered = threading.Event()

        def observed_replace(source: Path, target: Path) -> None:
            nonlocal active, max_active
            with active_lock:
                active += 1
                max_active = max(max_active, active)
                if active == 2:
                    both_entered.set()
            both_entered.wait(timeout=0.2)
            try:
                real_replace(source, target)
            finally:
                with active_lock:
                    active -= 1

        claims: list[Path | None] = []
        errors: list[Exception] = []
        start = threading.Barrier(2)

        def claim() -> None:
            try:
                start.wait(timeout=5)
                claims.append(spool.claim_next(claim_root))
            except Exception as exc:  # noqa: BLE001 — assertion records loser
                errors.append(exc)

        with patch.object(spool.os, "replace", side_effect=observed_replace):
            threads = [threading.Thread(target=claim) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=5)
        assert not errors
        assert sum(path is not None for path in claims) == 1
        assert max_active == 1

        process_root = Path(td) / "process-claim-spool"
        enqueue_capture(
            spool_root=process_root, transcript_path=claim_source,
            session_id="process-claim", project=None, trigger="stop")
        context = multiprocessing.get_context("spawn")
        ready = context.Barrier(2)
        results = context.Queue()
        processes = [context.Process(
            target=_claim_with_synchronized_move,
            args=(str(process_root), ready, results),
        ) for _ in range(2)]
        for process in processes:
            process.start()
        _join_processes(processes)
        process_claims = [results.get(timeout=5) for _ in processes]
        assert not any(str(value).startswith("error:")
                       for value in process_claims), process_claims
        assert sum(value is not None for value in process_claims) == 1, process_claims

        root = Path(td) / "spool"
        transcript = Path(td) / "session.jsonl"
        transcript.write_text("x", encoding="utf-8")
        enqueue_capture(spool_root=root, transcript_path=transcript,
                        session_id="s", project=None, trigger="stop")
        claimed = spool.claim_next(root)
        assert claimed and claimed.parent.name == "processing"
        released = spool.release_job(root, claimed, "database down", delay_s=1)
        value = json.loads(released.read_text())
        assert value["attempts"] == 1
        assert value["last_error"] == "database down"
        claimed = spool.claim_next(root, ignore_schedule=True)
        assert claimed
        preserved = spool.dead_letter(root, claimed, "checksum mismatch")
        assert preserved.exists()
        reason = preserved.with_suffix(".reason.json")
        assert json.loads(reason.read_text())["reason"] == "checksum mismatch"

        sidecar_root = Path(td) / "sidecar-spool"
        sidecar_paths = ensure_layout(sidecar_root)
        sidecar_source = sidecar_paths["incoming"] / "job.json"
        sidecar_source.write_text('{"evidence": true}', encoding="utf-8")
        orphan_reason = sidecar_paths["dead-letter"] / "job.reason.json"
        orphan_reason.write_text('{"orphan": true}', encoding="utf-8")
        preserved = spool.dead_letter(sidecar_root, sidecar_source, "collision")
        assert preserved.name != "job.json"
        assert orphan_reason.read_text(encoding="utf-8") == '{"orphan": true}'
        assert json.loads(preserved.with_suffix(".reason.json").read_text())[
            "reason"] == "collision"

        ordered_source = sidecar_paths["incoming"] / "ordered.json"
        ordered_source.write_text('{"ordered": true}', encoding="utf-8")
        real_replace = spool.os.replace

        def require_reason_before_manifest(source: Path, target: Path) -> None:
            if Path(source) == ordered_source:
                assert Path(target).with_suffix(".reason.json").exists()
            real_replace(source, target)

        with patch.object(spool.os, "replace",
                          side_effect=require_reason_before_manifest):
            ordered = spool.dead_letter(
                sidecar_root, ordered_source, "sidecar first")
        assert ordered.exists()

        recoverable = sidecar_paths["incoming"] / "recoverable.json"
        recoverable.write_text('{"recoverable": true}', encoding="utf-8")
        with patch.object(spool, "_atomic_json",
                          side_effect=OSError("sidecar write failed")):
            try:
                spool.dead_letter(sidecar_root, recoverable, "temporary failure")
            except OSError as exc:
                assert str(exc) == "sidecar write failed"
            else:
                raise AssertionError("sidecar failure was swallowed")
        assert recoverable.exists()
        assert not (sidecar_paths["dead-letter"] / "recoverable.json").exists()


def test_spool_state_transitions_sync_directories_and_leases() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "spool"
        paths = ensure_layout(root)
        _write_extraction_job(root, "state.json", "state-session")
        events: list[tuple[str, Path]] = []

        def sync_directory(path: Path) -> None:
            events.append(("dir", Path(path)))

        def sync_file(path: Path) -> None:
            events.append(("file", Path(path)))

        patches = (
            patch.object(spool, "ensure_layout", return_value=paths),
            patch.object(spool, "_fsync_directory", side_effect=sync_directory),
            patch.object(
                spool, "_fsync_file", side_effect=sync_file, create=True),
        )
        with patches[0], patches[1], patches[2]:
            claimed = spool.claim_next(root)
        assert claimed is not None
        assert events == [
            ("dir", paths["processing"]),
            ("dir", paths["incoming"]),
            ("file", claimed),
        ]

        events.clear()
        with patch.object(spool, "ensure_layout", return_value=paths), \
             patch.object(spool, "_fsync_directory", side_effect=sync_directory):
            released = spool.release_job(root, claimed, "retry", delay_s=1)
        assert events == [
            ("dir", paths["processing"]),
            ("dir", paths["incoming"]),
            ("dir", paths["processing"]),
        ]

        claimed = spool.claim_next(root, ignore_schedule=True)
        assert claimed is not None
        events.clear()
        with patch.object(spool, "ensure_layout", return_value=paths), \
             patch.object(spool, "_fsync_directory", side_effect=sync_directory):
            dead = spool.dead_letter(root, claimed, "permanent")
        assert events == [
            ("dir", paths["dead-letter"]),
            ("dir", paths["dead-letter"]),
            ("dir", paths["processing"]),
        ]
        assert dead.exists()

        _write_extraction_job(root, "complete.json", "complete-session")
        claimed = spool.claim_next(root)
        assert claimed is not None
        events.clear()
        with patch.object(spool, "ensure_layout", return_value=paths), \
             patch.object(spool, "_fsync_directory", side_effect=sync_directory):
            spool.complete_job(claimed)
        assert events == [("dir", paths["processing"])]
        assert not claimed.exists()

        stale = _write_extraction_job(root, "stale.json", "stale-session")
        stale = spool.claim_next(root)
        assert stale is not None
        old = (datetime.now(timezone.utc) - timedelta(minutes=20)).timestamp()
        os.utime(stale, (old, old))
        events.clear()
        with patch.object(spool, "ensure_layout", return_value=paths), \
             patch.object(spool, "_fsync_directory", side_effect=sync_directory):
            assert spool.requeue_stale(root, stale_after_s=900) == 1
        assert events == [
            ("dir", paths["incoming"]),
            ("dir", paths["processing"]),
        ]


def _assert_nonobject_manifest_does_not_block_drain() -> None:
    with tempfile.TemporaryDirectory() as td:
        old_home = core.CFG.home
        core.CFG.home = Path(td) / "memory"
        try:
            core.CFG.ensure_dirs()
            invalid = ensure_layout(core.CFG.spool)["incoming"] / "a-invalid.json"
            invalid.write_text("[]", encoding="utf-8")
            _write_extraction_job(
                core.CFG.spool, "b-extraction.json", "later-session")
            with patch("memoryd.extract.run_extraction",
                       return_value={"ok": True, "stored": 0}):
                stats = ingest.drain_spool()
            assert stats == {
                "processed": 1, "retried": 0,
                "dead_lettered": 1, "requeued": 0,
            }
            preserved = core.CFG.spool / "dead-letter" / "a-invalid.json"
            assert json.loads(preserved.read_text(encoding="utf-8")) == []
            assert preserved.with_suffix(".reason.json").exists()
        finally:
            core.CFG.home = old_home


def _assert_extraction_replay_classification() -> None:
    with tempfile.TemporaryDirectory() as td:
        old_home = core.CFG.home
        try:
            core.CFG.home = Path(td) / "no-events"
            core.CFG.ensure_dirs()
            _write_extraction_job(
                core.CFG.spool, "no-events.json", "empty-session")
            with patch("memoryd.extract.run_extraction", return_value={
                    "ok": False, "error": "no events for session"}):
                stats = ingest.drain_spool()
            assert stats == {
                "processed": 1, "retried": 0,
                "dead_lettered": 0, "requeued": 0,
            }
            assert not list((core.CFG.spool / "incoming").glob("*.json"))
            assert not list((core.CFG.spool / "dead-letter").glob("*.json"))

            core.CFG.home = Path(td) / "retryable"
            core.CFG.ensure_dirs()
            _write_extraction_job(
                core.CFG.spool, "retryable.json", "retry-session")
            with patch("memoryd.extract.run_extraction", return_value={
                    "ok": False, "error": "database down"}):
                stats = ingest.drain_spool()
            assert stats == {
                "processed": 0, "retried": 1,
                "dead_lettered": 0, "requeued": 0,
            }
            retry = next((core.CFG.spool / "incoming").glob("*.json"))
            assert json.loads(retry.read_text())["attempts"] == 1

            core.CFG.home = Path(td) / "capture-no-events"
            core.CFG.ensure_dirs()
            transcript = Path(td) / "capture.jsonl"
            transcript.write_text("x", encoding="utf-8")
            job = enqueue_capture(
                spool_root=core.CFG.spool, transcript_path=transcript,
                session_id="capture-empty", project=None,
                trigger="session_end")
            sha = job["blob_sha256"]
            canonical = (core.CFG.archive / "objects" / "sha256" /
                         sha[:2] / sha[2:4] / sha)
            canonical.parent.mkdir(parents=True, exist_ok=True)
            canonical.write_text("x", encoding="utf-8")
            with patch.object(ingest, "ingest_transcript", return_value={
                    "ok": True, "sha256": sha, "new_events": 0}), \
                 patch("memoryd.extract.run_extraction", return_value={
                     "ok": False, "error": "no events for session"}):
                stats = ingest.drain_spool()
            assert stats == {
                "processed": 1, "retried": 0,
                "dead_lettered": 0, "requeued": 0,
            }
            assert not list((core.CFG.spool / "incoming").glob("*.json"))
        finally:
            core.CFG.home = old_home


def _assert_strict_manifest_types_are_preserved() -> None:
    with tempfile.TemporaryDirectory() as td:
        old_home = core.CFG.home
        core.CFG.home = Path(td) / "strict-manifests"
        try:
            core.CFG.ensure_dirs()
            incoming = ensure_layout(core.CFG.spool)["incoming"]
            common = {
                "schema_version": 2,
                "job_id": "job-valid",
                "kind": "capture_snapshot",
                "created_at": datetime.now(timezone.utc).isoformat(),
                "session_id": "session-valid",
                "attempts": 0,
                "last_error": None,
                "next_attempt_at": None,
            }
            capture = {
                **common,
                "project": None,
                "trigger": "stop",
                "original_transcript_path": "C:/valid/session.jsonl",
                "blob_sha256": "0" * 64,
                "blob_bytes": 1,
            }
            extraction = {**common, "kind": "extraction"}
            invalid_values = (None, [], {}, "")
            cases: list[tuple[str, str, dict]] = []
            for field in (
                    "job_id", "kind", "session_id", "trigger",
                    "original_transcript_path", "blob_sha256", "blob_bytes"):
                for index, invalid_value in enumerate(invalid_values):
                    cases.append((
                        f"capture-{field}-{index}", field,
                        {**capture, field: invalid_value}))
            for field in ("job_id", "kind", "session_id"):
                for index, invalid_value in enumerate(invalid_values):
                    cases.append((
                        f"extraction-{field}-{index}", field,
                        {**extraction, field: invalid_value}))
            cases.extend((
                ("schema-bool", "schema_version",
                 {**capture, "schema_version": True}),
                ("schema-float", "schema_version",
                 {**capture, "schema_version": 2.0}),
                ("bytes-bool", "blob_bytes",
                 {**capture, "blob_bytes": True}),
                ("bytes-negative", "blob_bytes",
                 {**capture, "blob_bytes": -1}),
                ("attempts-bool", "attempts",
                 {**capture, "attempts": True}),
                ("project-list", "project",
                 {**capture, "project": ["bad"]}),
                ("next-attempt-dict", "next_attempt_at",
                 {**extraction, "next_attempt_at": {"bad": 1}}),
            ))
            originals: dict[str, dict] = {}
            for ordinal, (label, _field, value) in enumerate(cases):
                name = f"invalid-{ordinal:03d}-{label}.json"
                originals[name] = value
                (incoming / name).write_text(
                    json.dumps(value), encoding="utf-8")
            _write_extraction_job(
                core.CFG.spool, "zz-valid-extraction.json", "later-valid")

            with patch("memoryd.extract.run_extraction",
                       return_value={"ok": True, "stored": 0}):
                stats = ingest.drain_spool()
            assert stats == {
                "processed": 1, "retried": 0,
                "dead_lettered": len(cases), "requeued": 0,
            }
            dead = core.CFG.spool / "dead-letter"
            for name, original in originals.items():
                preserved = dead / name
                assert json.loads(preserved.read_text(encoding="utf-8")) == original
                reason = json.loads(
                    preserved.with_suffix(".reason.json").read_text(
                        encoding="utf-8"))["reason"]
                expected_field = next(
                    field for label, field, _value in cases
                    if name.endswith(f"{label}.json"))
                assert expected_field in reason, (name, reason)
        finally:
            core.CFG.home = old_home


def _assert_legacy_path_types_are_preserved() -> None:
    with tempfile.TemporaryDirectory() as td:
        old_home = core.CFG.home
        core.CFG.home = Path(td) / "legacy-types"
        try:
            core.CFG.ensure_dirs()
            invalid_paths = (None, ["bad"], {"bad": 1}, "")
            originals = {}
            for index, invalid_path in enumerate(invalid_paths):
                name = f"legacy-invalid-{index}.json"
                value = {
                    "transcript_path": invalid_path,
                    "session_id": "legacy",
                    "trigger": "stop",
                }
                originals[name] = value
                (core.CFG.spool / name).write_text(
                    json.dumps(value), encoding="utf-8")
            _write_extraction_job(
                core.CFG.spool, "zz-valid-extraction.json", "later-valid")
            with patch("memoryd.extract.run_extraction",
                       return_value={"ok": True, "stored": 0}):
                stats = ingest.drain_spool()
            assert stats == {
                "processed": 1, "retried": 0,
                "dead_lettered": len(invalid_paths), "requeued": 0,
            }
            dead = core.CFG.spool / "dead-letter"
            for name, original in originals.items():
                preserved = dead / name
                assert json.loads(preserved.read_text(encoding="utf-8")) == original
                reason = json.loads(
                    preserved.with_suffix(".reason.json").read_text())[
                        "reason"]
                assert "transcript_path" in reason
        finally:
            core.CFG.home = old_home


def _assert_mixed_role_dispatch_is_unambiguous() -> None:
    with tempfile.TemporaryDirectory() as td:
        old_home = core.CFG.home
        core.CFG.home = Path(td) / "mixed-role"
        try:
            core.CFG.ensure_dirs()
            incoming = ensure_layout(core.CFG.spool)["incoming"]
            common = {
                "schema_version": 2,
                "job_id": "valid-job",
                "kind": "capture_snapshot",
                "created_at": datetime.now(timezone.utc).isoformat(),
                "session_id": "must-not-extract",
                "attempts": 0,
                "last_error": None,
                "next_attempt_at": None,
            }
            capture = {
                **common,
                "project": None,
                "trigger": "stop",
                "original_transcript_path": "C:/valid/session.jsonl",
                "blob_sha256": "0" * 64,
                "blob_bytes": 1,
            }
            extraction = {**common, "kind": "extraction"}
            schema_cases = (
                {**capture, "blob_bytes": True, "extract_only": True},
                {**extraction, "job_id": [], "extract_only": True},
                {**capture, "trigger": [], "extract_only": "yes"},
                {**extraction, "job_id": {}, "extract_only": ["yes"]},
                {**capture, "blob_sha256": {}, "extract_only": {"yes": 1}},
                {**extraction, "session_id": "must-not-extract",
                 "extract_only": 1, "attempts": True},
            )
            legacy_markers = ("yes", 1, [True], {"yes": True})
            originals: dict[str, dict] = {}
            for index, value in enumerate(schema_cases):
                name = f"schema-mixed-{index}.json"
                originals[name] = value
                (incoming / name).write_text(json.dumps(value), encoding="utf-8")
            for index, marker in enumerate(legacy_markers):
                name = f"legacy-marker-{index}.json"
                value = {
                    "extract_only": marker,
                    "session_id": "must-not-extract",
                    "attempts": 0,
                }
                originals[name] = value
                (incoming / name).write_text(json.dumps(value), encoding="utf-8")
            legitimate_legacy = incoming / "zz-legacy-exact.json"
            legitimate_legacy.write_text(json.dumps({
                "extract_only": True,
                "session_id": "legacy-valid",
                "attempts": 0,
            }), encoding="utf-8")
            _write_extraction_job(
                core.CFG.spool, "zzz-schema-valid.json", "schema-valid")

            extracted_sessions: list[str] = []

            def extracted(session_id: str) -> dict:
                extracted_sessions.append(session_id)
                return {"ok": True, "stored": 0}

            with patch("memoryd.extract.run_extraction", side_effect=extracted):
                stats = ingest.drain_spool()
            assert stats == {
                "processed": 2, "retried": 0,
                "dead_lettered": len(originals), "requeued": 0,
            }
            assert extracted_sessions == ["legacy-valid", "schema-valid"]
            dead = core.CFG.spool / "dead-letter"
            for name, original in originals.items():
                preserved = dead / name
                assert json.loads(preserved.read_text(encoding="utf-8")) == original
                assert preserved.with_suffix(".reason.json").exists()
        finally:
            core.CFG.home = old_home


def test_legacy_missing_source_is_preserved() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "spool"
        paths = ensure_layout(root)
        legacy = root / "cap-old.json"
        original = {"transcript_path": str(Path(td) / "gone.jsonl"),
                    "session_id": "old", "trigger": "stop"}
        legacy.write_text(json.dumps(original), encoding="utf-8")
        result = spool.upgrade_legacy_job(root, legacy)
        assert result is None
        preserved = paths["dead-letter"] / legacy.name
        assert json.loads(preserved.read_text()) == original
        assert preserved.with_suffix(".reason.json").exists()
    _assert_nonobject_manifest_does_not_block_drain()
    _assert_extraction_replay_classification()
    _assert_strict_manifest_types_are_preserved()
    _assert_legacy_path_types_are_preserved()
    _assert_mixed_role_dispatch_is_unambiguous()


def test_stale_processing_job_is_requeued() -> None:
    with tempfile.TemporaryDirectory() as td:
        lease_root = Path(td) / "lease-spool"
        lease_transcript = Path(td) / "lease.jsonl"
        lease_transcript.write_text("x", encoding="utf-8")
        enqueue_capture(
            spool_root=lease_root, transcript_path=lease_transcript,
            session_id="lease", project=None, trigger="stop")
        incoming = next((lease_root / "incoming").glob("*.json"))
        old = (datetime.now(timezone.utc) - timedelta(minutes=20)).timestamp()
        os.utime(incoming, (old, old))
        leased = spool.claim_next(lease_root)
        assert leased
        assert leased.stat().st_mtime > old
        assert spool.requeue_stale(lease_root, stale_after_s=900) == 0
        assert leased.exists()

        root = Path(td) / "spool"
        transcript = Path(td) / "session.jsonl"
        transcript.write_text("x", encoding="utf-8")
        enqueue_capture(spool_root=root, transcript_path=transcript,
                        session_id="s", project=None, trigger="stop")
        claimed = spool.claim_next(root)
        assert claimed
        old = (datetime.now(timezone.utc) - timedelta(minutes=20)).timestamp()
        os.utime(claimed, (old, old))
        assert spool.requeue_stale(root, stale_after_s=900) == 1
        assert list((root / "incoming").glob("*.json"))


def test_mixed_transcript_line_preserves_text_and_tools() -> None:
    assistant = {"type": "assistant", "message": {"content": [
        {"type": "text", "text": "final answer"},
        {"type": "tool_use", "name": "shell", "input": {"command": "pwd"}},
    ]}}
    user = {"type": "user", "message": {"content": [
        {"type": "text", "text": "remember this"},
        {"type": "tool_result", "content": "ok"},
    ]}}
    assert [kind for kind, _ in _classify_all(assistant)] == [
        "agent_response", "tool_call"]
    assert [kind for kind, _ in _classify_all(user)] == [
        "user_message", "tool_result"]


def test_malformed_transcript_shapes_archive_without_retryable_errors() -> None:
    malformed = (
        [],
        "scalar",
        {"type": "assistant", "message": "not-an-object"},
        {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "shell", "input": ["bad"]},
        ]}},
        {"type": "assistant", "message": {"content": [
            {"type": "text", "text": {"not": "text"}},
            {"type": "text", "text": 42},
        ]}},
        {"type": "user", "message": {"content": [
            {"type": "tool_result", "content": {"not": "text"}},
            {"type": "tool_result", "content": 42},
        ]}},
        {"type": "unknown", "message": {"content": ["raw evidence"]}},
    )
    for entry in malformed:
        assert _classify_all(entry) == []

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def commit(self) -> None:
            pass

    class Pool:
        def connection(self) -> Connection:
            return Connection()

    with tempfile.TemporaryDirectory() as td:
        old_home = core.CFG.home
        core.CFG.home = Path(td) / "memory"
        transcript = Path(td) / "malformed.jsonl"
        raw = "\n".join(json.dumps(value) for value in malformed) + "\n"
        transcript.write_text(raw, encoding="utf-8")
        try:
            core.CFG.ensure_dirs()
            with patch.object(ingest, "pool", return_value=Pool()), \
                 patch.object(ingest, "append_event",
                              side_effect=AssertionError("unexpected event")):
                result = ingest.ingest_transcript(
                    str(transcript), "malformed", None, "stop",
                    ingest_job_id="malformed-job",
                    captured_at="2026-07-10T23:59:59+00:00")
            assert result["ok"] is True
            assert result["new_events"] == 0
            assert core.read_blob(result["sha256"]) == transcript.read_bytes()
            entry = json.loads(
                (core.CFG.archive / "manifest.jsonl").read_text().splitlines()[0])
            assert entry["fonds_path"] == (
                "claude-code/2026/07/10/malformed.jsonl")
        finally:
            core.CFG.home = old_home


def test_capture_fonds_date_is_stable_across_midnight_retries() -> None:
    with tempfile.TemporaryDirectory() as td:
        transcript = Path(td) / "session.jsonl"
        transcript.write_text("", encoding="utf-8")
        archived_fonds: list[str] = []

        class Connection:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def commit(self) -> None:
                pass

        class Pool:
            def connection(self) -> Connection:
                return Connection()

        def archive(data: bytes, _mime: str, fonds: str, **_kwargs) -> str:
            assert data == b""
            archived_fonds.append(fonds)
            return hashlib.sha256(b"").hexdigest()

        created_at = "2026-07-10T23:59:59+00:00"
        with patch.object(ingest, "archive_bytes", side_effect=archive), \
             patch.object(ingest, "pool", return_value=Pool()):
            for _attempt in range(2):
                result = ingest.ingest_transcript(
                    str(transcript), "midnight", None, "stop",
                    ingest_job_id="shared-retry-job", captured_at=created_at)
                assert result["ok"] is True
            for session_id in (r"nested\session", "nested/session"):
                result = ingest.ingest_transcript(
                    str(transcript), session_id, None, "stop",
                    ingest_job_id="separator-job", captured_at=created_at)
                assert result["ok"] is True
        assert archived_fonds == [
            "claude-code/2026/07/10/midnight.jsonl",
            "claude-code/2026/07/10/midnight.jsonl",
            "claude-code/2026/07/10/nested/session.jsonl",
            "claude-code/2026/07/10/nested/session.jsonl",
        ]


def test_validated_blob_bytes_survive_path_swap_and_gc_rechecks_blob() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "spool"
        transcript = Path(td) / "session.jsonl"
        expected = b'{"type":"user","message":{"content":"safe"}}\n'
        transcript.write_bytes(expected)
        job = enqueue_capture(
            spool_root=root, transcript_path=transcript,
            session_id="swap", project=None, trigger="stop")
        blob = root / "blobs" / job["blob_sha256"]
        assert spool.read_validated_blob(root, job) == expected

        outside = Path(td) / "outside.bin"
        redirected = b"redirected bytes"
        outside.write_bytes(redirected)
        link_sha = hashlib.sha256(redirected).hexdigest()
        link = root / "blobs" / link_sha
        try:
            os.symlink(outside, link)
        except OSError:
            link_supported = False
        else:
            link_supported = True
            linked_job = {
                **job, "blob_sha256": link_sha,
                "blob_bytes": len(redirected)}
            try:
                spool.read_validated_blob(root, linked_job)
            except spool.PermanentSpoolError:
                pass
            else:
                raise AssertionError("redirected spool blob accepted")

            canonical = Path(td) / "canonical"
            canonical.write_bytes(redirected)
            assert not spool.gc_blob_if_unreferenced(root, link_sha, canonical)
            assert outside.read_bytes() == redirected
        if not link_supported:
            assert not os.path.lexists(link)

        redirected_outside = Path(td) / "redirected-spool-outside"
        redirected_outside.mkdir()
        redirected_root = Path(td) / "redirected-spool"
        _create_directory_link(redirected_outside, redirected_root)
        outside_before = set(redirected_outside.iterdir())
        try:
            ensure_layout(redirected_root)
        except spool.PermanentSpoolError:
            pass
        else:
            raise AssertionError("redirected root accepted during layout creation")
        assert set(redirected_outside.iterdir()) == outside_before
        try:
            spool.read_validated_blob(redirected_root, job)
        except spool.PermanentSpoolError:
            pass
        else:
            raise AssertionError("redirected spool root accepted")
        assert set(redirected_outside.iterdir()) == outside_before
        redirected_canonical = Path(td) / "redirected-canonical"
        redirected_canonical.write_bytes(expected)
        assert not spool.gc_blob_if_unreferenced(
            redirected_root, job["blob_sha256"], redirected_canonical)
        assert set(redirected_outside.iterdir()) == outside_before

        gc_root = Path(td) / "gc-spool"
        gc_source = Path(td) / "gc-source.jsonl"
        gc_source.write_bytes(expected)
        gc_job = enqueue_capture(
            spool_root=gc_root, transcript_path=gc_source,
            session_id="gc-swap", project=None, trigger="stop")
        next((gc_root / "incoming").glob("*.json")).unlink()
        gc_blob = gc_root / "blobs" / gc_job["blob_sha256"]
        gc_canonical = Path(td) / "gc-canonical"
        gc_canonical.write_bytes(expected)
        real_state_lock = spool._state_lock

        @contextlib.contextmanager
        def swap_canonical_before_gc_delete(lock_root: Path):
            gc_canonical.write_bytes(b"X" * len(expected))
            with real_state_lock(lock_root):
                yield

        with patch.object(
                spool, "_state_lock",
                side_effect=swap_canonical_before_gc_delete):
            assert not spool.gc_blob_if_unreferenced(
                gc_root, gc_job["blob_sha256"], gc_canonical)
        assert gc_blob.read_bytes() == expected
        gc_canonical.write_bytes(expected)
        redirected_state = Path(td) / "redirected-incoming"
        redirected_state.mkdir()
        (gc_root / "incoming").rmdir()
        _create_directory_link(redirected_state, gc_root / "incoming")
        assert not spool.gc_blob_if_unreferenced(
            gc_root, gc_job["blob_sha256"], gc_canonical)
        assert gc_blob.read_bytes() == expected

        old_home = core.CFG.home
        core.CFG.home = Path(td) / "memory"
        try:
            core.CFG.ensure_dirs()
            core.CFG.spool.mkdir(parents=True, exist_ok=True)
            drain_source = Path(td) / "drain.jsonl"
            drain_source.write_bytes(expected)
            drain_job = enqueue_capture(
                spool_root=core.CFG.spool, transcript_path=drain_source,
                session_id="drain-swap", project=None, trigger="stop")
            drain_blob = core.CFG.spool / "blobs" / drain_job["blob_sha256"]
            canonical = (core.CFG.archive / "objects" / "sha256" /
                         drain_job["blob_sha256"][:2] /
                         drain_job["blob_sha256"][2:4] /
                         drain_job["blob_sha256"])
            canonical.parent.mkdir(parents=True)
            canonical.write_bytes(expected)
            real_read = spool.read_validated_blob
            consumed: list[bytes] = []

            def read_then_swap(spool_root: Path, value: dict) -> bytes:
                data = real_read(spool_root, value)
                drain_blob.write_bytes(b"X" * len(data))
                return data

            def consume(_path: str, _session: str, _project: str | None,
                        _trigger: str, **kwargs) -> dict:
                consumed.append(kwargs["transcript_bytes"])
                return {"ok": True, "sha256": drain_job["blob_sha256"],
                        "new_events": 0}

            with patch.object(spool, "read_validated_blob",
                              side_effect=read_then_swap), \
                 patch.object(ingest, "ingest_transcript", side_effect=consume):
                stats = ingest.drain_spool()
            assert stats["processed"] == 1
            assert consumed == [expected]
            assert drain_blob.exists()
            assert drain_blob.read_bytes() != expected
        finally:
            core.CFG.home = old_home


def test_fonds_paths_cannot_escape_archive() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "archive"
        for unsafe in (
                "", ".", "../escape", "/absolute/path", "a//b", "a/",
                r"C:\escape", r"C:escape", r"a\..\escape"):
            try:
                core.validate_fonds_path(root, unsafe)
            except ValueError:
                pass
            else:
                raise AssertionError(f"unsafe path accepted: {unsafe}")
        safe = core.validate_fonds_path(root, "claude-code/2026/07/session.jsonl")
        assert safe.is_relative_to((root / "fonds").resolve())

        objects = root / "objects"
        objects.mkdir(parents=True)
        leaf = root / "fonds" / "safe" / "existing"
        leaf.parent.mkdir(parents=True)
        _create_directory_link(objects, leaf)
        expected_leaf = (root / "fonds").resolve() / "safe" / "existing"
        assert core.validate_fonds_path(root, "safe/existing") == expected_leaf

        outside = Path(td) / "outside"
        outside.mkdir()
        escaping_parent = root / "fonds" / "escaping"
        _create_directory_link(outside, escaping_parent)
        try:
            core.validate_fonds_path(root, "escaping/file.txt")
        except ValueError:
            pass
        else:
            raise AssertionError("unsafe parent symlink accepted")

        old_home = core.CFG.home
        try:
            core.CFG.home = Path(td) / "pre-swapped-memory"
            core.CFG.ensure_dirs()
            redirected_root = core.CFG.archive / "fonds"
            redirected_root.rmdir()
            redirected_outside = Path(td) / "pre-swapped-outside"
            redirected_outside.mkdir()
            _create_directory_link(redirected_outside, redirected_root)
            try:
                core.archive_bytes(b"redirected", "text/plain", "capture.txt")
            except ValueError:
                pass
            else:
                raise AssertionError("pre-swapped fonds root accepted")
            assert not os.path.lexists(redirected_outside / "capture.txt")
            assert not (core.CFG.archive / "manifest.jsonl").exists()

            core.CFG.home = Path(td) / "memory"
            core.CFG.ensure_dirs()
            outside = Path(td) / "swap-outside"
            outside.mkdir()
            parent = core.CFG.archive / "fonds" / "swap"
            parent.mkdir()
            original_validate = core.validate_fonds_path
            swapped = False

            def validate_then_swap(archive_root: Path, fonds_path: str) -> Path:
                nonlocal swapped
                target = original_validate(archive_root, fonds_path)
                if not swapped:
                    parent.rmdir()
                    if os.name == "nt":
                        import _winapi
                        _winapi.CreateJunction(str(outside), str(parent))
                    else:
                        _create_directory_link(outside, parent)
                    swapped = True
                return target

            def windows_link_marker(target: str, link: str, **kwargs) -> None:
                if kwargs:
                    raise AssertionError("unsafe Windows fonds mutation attempted")
                Path(link).write_text(target)

            link_patch = (
                patch.object(core.os, "symlink", side_effect=windows_link_marker)
                if os.name == "nt" else contextlib.nullcontext()
            )
            with patch.object(core, "validate_fonds_path",
                              side_effect=validate_then_swap), link_patch:
                core.archive_bytes(b"swap", "text/plain", "swap/capture.txt")
            assert not os.path.lexists(outside / "capture.txt")

            core.CFG.home = Path(td) / "root-swap-memory"
            core.CFG.ensure_dirs()
            outside_root = Path(td) / "root-swap-outside"
            outside_root.mkdir()
            fonds_root = core.CFG.archive / "fonds"
            swapped = False

            def validate_then_swap_root(archive_root: Path,
                                        fonds_path: str) -> Path:
                nonlocal swapped
                target = original_validate(archive_root, fonds_path)
                if not swapped:
                    fonds_root.rmdir()
                    if os.name == "nt":
                        import _winapi
                        _winapi.CreateJunction(str(outside_root), str(fonds_root))
                    else:
                        _create_directory_link(outside_root, fonds_root)
                    swapped = True
                return target

            with patch.object(core, "validate_fonds_path",
                              side_effect=validate_then_swap_root), link_patch:
                core.archive_bytes(b"root-swap", "text/plain", "capture.txt")
            assert not os.path.lexists(outside_root / "capture.txt")
        finally:
            core.CFG.home = old_home


def test_archive_object_ancestors_cannot_redirect_publication_or_gc() -> None:
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        old_home = core.CFG.home
        core.CFG.home = base / "publication-home"
        try:
            core.CFG.ensure_dirs()
            data = b"redirected archive ancestor"
            sha = hashlib.sha256(data).hexdigest()
            shard = core.CFG.archive / "objects" / "sha256" / sha[:2]
            outside = base / "publication-outside"
            outside.mkdir()
            _create_directory_link(outside, shard)
            outside_before = _tree_snapshot(outside)
            try:
                core.archive_bytes(
                    data, "text/plain", "redirected/capture.txt")
            except (OSError, ValueError):
                pass
            else:
                raise AssertionError("redirected archive shard accepted")
            assert _tree_snapshot(outside) == outside_before
            assert not (core.CFG.archive / "manifest.jsonl").exists()
        finally:
            core.CFG.home = old_home

        data = b"gc redirected archive ancestor"
        sha = hashlib.sha256(data).hexdigest()
        spool_root = base / "gc-spool"
        source = base / "gc-source.jsonl"
        source.write_bytes(data)
        job = enqueue_capture(
            spool_root=spool_root, transcript_path=source,
            session_id="gc-redirected-shard", project=None, trigger="stop")
        assert job["blob_sha256"] == sha
        next((spool_root / "incoming").glob("*.json")).unlink()
        blob = spool_root / "blobs" / sha

        archive = base / "gc-archive"
        object_root = archive / "objects" / "sha256"
        object_root.mkdir(parents=True)
        outside = base / "gc-outside"
        external_object = outside / sha[2:4] / sha
        external_object.parent.mkdir(parents=True)
        external_object.write_bytes(data)
        _create_directory_link(outside, object_root / sha[:2])
        canonical = object_root / sha[:2] / sha[2:4] / sha

        assert not spool.gc_blob_if_unreferenced(
            spool_root, sha, canonical)
        assert blob.read_bytes() == data
        assert external_object.read_bytes() == data


def test_archive_leaf_swap_rolls_back_manifest_and_preserves_temp() -> None:
    with tempfile.TemporaryDirectory() as td:
        old_home = core.CFG.home
        core.CFG.home = Path(td) / "memory"
        try:
            core.CFG.ensure_dirs()
            data = b"known-good archive leaf"
            sha = hashlib.sha256(data).hexdigest()
            obj = (core.CFG.archive / "objects" / "sha256" /
                   sha[:2] / sha[2:4] / sha)
            real_append = core.append_manifest_occurrence
            swapped = False

            def swap_before_locked_append(
                    archive_root: Path, occurrence: dict, **kwargs) -> bool:
                nonlocal swapped
                if not swapped:
                    swapped = True
                    try:
                        obj.unlink()
                        obj.write_bytes(b"X" * len(data))
                    except PermissionError:
                        # Windows denies replacement while the verified leaf
                        # descriptor is open. Force the same lock-bound
                        # precondition failure to exercise rollback/preservation.
                        with patch.object(
                                core, "_archive_object_still_bound",
                                return_value=False):
                            return real_append(
                                archive_root, occurrence, **kwargs)
                return real_append(archive_root, occurrence, **kwargs)

            with patch.object(
                    core, "append_manifest_occurrence",
                    side_effect=swap_before_locked_append):
                try:
                    core.archive_bytes(
                        data, "text/plain", "swap/final-leaf.txt",
                        ingest_job_id="leaf-swap")
                except ValueError:
                    pass
                else:
                    raise AssertionError("swapped archive leaf was manifested")

            assert swapped
            manifest = core.CFG.archive / "manifest.jsonl"
            assert not manifest.exists() or not manifest.read_bytes()
            assert obj.read_bytes() in (data, b"X" * len(data))
            preserved = list(obj.parent.glob(f".{sha}.*.tmp"))
            assert preserved
            assert any(path.read_bytes() == data for path in preserved)
        finally:
            core.CFG.home = old_home


def test_archive_records_each_occurrence() -> None:
    with tempfile.TemporaryDirectory() as td:
        old_home = core.CFG.home
        core.CFG.home = Path(td)
        try:
            core.CFG.ensure_dirs()
            sha1 = core.archive_bytes(b"same", "text/plain", "a/one.txt", ingest_job_id="j1")
            sha2 = core.archive_bytes(b"same", "text/plain", "b/two.txt", ingest_job_id="j2")
            assert sha1 == sha2
            entries = [json.loads(line) for line in
                       (core.CFG.archive / "manifest.jsonl").read_text().splitlines()]
            assert [entry["fonds_path"] for entry in entries] == ["a/one.txt", "b/two.txt"]
            assert [entry["ingest_job_id"] for entry in entries] == ["j1", "j2"]

            context = multiprocessing.get_context("spawn")
            ready = context.Barrier(2)
            processes = [
                context.Process(
                    target=_archive_with_synchronized_publication,
                    args=(td, str(index), index == 0, ready),
                )
                for index in range(2)
            ]
            for process in processes:
                process.start()
            _join_processes(processes)
            assert [process.exitcode for process in processes] == [0, 0]

            entries = [json.loads(line) for line in
                       (core.CFG.archive / "manifest.jsonl").read_text().splitlines()]
            assert {entry["ingest_job_id"] for entry in entries} == {
                "j1", "j2", "0", "1"}
            object_files = [path for path in
                            (core.CFG.archive / "objects" / "sha256").rglob("*")
                            if path.is_file()]
            assert len(object_files) == 2

            with patch.object(core.os, "link",
                              side_effect=PermissionError("persistent denial")), \
                 patch.object(core.os, "replace",
                              side_effect=PermissionError("persistent denial")), \
                 patch.object(core.time, "monotonic", side_effect=(0, 6)):
                try:
                    core.archive_bytes(b"blocked", "text/plain", "blocked/file.txt")
                except PermissionError:
                    pass
                else:
                    raise AssertionError("unrelated object denial was swallowed")

            _assert_manifest_permission_contention_is_bounded(
                core.CFG.archive / "manifest.jsonl")
            _assert_manifest_lock_ownership_is_safe(
                core.CFG.archive / "manifest.jsonl")
            _assert_corrupt_objects_are_rejected()
        finally:
            core.CFG.home = old_home


def _tree_snapshot(root: Path) -> dict[str, tuple[int, int, int, bytes]] | None:
    if not root.exists():
        return None
    snapshot = {}
    for path in [root, *sorted(root.rglob("*"))]:
        path_stat = path.stat(follow_symlinks=False)
        content = path.read_bytes() if path.is_file() else b""
        snapshot[str(path.relative_to(root))] = (
            path_stat.st_mode,
            path_stat.st_size,
            path_stat.st_mtime_ns,
            content,
        )
    return snapshot


def _doctor_job(job_id: str, *, kind: str = "extraction", **fields) -> dict:
    job = {
        "schema_version": 2,
        "job_id": job_id,
        "kind": kind,
        "created_at": "2026-07-10T08:00:00+00:00",
        "session_id": f"session-{job_id}",
        "attempts": 0,
        "last_error": None,
        "next_attempt_at": None,
    }
    job.update(fields)
    return job


def test_session_separator_fonds_identity_matches_doctor_repair() -> None:
    with tempfile.TemporaryDirectory() as td:
        home = Path(td)
        spool_root = home / "spool"
        paths = ensure_layout(spool_root)
        archive_root = home / "archive"
        data = b"separator identity"
        sha = hashlib.sha256(data).hexdigest()
        obj = (archive_root / "objects" / "sha256" /
               sha[:2] / sha[2:4] / sha)
        obj.parent.mkdir(parents=True)
        obj.write_bytes(data)
        job = _doctor_job(
            "separator-job", kind="capture_snapshot", project="memoryd",
            trigger="stop", original_transcript_path="C:/separator.jsonl",
            blob_sha256=sha, blob_bytes=len(data),
            session_id=r"nested\session")
        (paths["dead-letter"] / "separator.json").write_text(
            json.dumps(job), encoding="utf-8")
        manifest = archive_root / "manifest.jsonl"
        manifest.parent.mkdir(parents=True, exist_ok=True)
        expected_fonds = "claude-code/2026/07/10/nested/session.jsonl"
        manifest.write_text(json.dumps({
            "sha256": sha,
            "bytes": len(data),
            "mime": "application/x-jsonl",
            "occurrence_at": job["created_at"],
            "fonds_path": expected_fonds,
            "ingest_job_id": job["job_id"],
        }) + "\n", encoding="utf-8")

        before = manifest.read_bytes()
        actions = repair_archive(archive_root, spool_root)
        assert not any(
            item.code == "occurrence_identity_collision" for item in actions)
        assert not any(
            item.code == "manifest_occurrence_reconstructed" for item in actions)
        assert manifest.read_bytes() == before


def test_doctor_reports_unmanifested_capture_evidence() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "spool"
        paths = ensure_layout(root)
        data = b"preserved collision evidence"
        sha = hashlib.sha256(data).hexdigest()
        collision = paths["blobs"] / f".collision.{sha}.job_preserved"
        temporary = paths["blobs"] / ".job_preserved.tmp"
        collision.write_bytes(data)
        temporary.write_bytes(b"temporary evidence")

        findings = inspect_spool(root)
        evidence = [
            item for item in findings
            if item.code == "unmanifested_capture_evidence"]
        assert {Path(item.path) for item in evidence} == {
            collision, temporary}
        assert all(item.severity == "error" for item in evidence)
        assert collision.read_bytes() == data
        assert temporary.read_bytes() == b"temporary evidence"


def test_doctor_inspection_is_read_only_and_reports_defects() -> None:
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        old_home = core.CFG.home
        missing_home = base / "missing-home"
        try:
            core.CFG.home = missing_home
            stdout = io.StringIO()
            with patch("memoryd.core.pool", side_effect=OSError("database down")), \
                 patch("memoryd.doctor.urllib.request.urlopen",
                       side_effect=OSError("daemon down")), \
                 contextlib.redirect_stdout(stdout):
                assert doctor_main() == 1
            assert not missing_home.exists()
            assert "database_unreachable" in stdout.getvalue()
            assert "daemon_unreachable" in stdout.getvalue()
        finally:
            core.CFG.home = old_home

        empty_home = base / "empty-home"
        empty_home.mkdir()
        before_empty = _tree_snapshot(empty_home)
        assert inspect_spool(empty_home / "spool") == []
        assert inspect_archive(empty_home / "archive") == []
        assert _tree_snapshot(empty_home) == before_empty

        cli_home = base / "invalid-cli-home"
        env = {**os.environ, "MEMORYD_HOME": str(cli_home)}
        invalid_cli = subprocess.run(
            [sys.executable, "-m", "memoryd", "doctor", "--unknown"],
            capture_output=True, text=True, timeout=15, env=env, check=False)
        assert invalid_cli.returncode == 2
        assert "usage: memoryd doctor [--repair]" in invalid_cli.stderr
        assert not cli_home.exists()

        home = base / "defects"
        paths = ensure_layout(home / "spool")
        legacy = home / "spool" / "legacy.json"
        legacy.write_text(json.dumps({
            "transcript_path": str(home / "gone.jsonl"),
            "session_id": "legacy",
            "trigger": "stop",
        }), encoding="utf-8")
        (home / "spool" / "malformed.json").write_text(
            "{", encoding="utf-8")
        (paths["incoming"] / "unexpected.json").write_text(
            "[]", encoding="utf-8")
        (paths["incoming"] / "unreadable.json").mkdir()
        missing_blob_job = _doctor_job(
            "missing-blob", kind="capture_snapshot", project=None,
            trigger="stop", original_transcript_path="C:/gone.jsonl",
            blob_sha256="b" * 64, blob_bytes=1)
        (paths["incoming"] / "missing-blob.json").write_text(
            json.dumps(missing_blob_job), encoding="utf-8")

        stale = paths["processing"] / "stale.json"
        stale.write_text(json.dumps(_doctor_job("stale")), encoding="utf-8")
        old = (datetime.now(timezone.utc) - timedelta(minutes=20)).timestamp()
        os.utime(stale, (old, old))

        dead_evidence = paths["dead-letter"] / "evidence.json"
        dead_evidence.write_text(
            json.dumps(_doctor_job("evidence")), encoding="utf-8")
        sidecar = dead_letter_reason_path(dead_evidence)
        sidecar.write_text(json.dumps({
            "dead_lettered_at": "2026-07-10T08:05:00+00:00",
            "reason": "preserved",
            "manifest": dead_evidence.name,
        }), encoding="utf-8")
        named_like_sidecar = paths["dead-letter"] / "capture.reason.json"
        named_like_sidecar.write_text(
            json.dumps(_doctor_job("named-like-sidecar")), encoding="utf-8")
        missing_reason = paths["dead-letter"] / "missing-reason.json"
        missing_reason.write_text(
            json.dumps(_doctor_job("missing-reason")), encoding="utf-8")
        malformed_reason_job = paths["dead-letter"] / "malformed-reason.json"
        malformed_reason_job.write_text(
            json.dumps(_doctor_job("malformed-reason")), encoding="utf-8")
        dead_letter_reason_path(malformed_reason_job).write_text(
            "{}", encoding="utf-8")
        mismatched_reason_job = paths["dead-letter"] / "mismatch.json"
        mismatched_reason_job.write_text(
            json.dumps(_doctor_job("mismatch")), encoding="utf-8")
        (paths["dead-letter"] / "misplaced-record.json").write_text(
            json.dumps({
                "dead_lettered_at": "2026-07-10T08:06:00+00:00",
                "reason": "wrong path",
                "manifest": mismatched_reason_job.name,
            }), encoding="utf-8")
        (paths["dead-letter"] / "absent.reason.json").write_text(
            json.dumps({
                "dead_lettered_at": "2026-07-10T08:07:00+00:00",
                "reason": "orphan",
                "manifest": "absent.json",
            }), encoding="utf-8")

        archive = home / "archive"
        object_root = archive / "objects" / "sha256"
        corrupt_sha = "c" * 64
        corrupt = object_root / corrupt_sha[:2] / corrupt_sha[2:4] / corrupt_sha
        corrupt.parent.mkdir(parents=True)
        corrupt.write_bytes(b"corrupt")
        orphan_data = b"orphan"
        orphan_sha = hashlib.sha256(orphan_data).hexdigest()
        orphan = object_root / orphan_sha[:2] / orphan_sha[2:4] / orphan_sha
        orphan.parent.mkdir(parents=True)
        orphan.write_bytes(orphan_data)
        bad_name = object_root / "bad-object-name"
        bad_name.write_bytes(b"evidence")
        (archive / "manifest.jsonl").write_text("\n".join((
            json.dumps({"sha256": "a" * 64, "fonds_path": "safe/path"}),
            json.dumps({"sha256": corrupt_sha, "fonds_path": "../escape"}),
            "{",
            "[]",
            json.dumps({"sha256": ["bad"], "fonds_path": "safe/path"}),
        )) + "\n", encoding="utf-8")

        before = _tree_snapshot(home)
        spool_findings = inspect_spool(home / "spool")
        archive_findings = inspect_archive(archive)
        assert _tree_snapshot(home) == before

        spool_codes = {item.code for item in spool_findings}
        assert {
            "invalid_manifest", "legacy_source_missing", "spool_blob_invalid",
            "stale_processing_job", "dead_letter_jobs",
            "dead_letter_reason_missing", "dead_letter_reason_invalid",
            "dead_letter_reason_mismatched", "dead_letter_reason_orphan",
        } <= spool_codes
        dead_count = next(
            item for item in spool_findings if item.code == "dead_letter_jobs")
        assert dead_count.detail == "5"
        assert all(item.path != str(sidecar) for item in spool_findings)

        archive_codes = {item.code for item in archive_findings}
        assert {
            "invalid_manifest_line", "unsafe_fonds_path",
            "manifest_object_missing", "object_hash_mismatch", "orphan_object",
            "invalid_object_name",
        } <= archive_codes

        topology_home = base / "topology"
        topology_spool = topology_home / "spool"
        topology_spool.mkdir(parents=True)
        (topology_spool / "incoming").write_text(
            "not a directory", encoding="utf-8")
        outside_archive = base / "outside-inspection-archive"
        outside_archive.mkdir()
        _create_directory_link(outside_archive, topology_home / "archive")
        before_topology = _tree_snapshot(topology_home)
        before_outside = _tree_snapshot(outside_archive)
        assert "spool_topology_invalid" in {
            item.code for item in inspect_spool(topology_spool)}
        assert "archive_topology_invalid" in {
            item.code for item in inspect_archive(topology_home / "archive")}
        old_home = core.CFG.home
        output = io.StringIO()
        try:
            core.CFG.home = topology_home
            with patch("memoryd.core.pool", side_effect=OSError("database down")), \
                 patch("memoryd.doctor.urllib.request.urlopen",
                       side_effect=OSError("daemon down")), \
                 contextlib.redirect_stdout(output):
                assert doctor_main() == 1
        finally:
            core.CFG.home = old_home
        assert "spool_topology_invalid" in output.getvalue()
        assert "archive_topology_invalid" in output.getvalue()
        assert _tree_snapshot(topology_home) == before_topology
        assert _tree_snapshot(outside_archive) == before_outside


def test_doctor_repair_preserves_and_requeues_spool_evidence() -> None:
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        root = base / "spool"
        paths = ensure_layout(root)
        source = base / "source.jsonl"
        source.write_bytes(b"source evidence")
        upgrade = root / "upgrade.json"
        upgrade_value = {
            "transcript_path": str(source),
            "session_id": "upgrade",
            "trigger": "stop",
        }
        upgrade.write_text(json.dumps(upgrade_value), encoding="utf-8")
        missing = root / "missing.json"
        missing_value = {
            "transcript_path": str(base / "gone.jsonl"),
            "session_id": "missing",
            "trigger": "stop",
        }
        missing.write_text(json.dumps(missing_value), encoding="utf-8")
        malformed = paths["incoming"] / "malformed.json"
        malformed.write_bytes(b"{original malformed evidence")
        named_like_sidecar = paths["incoming"] / "capture.reason.json"
        corrupt_value = _doctor_job(
            "corrupt", kind="capture_snapshot", project=None, trigger="stop",
            original_transcript_path="C:/gone.jsonl",
            blob_sha256="d" * 64, blob_bytes=1)
        named_like_sidecar.write_text(
            json.dumps(corrupt_value), encoding="utf-8")
        stale = paths["processing"] / "stale.json"
        stale.write_text(json.dumps(_doctor_job("stale")), encoding="utf-8")
        old = (datetime.now(timezone.utc) - timedelta(minutes=20)).timestamp()
        os.utime(stale, (old, old))

        preserved_dead = paths["dead-letter"] / "preserved.json"
        preserved_dead.write_text(
            json.dumps(_doctor_job("preserved")), encoding="utf-8")
        preserved_reason = dead_letter_reason_path(preserved_dead)
        preserved_reason.write_text(json.dumps({
            "dead_lettered_at": "2026-07-10T08:05:00+00:00",
            "reason": "existing evidence",
            "manifest": preserved_dead.name,
        }), encoding="utf-8")
        collision_dead = paths["dead-letter"] / missing.name
        collision_dead.write_text(
            json.dumps(_doctor_job("existing-missing")), encoding="utf-8")
        dead_letter_reason_path(collision_dead).write_text(json.dumps({
            "dead_lettered_at": "2026-07-10T08:05:30+00:00",
            "reason": "existing collision",
            "manifest": collision_dead.name,
        }), encoding="utf-8")
        dead_before = {
            preserved_dead.name: preserved_dead.read_bytes(),
            preserved_reason.name: preserved_reason.read_bytes(),
        }

        actions = repair_spool(root)
        action_codes = {item.code for item in actions}
        assert {
            "stale_jobs_requeued", "legacy_upgraded", "legacy_dead_lettered",
            "invalid_job_dead_lettered", "corrupt_job_dead_lettered",
        } <= action_codes
        dead_lettered = next(
            item for item in actions if item.code == "legacy_dead_lettered")
        assert Path(dead_lettered.path).is_file()
        assert Path(dead_lettered.path).parent == paths["dead-letter"]
        assert Path(dead_lettered.path).name != collision_dead.name
        assert source.read_bytes() == b"source evidence"
        assert (paths["incoming"] / "stale.json").exists()

        dead_files = list(paths["dead-letter"].glob("*.json"))
        evidence = [path for path in dead_files
                    if path.name not in {preserved_reason.name} and
                    not spool.is_dead_letter_sidecar(path)]
        evidence_contents = {path.read_bytes() for path in evidence}
        assert json.dumps(upgrade_value).encode() in evidence_contents
        assert json.dumps(missing_value).encode() in evidence_contents
        assert b"{original malformed evidence" in evidence_contents
        assert json.dumps(corrupt_value).encode() in evidence_contents
        assert any(path.name.startswith("capture.reason") for path in evidence)
        assert all(dead_letter_reason_path(path).is_file() for path in evidence)
        assert {
            preserved_dead.name: preserved_dead.read_bytes(),
            preserved_reason.name: preserved_reason.read_bytes(),
        } == dead_before

        transition_root = base / "transition-spool"
        transition_paths = ensure_layout(transition_root)
        incoming_collision = transition_paths["incoming"] / "collision.json"
        processing_collision = transition_paths["processing"] / "collision.json"
        incoming_collision.write_text(
            json.dumps({"marker": "incoming"}), encoding="utf-8")
        processing_collision.write_text(
            json.dumps({"marker": "processing"}), encoding="utf-8")
        os.utime(processing_collision, (old, old))
        assert spool.requeue_stale(transition_root) == 1
        collision_values = [
            json.loads(path.read_text(encoding="utf-8"))
            for path in transition_paths["incoming"].glob("collision*.json")]
        assert {value["marker"] for value in collision_values} == {
            "incoming", "processing"}

        retry_processing = transition_paths["processing"] / "retry.json"
        retry_processing.write_text(
            json.dumps(_doctor_job("retry")), encoding="utf-8")
        retry_incoming = transition_paths["incoming"] / "retry.json"
        retry_incoming.write_text(
            json.dumps({"marker": "preserved retry"}), encoding="utf-8")
        released = spool.release_job(
            transition_root, retry_processing, "retry", delay_s=1)
        assert released != retry_incoming
        assert json.loads(retry_incoming.read_text())["marker"] == "preserved retry"
        assert json.loads(released.read_text())["last_error"] == "retry"

        root_claim = transition_root / "claim.json"
        root_claim.write_text(
            json.dumps(_doctor_job("claim-source")), encoding="utf-8")
        occupied_claim = transition_paths["processing"] / "claim.json"
        occupied_claim.write_text(
            json.dumps(_doctor_job("claim-occupied")), encoding="utf-8")
        claimed = spool.claim_next(transition_root)
        assert claimed is not None and claimed != occupied_claim
        assert occupied_claim.is_file()

        unsafe_root = base / "unsafe-lock-spool"
        unsafe_root.mkdir()
        (unsafe_root / "state.lock").mkdir()
        before_unsafe = _tree_snapshot(unsafe_root)
        refused = repair_spool(unsafe_root)
        assert any(item.code == "repair_refused" for item in refused)
        assert _tree_snapshot(unsafe_root) == before_unsafe

        redirected_root = base / "redirected-spool"
        redirected_root.mkdir()
        redirected_outside = base / "redirected-spool-outside"
        redirected_outside.mkdir()
        redirected_job = redirected_outside / "redirected.json"
        redirected_job.write_text(
            json.dumps(missing_value), encoding="utf-8")
        _create_directory_link(
            redirected_outside, redirected_root / "incoming")
        before_redirected = _tree_snapshot(redirected_root)
        before_redirected_outside = _tree_snapshot(redirected_outside)
        redirected_actions = repair_spool(redirected_root)
        assert any(
            item.code == "repair_refused" for item in redirected_actions)
        assert _tree_snapshot(redirected_root) == before_redirected
        assert _tree_snapshot(redirected_outside) == before_redirected_outside

        cli_home = base / "unsafe-cli-home"
        cli_home.mkdir()
        cli_outside = base / "unsafe-cli-archive"
        cli_outside.mkdir()
        _create_directory_link(cli_outside, cli_home / "archive")
        before_cli_home = _tree_snapshot(cli_home)
        before_cli_outside = _tree_snapshot(cli_outside)
        old_home = core.CFG.home
        try:
            core.CFG.home = cli_home
            with patch("memoryd.core.pool", side_effect=OSError("database down")), \
                 patch("memoryd.doctor.urllib.request.urlopen",
                       side_effect=OSError("daemon down")), \
                 contextlib.redirect_stdout(io.StringIO()):
                assert doctor_main(repair=True) == 1
        finally:
            core.CFG.home = old_home
        assert _tree_snapshot(cli_home) == before_cli_home
        assert _tree_snapshot(cli_outside) == before_cli_outside

        evidence_root = base / "redirected-evidence-spool"
        evidence_paths = ensure_layout(evidence_root)
        redirected_manifest = evidence_paths["incoming"] / "redirected.json"
        redirected_manifest.write_text(
            json.dumps(_doctor_job("redirected")), encoding="utf-8")
        evidence_before = _tree_snapshot(evidence_root)
        real_redirected = doctor._redirected
        with patch.object(
                doctor, "_redirected",
                side_effect=lambda path, value: (
                    path == redirected_manifest or real_redirected(path, value))):
            redirected_file_actions = repair_spool(evidence_root)
        assert any(
            item.code == "repair_refused" for item in redirected_file_actions)
        assert _tree_snapshot(evidence_root) == evidence_before

        blob_root = base / "redirected-blob-spool"
        blob_paths = ensure_layout(blob_root)
        blob_data = b"blob evidence"
        blob_sha = hashlib.sha256(blob_data).hexdigest()
        blob = blob_paths["blobs"] / blob_sha
        blob.write_bytes(blob_data)
        blob_manifest = blob_paths["incoming"] / "blob.json"
        blob_manifest.write_text(json.dumps(_doctor_job(
            "blob", kind="capture_snapshot", project=None, trigger="stop",
            original_transcript_path="C:/blob.jsonl", blob_sha256=blob_sha,
            blob_bytes=len(blob_data))), encoding="utf-8")
        blob_before = _tree_snapshot(blob_root)
        with patch.object(
                doctor, "_redirected",
                side_effect=lambda path, value: (
                    path == blob or real_redirected(path, value))):
            blob_actions = repair_spool(blob_root)
        assert any(item.code == "repair_refused" for item in blob_actions)
        assert _tree_snapshot(blob_root) == blob_before

        scandir_root = base / "scandir-spool"
        ensure_layout(scandir_root)
        scandir_before = _tree_snapshot(scandir_root)
        with patch.object(
                doctor.os, "scandir", side_effect=PermissionError("denied")):
            assert "spool_topology_unreadable" in {
                item.code for item in inspect_spool(scandir_root)}
            scandir_actions = repair_spool(scandir_root)
        assert any(item.code == "repair_refused" for item in scandir_actions)
        assert _tree_snapshot(scandir_root) == scandir_before

        layout_root = base / "layout-failure-spool"
        with patch.object(
                doctor, "ensure_layout", side_effect=OSError("mkdir denied")):
            layout_actions = repair_spool(layout_root)
        assert any(item.code == "repair_refused" for item in layout_actions)
        assert not layout_root.exists()

        disappearing_root = base / "disappearing-legacy-spool"
        disappearing_paths = ensure_layout(disappearing_root)
        disappearing_source = base / "disappearing.jsonl"
        disappearing_source.write_text("evidence", encoding="utf-8")
        disappearing_job = disappearing_root / "legacy.json"
        disappearing_job.write_text(json.dumps({
            "transcript_path": str(disappearing_source),
            "session_id": "disappearing", "trigger": "stop",
        }), encoding="utf-8")
        occupied = disappearing_paths["dead-letter"] / disappearing_job.name
        occupied.write_text(json.dumps(_doctor_job("occupied")), encoding="utf-8")
        dead_letter_reason_path(occupied).write_text(json.dumps({
            "dead_lettered_at": "2026-07-10T08:00:00+00:00",
            "reason": "occupied", "manifest": occupied.name,
        }), encoding="utf-8")

        def disappear_during_upgrade(root: Path, path: Path):
            disappearing_source.unlink()
            spool.dead_letter(root, path, "legacy transcript source missing")
            return None

        with patch.object(
                doctor, "upgrade_legacy_job",
                side_effect=disappear_during_upgrade):
            disappearing_actions = repair_spool(disappearing_root)
        disappeared = next(
            item for item in disappearing_actions
            if item.code == "legacy_dead_lettered")
        assert Path(disappeared.path).is_file()
        assert Path(disappeared.path) != disappearing_job
        assert Path(disappeared.path).name != occupied.name


def test_doctor_reconstructs_each_supported_occurrence_idempotently() -> None:
    with tempfile.TemporaryDirectory() as td:
        home = Path(td)
        spool_root = home / "spool"
        paths = ensure_layout(spool_root)
        data = b"archived"
        sha = hashlib.sha256(data).hexdigest()
        obj = home / "archive" / "objects" / "sha256" / sha[:2] / sha[2:4] / sha
        obj.parent.mkdir(parents=True)
        obj.write_bytes(data)
        jobs = (
            _doctor_job(
                "job-one", kind="capture_snapshot", project="memoryd",
                trigger="stop", original_transcript_path="C:/one.jsonl",
                blob_sha256=sha, blob_bytes=len(data), session_id="same-session"),
            _doctor_job(
                "job-two", kind="capture_snapshot", project="memoryd",
                trigger="stop", original_transcript_path="C:/two.jsonl",
                blob_sha256=sha, blob_bytes=len(data), session_id="same-session"),
        )
        (paths["dead-letter"] / "job-one.json").write_text(
            json.dumps(jobs[0]), encoding="utf-8")
        (paths["dead-letter"] / "job-two.reason.json").write_text(
            json.dumps(jobs[1]), encoding="utf-8")

        legacy_source = home / "legacy-source.jsonl"
        legacy_source.write_bytes(data)
        legacy = {
            "transcript_path": str(legacy_source),
            "session_id": "legacy-session",
            "trigger": "stop",
            "created_at": "2026-07-10T09:00:00+00:00",
            "blob_sha256": sha,
            "blob_bytes": len(data),
        }
        (spool_root / "legacy.json").write_text(
            json.dumps(legacy), encoding="utf-8")
        second_legacy = {
            **legacy,
            "transcript_path": str(home / "legacy-source-two.jsonl"),
            "created_at": "2026-07-10T09:01:00+00:00",
        }
        Path(second_legacy["transcript_path"]).write_bytes(data)
        (spool_root / "legacy-two.json").write_text(
            json.dumps(second_legacy), encoding="utf-8")
        fonds_evidence = home / "archive" / "fonds" / "preserved.txt"
        fonds_evidence.parent.mkdir(parents=True)
        fonds_evidence.write_bytes(b"fonds evidence")
        manifest = home / "archive" / "manifest.jsonl"
        preserved_tail = b"{preserved malformed line"
        manifest.write_bytes(preserved_tail)

        actions = repair_archive(home / "archive", spool_root)
        reconstructed = [
            item for item in actions
            if item.code == "manifest_occurrence_reconstructed"]
        assert len(reconstructed) == 4
        assert manifest.read_bytes().startswith(preserved_tail + b"\n")
        lines = manifest.read_text(encoding="utf-8").splitlines()
        assert lines[0] == "{preserved malformed line"
        entries = [json.loads(line) for line in lines[1:]]
        assert {entry["ingest_job_id"] for entry in entries} == {
            "job-one", "job-two", None}
        assert sum(entry["ingest_job_id"] is None for entry in entries) == 2
        assert all(entry["sha256"] == sha for entry in entries)
        assert legacy_source.read_bytes() == data
        assert fonds_evidence.read_bytes() == b"fonds evidence"

        before_second_repair = _tree_snapshot(home)
        assert repair_archive(home / "archive", spool_root) == []
        assert _tree_snapshot(home) == before_second_repair

        fallback_home = home / "fallback-case"
        fallback_spool = fallback_home / "spool"
        ensure_layout(fallback_spool)
        fallback_archive = fallback_home / "archive"
        fallback_obj = (fallback_archive / "objects" / "sha256" /
                        sha[:2] / sha[2:4] / sha)
        fallback_obj.parent.mkdir(parents=True)
        fallback_obj.write_bytes(data)
        fallback_source = fallback_home / "source.jsonl"
        fallback_source.write_bytes(data)
        fallback_jobs = (
            {
                **legacy,
                "transcript_path": str(fallback_source),
                "created_at": "2026-07-10T10:00:00+00:00",
            },
            {
                **legacy,
                "transcript_path": str(fallback_source),
                "created_at": "2026-07-10T10:01:00+00:00",
            },
        )
        for index, value in enumerate(fallback_jobs):
            (fallback_spool / f"legacy-{index}.json").write_text(
                json.dumps(value), encoding="utf-8")
        fallback_fonds = "claude-code/2026/07/10/legacy-session.jsonl"
        fallback_manifest = fallback_archive / "manifest.jsonl"
        fallback_manifest.write_text(json.dumps({
            "sha256": sha,
            "bytes": len(data),
            "mime": "application/x-jsonl",
            "occurrence_at": "2026-07-10T10:00:00+00:00",
            "fonds_path": fallback_fonds,
            "ingest_job_id": None,
        }) + "\n", encoding="utf-8")
        fallback_actions = repair_archive(fallback_archive, fallback_spool)
        assert sum(
            item.code == "manifest_occurrence_reconstructed"
            for item in fallback_actions) == 1
        fallback_after = fallback_manifest.read_bytes()
        assert repair_archive(fallback_archive, fallback_spool) == []
        assert fallback_manifest.read_bytes() == fallback_after

        conflict_home = home / "identity-conflict"
        conflict_spool = conflict_home / "spool"
        conflict_paths = ensure_layout(conflict_spool)
        conflict_archive = conflict_home / "archive"
        conflict_obj = (conflict_archive / "objects" / "sha256" /
                        sha[:2] / sha[2:4] / sha)
        conflict_obj.parent.mkdir(parents=True)
        conflict_obj.write_bytes(data)
        conflict_job = _doctor_job(
            "duplicate-id", kind="capture_snapshot", project="memoryd",
            trigger="stop", original_transcript_path="C:/conflict.jsonl",
            blob_sha256=sha, blob_bytes=len(data), session_id="conflict")
        (conflict_paths["dead-letter"] / "conflict.json").write_text(
            json.dumps(conflict_job), encoding="utf-8")
        conflict_manifest = conflict_archive / "manifest.jsonl"
        conflict_manifest.write_text("\n".join((
            json.dumps({
                "sha256": sha,
                "fonds_path": "claude-code/2026/07/10/conflict.jsonl",
                "ingest_job_id": "duplicate-id",
            }),
            json.dumps({
                "sha256": "e" * 64,
                "fonds_path": "claude-code/2026/07/10/other.jsonl",
                "ingest_job_id": "duplicate-id",
            }),
        )) + "\n", encoding="utf-8")
        assert "occurrence_identity_collision" in {
            item.code for item in inspect_archive(conflict_archive)}
        conflict_before = conflict_manifest.read_bytes()
        conflict_actions = repair_archive(conflict_archive, conflict_spool)
        assert any(
            item.code == "occurrence_identity_collision"
            for item in conflict_actions)
        assert conflict_manifest.read_bytes() == conflict_before

        race_home = home / "object-race"
        race_spool = race_home / "spool"
        race_paths = ensure_layout(race_spool)
        race_archive = race_home / "archive"
        race_obj = (race_archive / "objects" / "sha256" /
                    sha[:2] / sha[2:4] / sha)
        race_obj.parent.mkdir(parents=True)
        race_obj.write_bytes(data)
        (race_paths["dead-letter"] / "race.json").write_text(json.dumps(
            _doctor_job(
                "race", kind="capture_snapshot", project="memoryd",
                trigger="stop", original_transcript_path="C:/race.jsonl",
                blob_sha256=sha, blob_bytes=len(data), session_id="race")),
            encoding="utf-8")
        real_fstat = os.fstat
        fstat_calls = 0

        def unstable_fstat(fd: int):
            nonlocal fstat_calls
            fstat_calls += 1
            result = real_fstat(fd)
            if fstat_calls == 2:
                fields = list(result)
                fields[1] += 1
                return os.stat_result(fields)
            return result

        with patch.object(doctor.os, "fstat", side_effect=unstable_fstat):
            race_actions = repair_archive(race_archive, race_spool)
        assert fstat_calls >= 2
        assert not any(
            item.code == "manifest_occurrence_reconstructed"
            for item in race_actions)
        assert not (race_archive / "manifest.jsonl").exists()

        unsafe_archive = home / "unsafe-archive"
        unsafe_archive.mkdir()
        (unsafe_archive / "manifest.lock").mkdir()
        unsafe_spool = home / "unsafe-archive-spool"
        ensure_layout(unsafe_spool)
        unsafe_before = _tree_snapshot(unsafe_archive)
        unsafe_actions = repair_archive(unsafe_archive, unsafe_spool)
        assert any(item.code == "repair_refused" for item in unsafe_actions)
        assert _tree_snapshot(unsafe_archive) == unsafe_before

        shard_home = home / "redirected-shard"
        shard_spool = shard_home / "spool"
        shard_paths = ensure_layout(shard_spool)
        shard_archive = shard_home / "archive"
        shard_root = shard_archive / "objects" / "sha256"
        shard_root.mkdir(parents=True)
        shard_outside = shard_home / "outside-shard"
        (shard_outside / sha[2:4]).mkdir(parents=True)
        (shard_outside / sha[2:4] / sha).write_bytes(data)
        _create_directory_link(shard_outside, shard_root / sha[:2])
        (shard_paths["dead-letter"] / "shard.json").write_text(json.dumps(
            _doctor_job(
                "shard", kind="capture_snapshot", project="memoryd",
                trigger="stop", original_transcript_path="C:/shard.jsonl",
                blob_sha256=sha, blob_bytes=len(data), session_id="shard")),
            encoding="utf-8")
        shard_before = _tree_snapshot(shard_archive)
        shard_outside_before = _tree_snapshot(shard_outside)
        shard_actions = repair_archive(shard_archive, shard_spool)
        assert any(item.code == "repair_refused" for item in shard_actions)
        assert _tree_snapshot(shard_archive) == shard_before
        assert _tree_snapshot(shard_outside) == shard_outside_before

        spool_conflict_home = home / "spool-id-conflict"
        spool_conflict = spool_conflict_home / "spool"
        spool_conflict_paths = ensure_layout(spool_conflict)
        conflict_data = (b"conflict-one", b"conflict-two")
        for index, value in enumerate(conflict_data):
            value_sha = hashlib.sha256(value).hexdigest()
            value_obj = (spool_conflict_home / "archive" / "objects" /
                         "sha256" / value_sha[:2] / value_sha[2:4] / value_sha)
            value_obj.parent.mkdir(parents=True)
            value_obj.write_bytes(value)
            (spool_conflict_paths["dead-letter"] /
             f"conflict-{index}.json").write_text(json.dumps(_doctor_job(
                "shared-spool-id", kind="capture_snapshot", project="memoryd",
                trigger="stop", original_transcript_path=f"C:/{index}.jsonl",
                blob_sha256=value_sha, blob_bytes=len(value),
                session_id=f"conflict-{index}")), encoding="utf-8")
        assert "occurrence_identity_collision" in {
            item.code for item in inspect_spool(spool_conflict)}
        spool_conflict_actions = repair_archive(
            spool_conflict_home / "archive", spool_conflict)
        assert any(
            item.code == "occurrence_identity_collision"
            for item in spool_conflict_actions)
        assert not (spool_conflict_home / "archive" / "manifest.jsonl").exists()

        rollback_archive = home / "rollback-archive"
        rollback_archive.mkdir()
        rollback_manifest = rollback_archive / "manifest.jsonl"
        rollback_original = b"unterminated evidence"
        rollback_manifest.write_bytes(rollback_original)
        try:
            core.append_manifest_occurrence(
                rollback_archive, {"sha256": sha, "fonds_path": "safe/path"},
                pre_append=lambda: True, post_append=lambda: False)
        except ValueError:
            pass
        else:
            raise AssertionError("failed post-append validation was retained")
        assert rollback_manifest.read_bytes() == rollback_original

        mkdir_home = home / "mkdir-failure"
        mkdir_spool = mkdir_home / "spool"
        ensure_layout(mkdir_spool)
        with patch.object(Path, "mkdir", side_effect=OSError("mkdir denied")):
            mkdir_actions = repair_archive(mkdir_home / "archive", mkdir_spool)
        assert any(item.code == "repair_refused" for item in mkdir_actions)
        assert not (mkdir_home / "archive").exists()

        redirected_object_home = home / "redirected-object"
        redirected_object_spool = redirected_object_home / "spool"
        redirected_object_paths = ensure_layout(redirected_object_spool)
        redirected_object_archive = redirected_object_home / "archive"
        redirected_object = (redirected_object_archive / "objects" / "sha256" /
                             sha[:2] / sha[2:4] / sha)
        redirected_object.parent.mkdir(parents=True)
        redirected_object.write_bytes(data)
        (redirected_object_paths["dead-letter"] / "job.json").write_text(
            json.dumps(_doctor_job(
                "redirected-object", kind="capture_snapshot", project="memoryd",
                trigger="stop", original_transcript_path="C:/object.jsonl",
                blob_sha256=sha, blob_bytes=len(data), session_id="object")),
            encoding="utf-8")
        redirected_object_before = _tree_snapshot(redirected_object_home)
        real_redirected = doctor._redirected
        with patch.object(
                doctor, "_redirected",
                side_effect=lambda path, value: (
                    path == redirected_object or real_redirected(path, value))):
            redirected_object_actions = repair_archive(
                redirected_object_archive, redirected_object_spool)
        assert any(
            item.code == "repair_refused"
            for item in redirected_object_actions)
        assert _tree_snapshot(redirected_object_home) == redirected_object_before


def test_doctor_rechecks_occurrence_identity_under_manifest_lock() -> None:
    with tempfile.TemporaryDirectory() as td:
        home = Path(td)
        spool_root = home / "spool"
        paths = ensure_layout(spool_root)
        archive_root = home / "archive"
        data = b"repair race"
        sha = hashlib.sha256(data).hexdigest()
        obj = (archive_root / "objects" / "sha256" /
               sha[:2] / sha[2:4] / sha)
        obj.parent.mkdir(parents=True)
        obj.write_bytes(data)
        job = _doctor_job(
            "repair-race", kind="capture_snapshot", project="memoryd",
            trigger="stop", original_transcript_path="C:/repair-race.jsonl",
            blob_sha256=sha, blob_bytes=len(data), session_id="repair-race")
        job["created_at"] = "2026-07-10T23:59:59+00:00"
        (paths["dead-letter"] / "repair-race.json").write_text(
            json.dumps(job), encoding="utf-8")

        real_append = core.append_manifest_occurrence
        injected = False

        def append_after_competing_writer(
                root: Path, occurrence: dict, **kwargs) -> bool:
            nonlocal injected
            if not injected:
                injected = True
                real_append(root, occurrence)
            return real_append(root, occurrence, **kwargs)

        with patch.object(
                doctor, "append_manifest_occurrence",
                side_effect=append_after_competing_writer):
            actions = repair_archive(archive_root, spool_root)

        entries = [json.loads(line) for line in
                   (archive_root / "manifest.jsonl").read_text().splitlines()]
        exact = [entry for entry in entries
                 if entry.get("ingest_job_id") == "repair-race"]
        assert len(exact) == 1
        assert exact[0]["fonds_path"] == (
            "claude-code/2026/07/10/repair-race.jsonl")
        assert not any(
            action.code == "manifest_occurrence_reconstructed"
            for action in actions)


if __name__ == "__main__":
    test_status_counts_spool_states()
    test_snapshot_survives_original_deletion()
    test_identical_snapshots_share_one_blob()
    test_snapshot_rejects_invalid_blob_collision_without_losing_bytes()
    test_publication_swap_preserves_known_good_temp()
    test_publication_fsyncs_blob_json_and_archive_namespaces()
    test_first_use_namespaces_sync_parents_and_eio_propagates()
    test_hook_spools_bytes_when_daemon_is_down()
    test_hook_warns_when_delivery_and_spooling_fail()
    test_capture_persists_snapshot_before_acknowledgement()
    test_claim_retry_and_dead_letter_preserve_manifest()
    test_spool_state_transitions_sync_directories_and_leases()
    test_legacy_missing_source_is_preserved()
    test_stale_processing_job_is_requeued()
    test_mixed_transcript_line_preserves_text_and_tools()
    test_malformed_transcript_shapes_archive_without_retryable_errors()
    test_capture_fonds_date_is_stable_across_midnight_retries()
    test_validated_blob_bytes_survive_path_swap_and_gc_rechecks_blob()
    test_fonds_paths_cannot_escape_archive()
    test_archive_object_ancestors_cannot_redirect_publication_or_gc()
    test_archive_leaf_swap_rolls_back_manifest_and_preserves_temp()
    test_archive_records_each_occurrence()
    test_session_separator_fonds_identity_matches_doctor_repair()
    test_doctor_reports_unmanifested_capture_evidence()
    test_doctor_inspection_is_read_only_and_reports_defects()
    test_doctor_repair_preserves_and_requeues_spool_evidence()
    test_doctor_reconstructs_each_supported_occurrence_idempotently()
    test_doctor_rechecks_occurrence_identity_under_manifest_lock()
    print("28 passed, 0 failed")
