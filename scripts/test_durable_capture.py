from __future__ import annotations

import contextlib
import hashlib
import io
import json
import multiprocessing
import os
import tempfile
import threading
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from memoryd import core, server, spool
from memoryd.hook import capture
from memoryd.ingest import _classify_all
from memoryd.spool import enqueue_capture, ensure_layout, validate_blob


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
        finally:
            httpd.shutdown()
            httpd.server_close()
            if thread.is_alive():
                thread.join(timeout=5)
            try:
                server.CAPTURE_Q.get_nowait()
            except server.queue.Empty:
                pass
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


def test_stale_processing_job_is_requeued() -> None:
    with tempfile.TemporaryDirectory() as td:
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
