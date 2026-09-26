"""Independent contracts for Linux ownership, without a real user manager.

These tests deliberately inject ambiguous transport outcomes: a failed query is
not evidence that a previously submitted workload has stopped.
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from long_task_callback.platforms import LaunchError, OwnerState, SystemdUserBackend
from long_task_callback.platforms import linux


class SystemdOwnershipTests(unittest.TestCase):
    def setUp(self) -> None:
        self.backend = SystemdUserBackend()
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        platform = mock.patch.object(linux.sys, "platform", "linux")
        binaries = mock.patch.object(linux.shutil, "which", side_effect=lambda name: "/usr/bin/" + name)
        platform.start()
        binaries.start()
        self.addCleanup(platform.stop)
        self.addCleanup(binaries.stop)

    def test_availability_requires_a_reachable_sufficiently_recent_user_manager(self) -> None:
        for returncode, output, expected in (
            (0, "245\n", True), (0, "239\n", False),
            (1, "245\n", False), (0, "", False), (0, "running\n", False),
        ):
            with self.subTest(returncode=returncode, output=output), mock.patch(
                "subprocess.run", return_value=subprocess.CompletedProcess([], returncode, output, "")
            ) as run:
                self.assertEqual(self.backend.available(), expected)
                self.assertIn("--user", run.call_args.args[0])
                self.assertGreater(run.call_args.kwargs.get("timeout", 0), 0)

    def test_availability_does_not_advertise_linux_backend_on_other_platforms(self) -> None:
        for platform in ("darwin", "win32"):
            with self.subTest(platform=platform), mock.patch.object(linux.sys, "platform", platform), mock.patch(
                "subprocess.run"
            ) as run:
                self.assertFalse(self.backend.available())
                run.assert_not_called()

    def test_owner_identity_separates_queues_and_attempts_and_survives_restart(self) -> None:
        queue = self.directory / "queue with spaces"
        first = self.backend.owner_name("0a1b2c3d", 1, queue)
        self.assertEqual(first, SystemdUserBackend().owner_name("0a1b2c3d", 1, queue))
        self.assertNotEqual(first, self.backend.owner_name("0a1b2c3d", 2, queue))
        self.assertNotEqual(first, self.backend.owner_name("0a1b2c3d", 1, self.directory / "other"))
        self.assertNotEqual(first, self.backend.owner_name("0a1b2c3e", 1, queue))
        self.assertTrue(first.endswith(".service"))
        self.assertLessEqual(len(first.encode()), 255)
        self.assertNotIn("/", first)
        self.assertNotIn(" ", first)

    def test_launch_is_an_independent_service_not_a_scope_or_attached_pipe(self) -> None:
        owner = self.backend.owner_name("0a1b2c3d", 1, self.directory)
        cwd = self.directory / "work with spaces"
        log = self.directory / "attempt log.txt"
        worker = [str(self.directory / "ltc program/bin/ltc"), "_task-worker", "--task-file", "a path/task.json", "--attempt", "1"]
        with mock.patch("subprocess.run", return_value=subprocess.CompletedProcess([], 0, "", "")) as run:
            self.backend.launch(owner, worker, str(cwd), str(log))
        command = run.call_args.args[0]
        self.assertIn("--user", command)
        self.assertIn(f"--unit={owner}", command)
        self.assertIn("--property=Restart=no", command)
        self.assertIn("--property=KillMode=control-group", command)
        self.assertIn(f"--working-directory={cwd}", command)
        self.assertIn(f"--property=StandardOutput=append:{log}", command)
        self.assertIn(f"--property=StandardError=append:{log}", command)
        self.assertEqual(command[command.index("--") + 1 :], worker)
        for attached_option in ("--scope", "--pipe", "--pty", "--wait"):
            self.assertNotIn(attached_option, command)
        self.assertFalse(run.call_args.kwargs.get("shell", False))
        self.assertGreater(run.call_args.kwargs.get("timeout", 0), 0)

    def test_launch_timeout_is_uncertain_because_manager_may_have_accepted_unit(self) -> None:
        with mock.patch("subprocess.run", side_effect=subprocess.TimeoutExpired("systemd-run", 5)):
            with self.assertRaises(LaunchError) as caught:
                self.backend.launch("ltc-example.service", [sys.executable], str(self.directory), str(self.directory / "log"))
        self.assertTrue(caught.exception.uncertain)

    def test_nonzero_launch_response_is_not_proof_no_workload_exists(self) -> None:
        result = subprocess.CompletedProcess([], 1, "", "Unit already exists or reply lost")
        with mock.patch("subprocess.run", return_value=result):
            with self.assertRaises(LaunchError) as caught:
                self.backend.launch("ltc-example.service", [sys.executable], str(self.directory), str(self.directory / "log"))
        self.assertTrue(caught.exception.uncertain)

    def test_missing_launcher_is_definitive_prelaunch_failure(self) -> None:
        with mock.patch("subprocess.run", side_effect=FileNotFoundError("systemd-run")):
            with self.assertRaises(LaunchError) as caught:
                self.backend.launch("ltc-example.service", [sys.executable], str(self.directory), str(self.directory / "log"))
        self.assertFalse(caught.exception.uncertain)

    def test_query_errors_and_malformed_status_are_unknown_not_absent(self) -> None:
        responses = [
            subprocess.CompletedProcess([], 1, "", "Failed to connect to bus"),
            subprocess.CompletedProcess([], 4, "", "Unit not found"),
            subprocess.CompletedProcess([], 0, "", ""),
            subprocess.CompletedProcess([], 0, "LoadState=loaded\n", ""),
            subprocess.CompletedProcess([], 0, "LoadState=loaded\nActiveState=unexpected\n", ""),
        ]
        for response in responses:
            with self.subTest(response=response), mock.patch("subprocess.run", return_value=response):
                self.assertEqual(self.backend.probe("ltc-example.service"), OwnerState.UNKNOWN)
        for error in (FileNotFoundError("systemctl"), subprocess.TimeoutExpired("systemctl", 5)):
            with self.subTest(error=error), mock.patch("subprocess.run", side_effect=error):
                self.assertEqual(self.backend.probe("ltc-example.service"), OwnerState.UNKNOWN)

    def test_active_or_stopping_service_cannot_be_treated_as_relaunchable(self) -> None:
        for state in ("active", "activating", "deactivating", "reloading"):
            status = f"LoadState=loaded\nActiveState={state}\nJob=0\n"
            with self.subTest(state=state), mock.patch(
                "subprocess.run", return_value=subprocess.CompletedProcess([], 0, status, "")
            ):
                self.assertEqual(self.backend.probe("ltc-example.service"), OwnerState.ALIVE)

    def test_inactive_service_with_pending_start_job_is_still_owned(self) -> None:
        status = "LoadState=loaded\nActiveState=inactive\nJob=314\n"
        with mock.patch("subprocess.run", return_value=subprocess.CompletedProcess([], 0, status, "")):
            self.assertEqual(self.backend.probe("ltc-example.service"), OwnerState.ALIVE)

    def test_older_systemd_empty_job_property_means_no_pending_job(self) -> None:
        for load, state, expected in (
            ("loaded", "active", OwnerState.ALIVE),
            ("loaded", "inactive", OwnerState.ABSENT),
            ("not-found", "inactive", OwnerState.ABSENT),
        ):
            status = f"LoadState={load}\nActiveState={state}\nJob=\n"
            with self.subTest(status=status), mock.patch(
                "subprocess.run", return_value=subprocess.CompletedProcess([], 0, status, "")
            ):
                self.assertEqual(self.backend.probe("ltc-example.service"), expected)

    def test_only_successful_explicit_absence_is_absent(self) -> None:
        for status in (
            "LoadState=not-found\nActiveState=inactive\nJob=0\n",
            "LoadState=loaded\nActiveState=inactive\nJob=0\n",
            "LoadState=loaded\nActiveState=failed\nJob=0\n",
        ):
            with self.subTest(status=status), mock.patch(
                "subprocess.run", return_value=subprocess.CompletedProcess([], 0, status, "")
            ):
                self.assertEqual(self.backend.probe("ltc-example.service"), OwnerState.ABSENT)


if __name__ == "__main__":
    unittest.main()
