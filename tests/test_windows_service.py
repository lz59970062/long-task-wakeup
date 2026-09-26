"""Coordinator update/removal safety without touching Task Scheduler."""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

from long_task_callback import cli, windows_service
from long_task_callback.platforms import windows, windows_io


@unittest.skipUnless(os.name == "nt", "Windows service contracts with native private files")
class WindowsServiceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="ltc service regression ")
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name).resolve()
        self.root = self.directory / "queue"
        self.target = self.directory / "state" / "windows-test.json"
        self.config = {
            "version": 1, "run": True, "owner": "ltc-test-service",
            "command": [sys.executable, "-c", "pass"], "queue_dir": str(self.root),
            "environment": {"CODEX_HOME": str(self.directory / "profile")},
            "cwd": str(self.target.parent), "log_path": str(self.target.parent / "daemon.log"),
            "restart_sec": 1,
        }
        self.args = argparse.Namespace(name="test", enable=True, print=False, force=True, now=True)
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(mock.patch.object(windows_service, "config_path", return_value=self.target))
        self.stack.enter_context(mock.patch.object(windows_service, "configuration", return_value=self.config))
        self.stack.enter_context(mock.patch.object(cli, "configure_proxy_environment"))
        self.stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
        self.errors = self.stack.enter_context(contextlib.redirect_stderr(io.StringIO()))

    def runtime(self, root=None):
        return {
            "pid": 123456, "queue_dir": str((root or self.root).resolve()),
            "reload_protocol": cli.RELOAD_PROTOCOL_VERSION,
            "process_identity": {"boot_id": "boot", "creation_time": 100, "sid": "user"},
        }

    def test_recycled_pid_does_not_block_queue_change(self):
        old_root = self.directory / "old-queue"
        runtime = self.runtime(old_root)
        cli.write_request(self.target, dict(self.config, queue_dir=str(old_root)))
        with mock.patch.object(cli, "read_daemon_runtime", return_value=runtime), mock.patch.object(
            cli, "daemon_process_identity", return_value=dict(runtime["process_identity"], creation_time=200)
        ), mock.patch.object(windows, "scheduler_call", return_value={"registered": True}) as control:
            self.assertEqual(windows_service.install(self.args), 0, self.errors.getvalue())
        self.assertEqual(json.loads(self.target.read_text(encoding="utf-8"))["queue_dir"], str(self.root))
        self.assertEqual([call.args[0] for call in control.call_args_list], ["status", "register", "run"])

    def test_verified_other_queue_coordinator_blocks_profile_reuse(self):
        runtime = self.runtime(self.directory / "other-queue")
        with mock.patch.object(cli, "read_daemon_runtime", return_value=runtime), mock.patch.object(
            cli, "daemon_process_identity", return_value=runtime["process_identity"]
        ), mock.patch.object(windows, "scheduler_call") as control:
            self.assertEqual(windows_service.install(self.args), 2)
        control.assert_not_called()
        self.assertFalse(self.target.exists())
        self.assertIn("another queue coordinator", self.errors.getvalue())

    def test_live_supervisor_prevents_changing_its_queue_even_without_runtime(self):
        old = dict(self.config, queue_dir=str(self.directory / "old-queue"))
        cli.write_request(self.target, old)
        with mock.patch.object(windows_io, "probe_existing_lock", return_value=True), mock.patch.object(
            cli, "read_daemon_runtime", return_value=None
        ), mock.patch.object(windows, "scheduler_call") as control:
            self.assertEqual(windows_service.install(self.args), 2)
        control.assert_not_called()
        self.assertEqual(json.loads(self.target.read_text(encoding="utf-8")), old)

    def test_reload_also_ensures_supervisor_after_an_orphan_daemon(self):
        runtime = self.runtime()
        cli.write_request(self.target, self.config)
        with mock.patch.object(cli, "read_daemon_runtime", return_value=runtime), mock.patch.object(
            cli, "acquire_owner_lock", return_value=None
        ), mock.patch.object(cli, "send_standalone_reload", return_value=True) as reload, mock.patch.object(
            windows, "scheduler_call", return_value={"registered": True}
        ) as control, mock.patch.object(os, "kill") as signal:
            self.assertEqual(windows_service.install(self.args), 0, self.errors.getvalue())
        reload.assert_called_once_with(runtime["pid"], expected_queue=self.root)
        self.assertEqual([call.args[0] for call in control.call_args_list], ["status", "register", "run"])
        signal.assert_not_called()

    def test_uninstall_ignores_stale_runtime_when_queue_has_no_owner(self):
        cli.write_request(self.target, self.config)
        with mock.patch.object(cli, "read_daemon_runtime", return_value=self.runtime()), mock.patch.object(
            cli, "daemon_process_identity", return_value=None
        ), mock.patch.object(windows, "scheduler_call", return_value={}) as control:
            self.assertEqual(windows_service.uninstall(self.args), 0, self.errors.getvalue())
        control.assert_called_once_with("delete", {"owner": self.config["owner"]})
        self.assertFalse(json.loads(self.target.read_text(encoding="utf-8"))["run"])
        self.assertFalse((self.root / "daemon-stop.json").exists())

    def test_uninstall_refuses_to_modify_unverified_live_coordinator(self):
        cli.write_request(self.target, self.config)
        runtime = self.runtime()
        with mock.patch.object(cli, "read_daemon_runtime", return_value=runtime), mock.patch.object(
            cli, "daemon_process_identity", return_value=dict(runtime["process_identity"], creation_time=200)
        ), mock.patch.object(windows_io, "probe_existing_lock", return_value=True), mock.patch.object(
            windows, "scheduler_call"
        ) as control:
            self.assertEqual(windows_service.uninstall(self.args), 2)
        control.assert_not_called()
        self.assertTrue(json.loads(self.target.read_text(encoding="utf-8"))["run"])
        self.assertFalse((self.root / "daemon-stop.json").exists())

    def test_uninstall_live_owner_writes_identity_bound_stop_without_killing(self):
        cli.write_request(self.target, self.config)
        runtime = self.runtime()
        with mock.patch.object(cli, "read_daemon_runtime", return_value=runtime), mock.patch.object(
            cli, "daemon_process_identity", return_value=runtime["process_identity"]
        ), mock.patch.object(windows_io, "probe_existing_lock", return_value=True), mock.patch.object(
            windows, "scheduler_call", return_value={}
        ) as control, mock.patch.object(os, "kill") as signal:
            self.assertEqual(windows_service.uninstall(self.args), 0, self.errors.getvalue())
        self.assertEqual(json.loads((self.root / "daemon-stop.json").read_text(encoding="utf-8")),
                         {"pid": runtime["pid"], "process_identity": runtime["process_identity"]})
        control.assert_called_once_with("delete", {"owner": self.config["owner"]})
        signal.assert_not_called()

    def test_replacement_supervisor_waits_for_existing_daemon_to_drain(self):
        cli.write_request(self.target, self.config)
        child = mock.Mock()

        def finish():
            cli.write_request(self.target, dict(self.config, run=False))
            return 75

        child.wait.side_effect = finish
        with mock.patch.object(windows_io, "probe_existing_lock", side_effect=[True, False]) as probe, mock.patch.object(
            windows_service.time, "sleep"
        ) as sleep, mock.patch.object(windows_service.subprocess, "Popen", return_value=child) as spawn:
            self.assertEqual(windows_service.run_coordinator(self.target), 0)
        self.assertEqual(probe.call_count, 2)
        sleep.assert_called_once_with(0.1)
        spawn.assert_called_once()
        self.assertFalse(windows_io.probe_existing_lock(self.target.with_suffix(".supervisor.lock")))

    def test_draining_finds_orphan_delivery_after_ack_moved_callback_to_done(self):
        cli.ensure_daemon_dirs(self.root)
        request_id = "acknowledged-orphan"
        cli.write_request(cli.request_path(self.root, "done", request_id), {
            "version": 1, "id": request_id, "cwd": str(self.directory), "prompt": "fixture",
            "agent": "codex", "target": {"kind": "session", "value": "fixture-session"},
        })
        lock = cli.acquire_owner_lock(self.root, cli.delivery_lock_id(request_id), blocking=False)
        self.assertIsNotNone(lock)
        try:
            # The parent coordinator died after ACK, so the replacement has no
            # Popen bookkeeping and the callback is no longer in running/.
            with mock.patch.dict(cli._BACKGROUND_RESUMES, {}, clear=True):
                self.assertTrue(cli.delivery_lock_is_held(self.root, request_id))
                self.assertTrue(cli.daemon_has_live_delivery_workers(self.root))
        finally:
            cli.release_owner_lock(lock, remove=False)
        with mock.patch.dict(cli._BACKGROUND_RESUMES, {}, clear=True):
            self.assertFalse(cli.daemon_has_live_delivery_workers(self.root))

    def test_draining_never_launches_new_tasks_or_deliveries(self):
        for operation, expected in (("daemon-stop.json", 0), ("daemon-reload.json", 75)):
            with self.subTest(operation=operation), mock.patch.object(cli, "load_service_proxy_environment"), mock.patch.object(
                cli, "write_daemon_runtime"
            ), mock.patch.object(cli, "clear_daemon_runtime"), mock.patch.object(
                cli, "consume_daemon_reload_request", side_effect=lambda root, filename="daemon-reload.json": filename == operation
            ), mock.patch.object(cli, "reap_background_resumes"), mock.patch.object(
                cli, "daemon_has_live_delivery_workers", side_effect=[True, False]
            ) as alive, mock.patch.object(cli, "recover_running"), mock.patch.object(
                cli, "recover_managed_tasks"
            ) as tasks, mock.patch.object(cli, "process_one") as deliveries, mock.patch.object(
                cli.time, "sleep"
            ) as sleep, mock.patch.object(cli.os, "execv") as execute:
                args = argparse.Namespace(queue_dir=str(self.root), once=False, interval=0.01, max_items=None)
                self.assertEqual(cli.daemon(args), expected)
            self.assertEqual(alive.call_count, 2)
            tasks.assert_not_called()
            deliveries.assert_not_called()
            execute.assert_not_called()
            sleep.assert_called_once_with(0.01)


if __name__ == "__main__":
    unittest.main()
