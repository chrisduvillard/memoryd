from __future__ import annotations

import contextlib
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
    real_replace = core.os.replace

    def synchronized_replace(source: Path, target: Path) -> None:
        ready.wait(timeout=10)
        if winner:
            core.time.sleep(0.05)
            real_replace(source, target)
        else:
            raise PermissionError("simulated Windows publication loser")

    core.os.replace = synchronized_replace
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
        for unsafe in ("../escape", "/absolute/path", r"C:\escape", r"a\..\escape"):
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
            for process in processes:
                process.join(timeout=15)
                if process.is_alive():
                    process.terminate()
                    process.join()
                    raise AssertionError("archive publication process timed out")
            assert [process.exitcode for process in processes] == [0, 0]

            entries = [json.loads(line) for line in
                       (core.CFG.archive / "manifest.jsonl").read_text().splitlines()]
            assert {entry["ingest_job_id"] for entry in entries} == {
                "j1", "j2", "0", "1"}
            object_files = [path for path in
                            (core.CFG.archive / "objects" / "sha256").rglob("*")
                            if path.is_file()]
            assert len(object_files) == 2

            with patch.object(core.os, "replace",
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
