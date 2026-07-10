from __future__ import annotations

import contextlib
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

from memoryd import core, ingest, server, spool
from memoryd.hook import capture
from memoryd.ingest import _classify_all
from memoryd.spool import enqueue_capture, ensure_layout, validate_blob


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
    stderr = io.StringIO()
    stdin = {"transcript_path": "missing.jsonl", "session_id": "s1", "cwd": ""}
    with patch("memoryd.hook._post", side_effect=OSError("down")), \
         contextlib.redirect_stderr(stderr):
        capture(stdin, "stop", 7437, Path("unused"))
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


if __name__ == "__main__":
    test_snapshot_survives_original_deletion()
    test_identical_snapshots_share_one_blob()
    test_hook_spools_bytes_when_daemon_is_down()
    test_hook_warns_when_delivery_and_spooling_fail()
    test_capture_persists_snapshot_before_acknowledgement()
    test_claim_retry_and_dead_letter_preserve_manifest()
    test_legacy_missing_source_is_preserved()
    test_stale_processing_job_is_requeued()
    test_mixed_transcript_line_preserves_text_and_tools()
    test_fonds_paths_cannot_escape_archive()
    test_archive_records_each_occurrence()
    print("11 passed, 0 failed")
