"""Persisted PIDs are hints, not authority to signal a process after restart."""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import signal
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from long_task_callback import cli


IDENTITY = {"boot_id": "current-boot", "machine_id": "current-machine",
            "pid_namespace": "pid:[10001]", "start_ticks": 50000}


class StandaloneDaemonIdentityTests(unittest.TestCase):
    def setUp(self) -> None:
        platform = mock.patch.object(sys, "platform", "linux")
        platform.start()
        self.addCleanup(platform.stop)
        if os.name == "nt":
            # This models Linux startup/reload with fake processes, while the
            # ownership lease is still a real native Windows exclusive handle.
            linux_os = types.SimpleNamespace(**vars(os))
            linux_os.name = "posix"
            for patcher in (
                mock.patch.object(cli, "os", linux_os),
                mock.patch.object(signal, "SIGHUP", 1, create=True),
                mock.patch.object(cli, "fsync_directory", side_effect=cli.windows_io.fsync_directory),
                mock.patch.object(cli, "acquire_owner_lock", side_effect=lambda root, ident, blocking:
                    cli.windows_io.acquire_path_lock(cli.owner_lock_path(root, ident), blocking=blocking)),
            ):
                patcher.start()
                self.addCleanup(patcher.stop)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.state = self.directory / "state"
        self.state.mkdir()
        self.queue = self.directory / "queue"
        self.runtime = self.state / "daemon-runtime.json"
        self.args = argparse.Namespace(queue_dir=str(self.queue))
        for patcher in (
            mock.patch.object(cli, "daemon_state_dir", return_value=self.state),
            mock.patch.object(cli, "daemon_runtime_path", return_value=self.runtime),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def seed_runtime(self, *, identity=IDENTITY, queue=None):
        pid = 81234
        (self.state / "daemon.pid").write_text(str(pid))
        record = {"pid": pid, "reload_protocol": cli.RELOAD_PROTOCOL_VERSION,
                  "queue_dir": str(queue or self.queue)}
        if identity is not None:
            record["process_identity"] = dict(identity)
        self.runtime.write_text(json.dumps(record))
        return pid

    def hold_coordinator_lock(self):
        lock = cli.acquire_owner_lock(self.queue, "daemon-singleton", blocking=True)
        self.assertIsNotNone(lock)
        self.addCleanup(cli.release_owner_lock, lock, remove=False)

    def start_with_spies(self, *, identity=IDENTITY, callbacks=False):
        process = mock.Mock(pid=91234)
        process.poll.return_value = None
        with mock.patch.object(cli, "daemon_process_identity", return_value=identity), mock.patch.object(
            cli, "has_running_callbacks", return_value=callbacks
        ), mock.patch.object(cli, "daemon_environment", return_value={}), mock.patch.object(
            cli, "daemon_command", return_value=[sys.executable, "-c", "pass"]
        ), mock.patch.object(cli.subprocess, "Popen", return_value=process) as popen, mock.patch.object(
            cli.linux_platform, "signal_if_identity_matches", return_value=True
        ) as reload, mock.patch.object(cli.os, "kill") as kill, mock.patch.object(
            cli.time, "sleep"
        ), contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            status = cli.start_standalone_daemon(self.args)
        self.assertEqual(status, 0)
        self.assertFalse(any(call.args[1] != 0 for call in kill.call_args_list))
        return popen, reload, process

    def test_reused_live_pid_without_queue_owner_starts_replacement_without_signaling(self):
        self.seed_runtime(identity=dict(IDENTITY, start_ticks=40000))
        popen, reload, process = self.start_with_spies()
        reload.assert_not_called()
        popen.assert_called_once()
        self.assertEqual(int((self.state / "daemon.pid").read_text()), process.pid)

    def test_legacy_runtime_with_live_queue_owner_neither_signals_nor_duplicates(self):
        self.seed_runtime(identity=None)
        self.hold_coordinator_lock()
        popen, reload, _ = self.start_with_spies()
        reload.assert_not_called()
        popen.assert_not_called()

    def test_verified_current_queue_owner_can_reload_without_second_coordinator(self):
        pid = self.seed_runtime()
        self.hold_coordinator_lock()
        popen, reload, _ = self.start_with_spies()
        reload.assert_called_once_with(pid, IDENTITY, signal.SIGHUP)
        popen.assert_not_called()

    def test_verified_runtime_from_different_queue_does_not_authorize_reload(self):
        self.seed_runtime(queue=self.directory / "different-queue")
        self.hold_coordinator_lock()
        popen, reload, _ = self.start_with_spies()
        reload.assert_not_called()
        popen.assert_not_called()

    def test_active_callback_defers_verified_reload(self):
        self.seed_runtime()
        self.hold_coordinator_lock()
        popen, reload, _ = self.start_with_spies(callbacks=True)
        reload.assert_not_called()
        popen.assert_not_called()

    def test_fresh_launch_is_detached_and_records_only_new_pid(self):
        popen, reload, process = self.start_with_spies()
        reload.assert_not_called()
        popen.assert_called_once()
        self.assertTrue(popen.call_args.kwargs["start_new_session"])
        self.assertEqual(popen.call_args.kwargs["stdin"], subprocess.DEVNULL)
        self.assertEqual(int((self.state / "daemon.pid").read_text()), process.pid)

    def test_reload_capability_requires_matching_process_fingerprint_and_queue(self):
        pid = self.seed_runtime()
        for identity in (None, dict(IDENTITY, start_ticks=50001),
                         dict(IDENTITY, boot_id="previous-boot"),
                         dict(IDENTITY, pid_namespace="pid:[previous-container]")):
            with self.subTest(identity=identity), mock.patch.object(cli, "daemon_process_identity", return_value=identity):
                self.assertFalse(cli.daemon_supports_hot_reload(pid, expected_queue=self.queue))
        with mock.patch.object(cli, "daemon_process_identity", return_value=IDENTITY):
            self.assertTrue(cli.daemon_supports_hot_reload(pid, expected_queue=self.queue))
            self.assertFalse(cli.daemon_supports_hot_reload(pid + 1, expected_queue=self.queue))
            self.assertFalse(cli.daemon_supports_hot_reload(pid, expected_queue=self.directory / "another"))

    def test_pinned_process_identity_is_rechecked_before_pidfd_signal_and_fd_is_closed(self):
        linux = cli.linux_platform
        for current, expected in ((IDENTITY, True), (dict(IDENTITY, start_ticks=50001), False)):
            with self.subTest(current=current), mock.patch.object(linux.sys, "platform", "linux"), mock.patch.object(
                linux.os, "pidfd_open", return_value=99, create=True
            ) as pin, mock.patch.object(linux.os, "close") as close, mock.patch.object(
                linux.os, "kill"
            ) as kill, mock.patch.object(linux.signal, "pidfd_send_signal", create=True) as send:
                def identity_after_pin(pid):
                    pin.assert_called_once_with(pid)
                    send.assert_not_called()
                    return current

                with mock.patch.object(linux, "process_identity", side_effect=identity_after_pin):
                    self.assertEqual(linux.signal_if_identity_matches(81234, IDENTITY, signal.SIGHUP), expected)
                if expected:
                    send.assert_called_once_with(99, signal.SIGHUP)
                else:
                    send.assert_not_called()
                close.assert_called_once_with(99)
                kill.assert_not_called()


if __name__ == "__main__":
    unittest.main()
