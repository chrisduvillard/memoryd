from __future__ import annotations

import contextlib
import hashlib
import io
import json
import multiprocessing
import os
import tempfile
import threading
from pathlib import Path
from unittest.mock import patch

from memoryd import core
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
    test_mixed_transcript_line_preserves_text_and_tools()
    test_fonds_paths_cannot_escape_archive()
    test_archive_records_each_occurrence()
    print("7 passed, 0 failed")
