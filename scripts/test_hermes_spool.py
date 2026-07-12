#!/usr/bin/env python3
"""DB-free crash-durability tests for the Hermes memoryd provider."""
from __future__ import annotations

import importlib.util
import http.client
import io
import json
import multiprocessing
import os
import stat
import sys
import tempfile
import threading
import types
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts" / "_stubs"))

SPEC = importlib.util.spec_from_file_location(
    "hermes_memoryd_plugin", REPO / "hermes_plugin" / "memoryd" / "__init__.py")
plugin = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(plugin)

CLI_SPEC = importlib.util.spec_from_file_location(
    "hermes_memoryd_cli", REPO / "hermes_plugin" / "memoryd" / "cli.py")
cli = importlib.util.module_from_spec(CLI_SPEC)
assert CLI_SPEC.loader is not None
CLI_SPEC.loader.exec_module(cli)


def _claim_in_spawned_process(home: str, start, output) -> None:
    spool = plugin.DurableSpool(Path(home))
    start.wait()
    claimed = spool.claim_oldest()
    output.put(claimed[1]["job_id"] if claimed else None)


class MutableClock:
    def __init__(self, value: float = 1_000.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value


class DurableSpoolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.clock = MutableClock()
        self.spool = plugin.DurableSpool(self.home, clock=self.clock)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _persist(self, endpoint: str = "/capture-events", job_id: str | None = None):
        body = {"session_id": "s1", "events": []}
        return self.spool.persist(endpoint, body, job_id=job_id)

    def test_job_is_canonical_and_persisted_before_send(self) -> None:
        job_id = self._persist(job_id="job-persisted")
        jobs = self.spool.list_jobs("incoming")
        self.assertEqual(len(jobs), 1)
        job = jobs[0][1]
        self.assertEqual(job["schema_version"], 1)
        self.assertEqual(job["job_id"], job_id)
        self.assertEqual(job["body"]["request_id"], job_id)
        self.assertEqual(job["attempts"], 0)
        self.assertEqual(job["next_attempt_at"], 1_000.0)
        canonical = json.dumps(job["body"], sort_keys=True, separators=(",", ":")).encode()
        self.assertEqual(job["body_sha256"], plugin.hashlib.sha256(canonical).hexdigest())

    def test_first_use_fsyncs_each_new_directory_parent(self) -> None:
        nested = self.home / "new-parent" / "memoryd" / "incoming"
        with mock.patch.object(plugin, "_fsync_dir") as fsync_dir:
            plugin._private_dir(nested)
        self.assertEqual(
            [call.args[0] for call in fsync_dir.call_args_list],
            [self.home, self.home / "new-parent", self.home / "new-parent" / "memoryd"])

    def test_directory_fsync_failure_never_reports_publication_success(self) -> None:
        crash_home = self.home / "new-profile"
        spool = plugin.DurableSpool(crash_home, clock=self.clock)
        with mock.patch.object(plugin, "_fsync_dir", side_effect=OSError("fsync failed")):
            with self.assertRaises(OSError):
                spool.persist("/miss", {"session_id": "s"}, job_id="never-success")
        self.assertFalse(list((crash_home / "spool" / "memoryd" /
                              "incoming").glob("*.json")))

    def test_retry_refsyncs_parent_of_directory_left_by_failed_fsync(self) -> None:
        spool = plugin.DurableSpool(self.home, clock=self.clock)
        with mock.patch.object(plugin, "_fsync_dir", side_effect=OSError("uncertain")):
            with self.assertRaises(OSError):
                spool._ensure()
        with mock.patch.object(plugin, "_fsync_dir") as fsync_dir:
            spool._ensure()
        self.assertIn(mock.call(self.home), fsync_dir.call_args_list)

    def test_job_publish_fsync_failure_preserves_collision_evidence(self) -> None:
        self.spool._ensure()
        incoming = self.spool._dir("incoming")

        def fail_publication(path):
            if path == incoming:
                raise OSError("fsync failed")

        with mock.patch.object(plugin, "_fsync_dir", side_effect=fail_publication):
            with self.assertRaises(OSError):
                self._persist(job_id="uncertain-publish")
        paths = list(self.spool._dir("incoming").glob("*.json"))
        self.assertEqual(len(paths), 1)
        self.assertEqual(json.loads(paths[0].read_text())["job_id"], "uncertain-publish")

    def test_publication_order_does_not_depend_on_random_job_id(self) -> None:
        self.spool.persist("/capture-events", {"session_id": "s"}, job_id="z-capture")
        self.spool.persist("/extract", {"session_id": "s"}, job_id="a-extract")
        endpoints = [job["endpoint"] for _, job in self.spool.list_jobs("incoming")]
        self.assertEqual(endpoints, ["/capture-events", "/extract"])

    def test_restart_recovers_incoming_and_stale_processing(self) -> None:
        self._persist(job_id="job-restart")
        path, _ = self.spool.claim_oldest()
        self.clock.value += plugin.STALE_PROCESSING_SECONDS + 1
        restarted = plugin.DurableSpool(self.home, clock=self.clock)
        self.assertEqual(restarted.recover_stale(), 1)
        self.assertFalse(path.exists())
        claimed = restarted.claim_oldest()
        self.assertIsNotNone(claimed)
        self.assertEqual(claimed[1]["job_id"], "job-restart")

    def test_retry_backoff_persists_and_caps_at_300_seconds(self) -> None:
        self._persist(job_id="job-backoff")
        for attempts in range(1, 12):
            path, job = self.spool.claim_oldest()
            self.spool.retry(path, job, "network down")
            persisted = self.spool.list_jobs("incoming")[0][1]
            expected = min(2 ** (attempts - 1), 300)
            self.assertEqual(persisted["attempts"], attempts)
            self.assertEqual(persisted["next_attempt_at"], self.clock.value + expected)
            self.assertEqual(persisted["last_error"], "network down")
            self.clock.value = persisted["next_attempt_at"]

    def test_permanent_failure_dead_letters_with_reason(self) -> None:
        self._persist(job_id="job-bad-request")
        path, job = self.spool.claim_oldest()
        self.spool.dead_letter(path, job, "HTTP 400: invalid session")
        self.assertEqual(self.spool.counts(), {"incoming": 0, "processing": 0,
                                               "dead_letter": 1})
        dead = self.spool.list_jobs("dead_letter")[0][1]
        self.assertEqual(dead["attempts"], 1)
        self.assertEqual(dead["dead_letter_reason"], "HTTP 400: invalid session")
        self.assertIn("dead_lettered_at", dead)

    def test_stale_recovery_finishes_interrupted_dead_letter_move(self) -> None:
        self._persist(job_id="job-dead-crash")
        path, job = self.spool.claim_oldest()
        job["attempts"] = 1
        job["last_error"] = "HTTP 400"
        job["dead_letter_reason"] = "HTTP 400"
        job["dead_lettered_at"] = self.clock.value
        job.pop("claimed_at", None)
        plugin._atomic_json(path, job, replace=True)
        os.utime(path, (self.clock.value, self.clock.value))
        self.clock.value += plugin.STALE_PROCESSING_SECONDS + 1
        self.assertEqual(self.spool.recover_stale(), 1)
        self.assertEqual(self.spool.counts(), {"incoming": 0, "processing": 0,
                                               "dead_letter": 1})

    def test_corrupt_processing_is_quarantined_instead_of_blocking_queue(self) -> None:
        self.spool._ensure()
        bad = self.spool._dir("processing") / "000-corrupt.json"
        bad.write_text("not JSON")
        self._persist(job_id="valid-after-corrupt")
        self.assertEqual(self.spool.recover_stale(), 1)
        self.assertEqual(self.spool.counts()["processing"], 0)
        self.assertEqual(self.spool.counts()["dead_letter"], 1)
        self.assertIn("invalid job", self.spool.fault())
        claimed = self.spool.claim_oldest()
        self.assertEqual(claimed[1]["job_id"], "valid-after-corrupt")

    def test_concurrent_claim_has_one_winner(self) -> None:
        self._persist(job_id="job-race")
        winners = []
        barrier = threading.Barrier(3)

        def claim() -> None:
            contender = plugin.DurableSpool(self.home, clock=self.clock)
            barrier.wait()
            result = contender.claim_oldest()
            if result:
                winners.append(result[1]["job_id"])

        threads = [threading.Thread(target=claim) for _ in range(2)]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join()
        self.assertEqual(winners, ["job-race"])

    def test_cross_process_claim_has_one_winner(self) -> None:
        self._persist(job_id="job-process-race")
        context = multiprocessing.get_context("spawn")
        start = context.Event()
        output = context.Queue()
        processes = [context.Process(target=_claim_in_spawned_process,
                                     args=(str(self.home), start, output))
                     for _ in range(2)]
        for process in processes:
            process.start()
        start.set()
        results = [output.get(timeout=10) for _ in processes]
        for process in processes:
            process.join(timeout=10)
            self.assertEqual(process.exitcode, 0)
        self.assertEqual(results.count("job-process-race"), 1)
        self.assertEqual(results.count(None), 1)

    def test_delete_only_after_confirmed_2xx_json(self) -> None:
        self._persist(job_id="job-response")
        provider = plugin.MemorydProvider()
        provider._spool_store = self.spool
        with mock.patch.object(provider, "_send_mutation", side_effect=[
                plugin.HttpResult("retry", None, "invalid JSON response"),
                plugin.HttpResult("success", {"ok": True}, "")]):
            self.assertTrue(provider._process_one())
            self.assertEqual(self.spool.counts()["incoming"], 1)
            self.clock.value = self.spool.list_jobs("incoming")[0][1]["next_attempt_at"]
            self.assertTrue(provider._process_one())
            self.assertEqual(self.spool.counts(), {"incoming": 0, "processing": 0,
                                                   "dead_letter": 0})

    def test_collision_preserves_existing_evidence(self) -> None:
        self._persist(job_id="same-id")
        original_path, original = self.spool.list_jobs("incoming")[0]
        with self.assertRaises(plugin.JobCollision):
            self.spool.persist("/miss", {"session_id": "s2"}, job_id="same-id")
        self.assertEqual(json.loads(original_path.read_text()), original)
        self.assertEqual(self.spool.counts()["incoming"], 1)

    def test_invalid_job_is_quarantined_without_killing_worker(self) -> None:
        self.spool._ensure()
        path = self.spool._dir("incoming") / "000-invalid.json"
        plugin._atomic_json(path, {}, replace=False)
        self.assertIsNone(self.spool.claim_oldest())
        self.assertEqual(self.spool.counts()["dead_letter"], 1)
        dead = self.spool.list_jobs("dead_letter")[0][1]
        self.assertIn("invalid job", dead["dead_letter_reason"])

    def test_body_digest_and_request_identity_are_verified_before_send(self) -> None:
        self._persist(job_id="job-integrity")
        path, job = self.spool.list_jobs("incoming")[0]
        job["body"]["request_id"] = "changed"
        plugin._atomic_json(path, job, replace=True)
        self.assertIsNone(self.spool.claim_oldest())
        dead = self.spool.list_jobs("dead_letter")[0][1]
        self.assertIn("request_id", dead["dead_letter_reason"])

    @unittest.skipIf(os.name == "nt", "POSIX permissions")
    def test_posix_permissions_are_private(self) -> None:
        self._persist()
        root = self.home / "spool" / "memoryd"
        for directory in (root, root / "incoming", root / "processing",
                          root / "dead-letter"):
            self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o700)
        for path, _ in self.spool.list_jobs("incoming"):
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE((root / "spool.lock").stat().st_mode), 0o600)


class ProviderDurabilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        (self.home / "memoryd.json").write_text(json.dumps({"url": "http://127.0.0.1:1"}))

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _provider(self, context: str = "primary"):
        provider = plugin.MemorydProvider()
        with mock.patch.object(plugin.MemorydProvider, "_start_worker"):
            provider.initialize("session", hermes_home=str(self.home), platform="cli",
                                agent_context=context)
        return provider

    def test_primary_hooks_persist_synchronously_and_boundary_order_is_preserved(self) -> None:
        provider = self._provider()
        before = len(provider._spool_store.list_jobs("incoming"))
        provider.sync_turn("u", "a")
        self.assertEqual(len(provider._spool_store.list_jobs("incoming")), before + 1)
        provider.on_session_end([{"role": "user", "content": "bye"}])
        endpoints = [job["endpoint"] for _, job in provider._spool_store.list_jobs("incoming")]
        self.assertEqual(endpoints[-2:], ["/capture-events", "/extract"])

    def test_boundary_does_not_publish_extract_if_capture_was_not_durable(self) -> None:
        provider = self._provider()
        with mock.patch.object(provider._spool_store, "persist",
                               side_effect=OSError("capture fsync failed")) as persist:
            provider.on_session_end([])
        self.assertEqual(persist.call_count, 1)
        self.assertEqual(persist.call_args.args[0], "/capture-events")
        self.assertTrue(provider.durability_fault)

    def test_miss_tool_returns_queued_semantics(self) -> None:
        provider = self._provider()
        result = json.loads(provider.handle_tool_call(
            "memoryd_report_miss", {"detail": "forgot deployment"}))
        self.assertTrue(result["ok"])
        self.assertTrue(result["queued"])
        self.assertEqual(provider._spool_store.list_jobs("incoming")[-1][1]["endpoint"], "/miss")

    def test_nonprimary_context_creates_no_queue_state(self) -> None:
        provider = self._provider("subagent")
        provider.sync_turn("u", "a")
        provider.on_session_end([])
        provider.handle_tool_call("memoryd_report_miss", {"detail": "x"})
        self.assertFalse((self.home / "spool" / "memoryd").exists())

    def test_disk_failure_is_fail_open_visible_and_not_reported_as_success(self) -> None:
        provider = self._provider()
        with mock.patch.object(provider._spool_store, "persist", side_effect=OSError("disk full")):
            provider.sync_turn("u", "a")
            result = json.loads(provider.handle_tool_call(
                "memoryd_report_miss", {"detail": "x"}))
        self.assertFalse(result["ok"])
        self.assertFalse(result["queued"])
        self.assertTrue(provider.durability_fault)
        marker = provider.prefetch("next")
        self.assertIn("capture durability fault", marker)
        with mock.patch.object(provider, "_recall", return_value=""):
            self.assertEqual(provider.prefetch("again"), "")

    def test_corrupt_stale_processing_is_fail_open_on_restart(self) -> None:
        processing = self.home / "spool" / "memoryd" / "processing"
        processing.mkdir(parents=True)
        (processing / "000-bad.json").write_text("not JSON")
        provider = plugin.MemorydProvider()
        with mock.patch.object(plugin.MemorydProvider, "_start_worker"):
            provider.initialize("session", hermes_home=str(self.home),
                                platform="cli", agent_context="primary")
        self.assertIn("capture durability fault", provider.prefetch("next"))

    def test_http_classification_retries_only_expected_failures(self) -> None:
        self.assertTrue(plugin._retryable_status(408))
        self.assertTrue(plugin._retryable_status(429))
        self.assertTrue(plugin._retryable_status(503))
        self.assertFalse(plugin._retryable_status(400))
        self.assertFalse(plugin._retryable_status(409))

    def test_malformed_url_remains_fail_open_for_recall(self) -> None:
        provider = self._provider()
        provider._url = "not-a-url"
        self.assertIn("unavailable", provider.prefetch("anything"))

    def test_restart_surfaces_existing_dead_letter_once(self) -> None:
        spool = plugin.DurableSpool(self.home, clock=MutableClock())
        spool.persist("/miss", {"session_id": "s"}, job_id="dead-on-restart")
        path, job = spool.claim_oldest()
        spool.dead_letter(path, job, "HTTP 400")
        provider = self._provider()
        self.assertIn("capture durability fault", provider.prefetch("next"))

    def test_shutdown_during_response_retains_processing_evidence(self) -> None:
        provider = self._provider()
        provider._spool_store.persist("/miss", {"session_id": "s"}, job_id="shutdown")

        def stop_during_send(job):
            provider._stop.set()
            return plugin.HttpResult("success", {"ok": True}, "")

        with mock.patch.object(provider, "_send_mutation", side_effect=stop_during_send):
            provider._process_one()
        self.assertEqual(provider._spool_store.counts()["processing"], 1)

    def test_bounded_shutdown_cannot_mutate_after_slow_response_returns(self) -> None:
        provider = self._provider()
        provider._spool_store.persist("/miss", {"session_id": "s"}, job_id="slow")
        entered = threading.Event()
        release = threading.Event()

        def slow_send(job):
            entered.set()
            release.wait(10)
            return plugin.HttpResult("success", {"ok": True}, "")

        with mock.patch.object(provider, "_send_mutation", side_effect=slow_send):
            worker = threading.Thread(target=provider._process_one, daemon=True)
            provider._worker = worker
            worker.start()
            self.assertTrue(entered.wait(2))
            provider.shutdown()
            self.assertEqual(provider._spool_store.counts()["processing"], 1)
            release.set()
            worker.join(2)
            self.assertFalse(worker.is_alive())
            self.assertEqual(provider._spool_store.counts()["processing"], 1)

    def test_empty_2xx_is_not_a_confirmed_json_response(self) -> None:
        provider = self._provider()
        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.status = 204
        response.read.return_value = b""
        with mock.patch.object(plugin.urllib.request, "urlopen", return_value=response):
            result = provider._request_json("/miss", {"request_id": "x"}, 1.0)
        self.assertEqual(result.kind, "retry")
        self.assertIn("JSON", result.error)

    def test_truncated_response_is_classified_for_durable_retry(self) -> None:
        provider = self._provider()
        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.status = 200
        response.read.side_effect = http.client.IncompleteRead(b'{"ok":', 10)
        with mock.patch.object(plugin.urllib.request, "urlopen", return_value=response):
            result = provider._request_json("/miss", {"request_id": "x"}, 1.0)
        self.assertEqual(result.kind, "retry")
        self.assertIn("network", result.error)


class CliStatusTests(unittest.TestCase):
    def test_status_snapshot_counts_and_fault_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            spool = plugin.DurableSpool(home, clock=MutableClock())
            spool.persist("/miss", {"session_id": "s"}, job_id="incoming")
            spool.persist("/miss", {"session_id": "s"}, job_id="dead")
            path, job = spool.claim_oldest()
            spool.dead_letter(path, job, "HTTP 400")
            spool.set_fault("disk warning")
            status = cli._spool_status(home)
            self.assertEqual(status["incoming"], 1)
            self.assertEqual(status["processing"], 0)
            self.assertEqual(status["dead_letter"], 1)
            self.assertEqual(status["fault"], "disk warning")
            self.assertFalse(status["healthy"])

    def test_non_object_state_is_reported_unhealthy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "spool" / "memoryd"
            root.mkdir(parents=True)
            (root / "state.json").write_text("[]")
            status = cli._spool_status(Path(tmp))
            self.assertEqual(status["fault"], "unreadable spool state")
            self.assertFalse(status["healthy"])

    def test_manual_miss_is_durably_queued(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            args = types.SimpleNamespace(memoryd_command="miss",
                                         detail=["forgot", "deploy"])
            with mock.patch.object(cli, "_home", return_value=Path(tmp)), \
                    mock.patch.object(cli.urllib.request, "urlopen") as urlopen, \
                    mock.patch("sys.stdout", new=io.StringIO()) as output:
                cli.memoryd_command(args)
            urlopen.assert_not_called()
            spool = plugin.DurableSpool(Path(tmp))
            job = spool.list_jobs("incoming")[0][1]
            self.assertEqual(job["endpoint"], "/miss")
            self.assertEqual(job["body"]["detail"]["note"], "forgot deploy")
            self.assertIn("queued", output.getvalue())


if __name__ == "__main__":
    unittest.main(verbosity=2)
