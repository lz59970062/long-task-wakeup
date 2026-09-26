"""macOS contracts: one-shot ownership, cautious recovery and safe activation."""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
from pathlib import Path
import plistlib
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from long_task_callback import cli, diagnostics, launchd_service
from long_task_callback.platforms import LaunchdBackend, LaunchError, OwnerState
from long_task_callback.platforms import macos, screen


class MacOSContracts(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="ltc mac ")
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name).resolve()
        self.queue = self.directory / "queue"
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(mock.patch.object(sys, "platform", "darwin"))
        self.stack.enter_context(mock.patch.object(cli, "current_machine_id", return_value="host"))
        self.stack.enter_context(mock.patch.object(cli, "current_boot_id", return_value="boot"))
        self.backend = LaunchdBackend()

    def arguments(self, **updates):
        values = dict(name="ltc-test", queue_dir=str(self.queue), interval=0.1, retries=0,
                      retry_delay=1.0, retry_backoff=2.0, resume_timeout=30.0, restart_sec=2.5,
                      exec_start=None, codex_bin=None, claude_bin=None, path=None,
                      force=True, enable=True, now=True, print=False, backend="auto", service="auto")
        values.update(updates)
        return argparse.Namespace(**values)

    def test_native_selection_and_explicit_wrong_platform_fail_closed(self):
        with mock.patch.object(LaunchdBackend, "available", return_value=True):
            self.assertEqual(cli.select_execution_backend(self.arguments()), "launchd")
            self.assertEqual(cli.select_daemon_service(self.arguments()), "launchd")
        with mock.patch.object(LaunchdBackend, "available", return_value=False), mock.patch.object(
            cli, "screen_binary", return_value="/usr/bin/screen"
        ):
            self.assertEqual(cli.select_execution_backend(self.arguments()), "screen")
            self.assertEqual(cli.select_daemon_service(self.arguments()), "standalone")
            with self.assertRaises(ValueError):
                cli.select_execution_backend(self.arguments(backend="launchd"))
        with self.assertRaises(ValueError):
            cli.select_execution_backend(self.arguments(backend="systemd"))
        self.assertTrue(diagnostics.environment_profile()["supported"])

    def test_owner_names_bind_queue_task_and_attempt(self):
        first = self.backend.owner_name("abcd1234", 1, self.queue)
        self.assertEqual(first, self.backend.owner_name("abcd1234", 1, self.queue))
        self.assertEqual(len({first, self.backend.owner_name("abcd1234", 2, self.queue),
                              self.backend.owner_name("abcd1234", 1, self.directory / "other")}), 3)

    def test_task_plist_never_auto_restarts_or_registers_at_login(self):
        owner = self.backend.owner_name("abcd1234", 1, self.queue)
        argv = [sys.executable, "/runtime/entry.py", "_task-worker", "--task-file",
                str(self.directory / "literal $value & 中文/task.json"), "--attempt", "1"]
        log = self.directory / "attempt log"
        with mock.patch.object(macos, "launchctl", return_value=subprocess.CompletedProcess([], 0)) as control:
            self.backend.launch(owner, argv, self.directory, log)
        path = self.directory / f"{owner}.plist"
        data = plistlib.loads(path.read_bytes())
        self.assertEqual(data["ProgramArguments"], argv)
        self.assertFalse(data["KeepAlive"])
        self.assertTrue(data["RunAtLoad"])
        self.assertFalse(data["AbandonProcessGroup"])
        self.assertEqual(data["EnvironmentVariables"], {macos.OWNER_ENV: owner, "PYTHONUNBUFFERED": "1"})
        self.assertEqual(data["StandardOutPath"], str(log))
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        control.assert_called_once_with(["bootstrap", macos.gui_domain(), str(path)])

    def test_failed_submission_is_ambiguous_except_before_exec(self):
        for error, uncertain in ((subprocess.TimeoutExpired("launchctl", 10), True),
                                 (FileNotFoundError("launchctl"), False)):
            with self.subTest(error=error), mock.patch.object(macos, "launchctl", side_effect=error), self.assertRaises(LaunchError) as caught:
                self.backend.launch("ltc.test", [sys.executable], self.directory, self.directory / "log")
            self.assertEqual(caught.exception.uncertain, uncertain)
        with mock.patch.object(macos, "launchctl", return_value=subprocess.CompletedProcess([], 5)), self.assertRaises(LaunchError) as caught:
            self.backend.launch("ltc.test", [sys.executable], self.directory, self.directory / "log")
        self.assertTrue(caught.exception.uncertain)

    def test_probe_preserves_unknown_and_pending_owners(self):
        for output, expected in (
            ("\tstate = running\n\tpid = 123\n", OwnerState.ALIVE),
            ("\tstate = spawn scheduled\n", OwnerState.ALIVE),
            ("\tstate = not running\n\truns = 0\n", OwnerState.UNKNOWN),
            ("\tstate = not running\n\truns = 1\n", OwnerState.ABSENT),
            ("\tstate = future format\n", OwnerState.UNKNOWN),
            ("\tstate = not running\n\t\tpid = 123\n", OwnerState.UNKNOWN),
        ):
            with self.subTest(output=output), mock.patch.object(macos, "launchctl", return_value=subprocess.CompletedProcess([], 0, output, "")):
                self.assertEqual(self.backend.probe("ltc.test"), expected)
        for code, error in ((5, "permission denied"), (113, "domain not available")):
            with mock.patch.object(macos, "launchctl", return_value=subprocess.CompletedProcess([], code, "", error)):
                self.assertEqual(self.backend.probe("ltc.test"), OwnerState.UNKNOWN)
        missing = subprocess.CompletedProcess([], 113, "", 'Could not find service "ltc.test" in domain for user gui: 501')
        with mock.patch.object(macos, "launchctl", return_value=missing), mock.patch.object(macos, "manager_available", return_value=True):
            self.assertEqual(self.backend.probe("ltc.test"), OwnerState.ABSENT)
        with mock.patch.object(macos, "launchctl", return_value=missing), mock.patch.object(macos, "manager_available", return_value=False):
            self.assertEqual(self.backend.probe("ltc.test"), OwnerState.UNKNOWN)

    def test_collection_never_stops_live_or_unknown_workers(self):
        for state in (OwnerState.ALIVE, OwnerState.UNKNOWN):
            with mock.patch.object(macos, "job_status", return_value=(state, None, True)), mock.patch.object(macos, "launchctl") as control:
                self.assertFalse(self.backend.collect("ltc.test"))
                control.assert_not_called()

    def test_worker_admission_binds_launchd_context_and_pid(self):
        with mock.patch.dict(os.environ, {macos.OWNER_ENV: "ltc.test", "XPC_SERVICE_NAME": "ltc.test"}), mock.patch.object(
            os, "getppid", return_value=1
        ), mock.patch.object(macos, "job_status", return_value=(OwnerState.ALIVE, os.getpid(), True)):
            self.assertTrue(self.backend.admits_worker("ltc.test"))
            self.assertFalse(self.backend.admits_worker("ltc.other"))
        with mock.patch.dict(os.environ, {macos.OWNER_ENV: "ltc.test", "XPC_SERVICE_NAME": "ltc.test"}), mock.patch.object(
            os, "getppid", return_value=1
        ), mock.patch.object(macos, "job_status", return_value=(OwnerState.ALIVE, os.getpid() + 1, True)):
            self.assertFalse(self.backend.admits_worker("ltc.test"))

    def test_worker_rejects_stale_attempt_host_boot_and_owner_before_command(self):
        ident = "abcd1234"
        task = {"version": 2, "id": ident, "queue_dir": str(self.queue),
                "execution_backend": "launchd", "state": "launching", "agent": "codex",
                "launch_attempt_count": 1, "launch_boot_id": "boot", "machine_id": "host",
                "execution_owner": self.backend.owner_name(ident, 1, self.queue)}
        args = argparse.Namespace(task_file=str(cli.managed_task_path(self.queue, ident)), attempt=1)
        for change in ({"state": "running"}, {"launch_attempt_count": 2},
                       {"launch_boot_id": "previous"}, {"launch_boot_id": None},
                       {"machine_id": "foreign"}, {"execution_owner": "ltc.other"}, {}):
            with self.subTest(change=change), mock.patch.object(LaunchdBackend, "admits_worker", return_value=True), mock.patch.object(
                cli, "run_managed_worker_locked", return_value=7
            ) as command, contextlib.redirect_stderr(io.StringIO()):
                cli.write_managed_task(self.queue, dict(task, **change))
                self.assertEqual(cli.run_task_worker(args), 125 if change else 7)
                self.assertEqual(command.call_count, 0 if change else 1)

    def test_launch_boot_detects_reboot_even_when_submitter_could_not_read_boot(self):
        task = {"version": 2, "id": "abcd1234", "queue_dir": str(self.queue),
                "execution_backend": "launchd", "machine_id": "host", "submission_boot_id": None,
                "launch_boot_id": "previous-boot", "state": "launching", "agent": "codex"}
        cli.write_managed_task(self.queue, task)
        with mock.patch.object(cli, "mark_managed_task_interrupted", return_value=True) as interrupt, mock.patch.object(
            LaunchdBackend, "launch"
        ) as launch, mock.patch.object(LaunchdBackend, "probe") as probe:
            cli.recover_managed_tasks(self.queue)
        self.assertEqual(interrupt.call_args.args[2], "not_started_before_host_reboot")
        launch.assert_not_called()
        probe.assert_not_called()
        self.assertEqual(diagnostics.runtime_issues(self.queue)[0]["code"], "task_boot_changed")

    def test_ambiguous_native_launch_is_never_replayed(self):
        task = {"version": 2, "id": "abcd1234", "queue_dir": str(self.queue),
                "execution_backend": "launchd", "machine_id": "host", "submission_boot_id": "boot",
                "state": "submitted", "environment_path": str(self.directory / "env.json"),
                "cwd": str(self.directory), "log_path": str(self.directory / "log"), "agent": "codex"}
        Path(task["environment_path"]).write_text("{}")
        cli.write_managed_task(self.queue, task)
        with mock.patch.object(LaunchdBackend, "launch", side_effect=LaunchError("unknown", uncertain=True)) as launch, mock.patch.object(
            LaunchdBackend, "probe", return_value=OwnerState.UNKNOWN
        ):
            cli.recover_managed_tasks(self.queue)
            cli.recover_managed_tasks(self.queue)
            launch.assert_called_once()
        current = cli.load_managed_task(cli.managed_task_path(self.queue, "abcd1234"))
        self.assertTrue(current["launch_uncertain"])
        self.assertEqual(current["state"], "launching")

    def test_reload_is_consumed_only_by_matching_process_without_signals(self):
        identity = {"boot_id": "boot", "start_microseconds": 123}
        payload = {"pid": os.getpid(), "process_identity": identity,
                   "reload_protocol": cli.RELOAD_PROTOCOL_VERSION, "queue_dir": str(self.queue)}
        with mock.patch.object(cli, "read_daemon_runtime", return_value=payload), mock.patch.object(
            cli, "daemon_process_identity", return_value=identity
        ), mock.patch.object(os, "kill") as kill:
            self.assertTrue(cli.send_standalone_reload(os.getpid(), expected_queue=self.queue))
            self.assertTrue(cli.consume_daemon_reload_request(self.queue))
            self.assertFalse(cli.consume_daemon_reload_request(self.queue))
            cli.write_request(self.queue / "daemon-reload.json", dict(payload, pid=os.getpid() + 1))
            self.assertFalse(cli.consume_daemon_reload_request(self.queue))
            kill.assert_not_called()

    def test_coordinator_preview_is_read_only_private_and_pins_profiles(self):
        with mock.patch.object(cli, "codex_home", return_value=self.directory / "codex"), mock.patch.object(
            cli, "claude_home", return_value=self.directory / "claude"
        ), mock.patch.dict(os.environ, {"HTTPS_PROXY": "SECRET_PROXY", "API_KEY": "SECRET_API"}), mock.patch.object(
            macos, "launchctl"
        ) as control, mock.patch.object(cli, "configure_proxy_environment") as configure, contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(launchd_service.install(self.arguments(print=True)), 0)
        data = plistlib.loads(output.getvalue().encode())
        self.assertTrue(data["KeepAlive"])
        self.assertEqual(data["EnvironmentVariables"]["CODEX_HOME"], str(self.directory / "codex"))
        self.assertEqual(data["EnvironmentVariables"]["CLAUDE_CONFIG_DIR"], str(self.directory / "claude"))
        self.assertEqual(data["ThrottleInterval"], 3)
        self.assertIn(str(self.queue), data["ProgramArguments"])
        self.assertNotIn("SECRET", output.getvalue())
        control.assert_not_called()
        configure.assert_not_called()
        self.assertFalse(self.queue.exists())

    def test_installation_reloads_live_coordinator_without_bootout(self):
        with mock.patch.object(launchd_service, "agent_directory", return_value=self.directory / "agents"), mock.patch.object(
            cli, "daemon_state_dir", return_value=self.directory / "state"
        ), mock.patch.object(cli, "configure_proxy_environment"), mock.patch.object(
            macos, "launchctl", return_value=subprocess.CompletedProcess([], 0)
        ) as control, mock.patch.object(macos, "job_status", return_value=(OwnerState.ALIVE, 1234, True)), mock.patch.object(
            cli, "send_standalone_reload", return_value=True
        ) as reload, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(launchd_service.install(self.arguments()), 0)
        reload.assert_called_once_with(1234, expected_queue=self.queue)
        control.assert_called_once_with(["enable", f"{macos.gui_domain()}/ltc-test"])

    def test_mac_screen_uses_system_compatible_logging_with_literal_arguments(self):
        argv = ["/literal python", "entry.py", "--task-file", "/tmp/$value;literal"]
        with mock.patch.object(screen, "screen_binary", return_value="/usr/bin/screen"), mock.patch.object(
            subprocess, "run", return_value=subprocess.CompletedProcess([], 0)
        ) as run:
            screen.ScreenBackend().launch("ltc-abcd1234", argv, self.directory, self.directory / "log")
        command = run.call_args.args[0]
        self.assertNotIn("-Logfile", command)
        self.assertEqual(command[-len(argv):], argv)
        self.assertEqual(command[1:3], ["-c", "/dev/null"])
        self.assertFalse(run.call_args.kwargs.get("shell", False))


@unittest.skipUnless(sys.platform == "darwin", "requires Darwin native interfaces")
class NativeMacIdentityTests(unittest.TestCase):
    def test_local_process_and_host_identity_are_available_and_repeatable(self):
        identity = macos.process_identity(os.getpid())
        self.assertIsNotNone(identity)
        self.assertEqual(identity, macos.process_identity(os.getpid()))
        self.assertEqual(identity["boot_id"], macos.current_boot_id())
        self.assertEqual(identity["machine_id"], macos.current_machine_id())
        self.assertEqual(identity["uid"], os.getuid())
        self.assertIsNone(macos.process_identity(-1))
        self.assertIsNone(macos.process_identity(True))


if __name__ == "__main__":
    unittest.main()
