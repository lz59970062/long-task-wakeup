"""Container screen ownership contracts, independent of systemd availability."""
from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from long_task_callback.platforms import LaunchError, OwnerState, ScreenBackend
from long_task_callback.platforms import screen


class ScreenOwnershipTests(unittest.TestCase):
    def setUp(self) -> None:
        self.backend = ScreenBackend()
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        for patcher in (
            mock.patch.object(screen.sys, "platform", "linux"),
            mock.patch.object(screen, "screen_binary", return_value="/usr/bin/screen"),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_availability_only_needs_a_working_screen_executable(self) -> None:
        for code, output, expected in ((0, "Screen version 4.09.00 (GNU) 30-Jan-22\n", True),
                                       (1, "", False), (0, "unrelated executable", False)):
            with self.subTest(code=code, output=output), mock.patch(
                "subprocess.run", return_value=subprocess.CompletedProcess([], code, output, "")
            ) as run:
                self.assertEqual(self.backend.available(), expected)
                self.assertEqual(run.call_args.args[0], ["/usr/bin/screen", "-v"])
                self.assertGreater(run.call_args.kwargs.get("timeout", 0), 0)

    def test_owner_name_preserves_legacy_short_and_long_identifiers(self) -> None:
        for ident in ("abcd1234", "0123456789abcdef0123456789abcdef"):
            for attempt in (1, 2):
                self.assertEqual(self.backend.owner_name(ident, attempt, self.directory), f"ltc-{ident}")

    def test_exact_live_owner_is_required_not_a_matching_name_prefix(self) -> None:
        for status in ("Attached", "Detached"):
            output = f"There is a screen on:\n\t1234.ltc-abcd1234\t(09/25/26 10:00:00)\t({status})\n1 Socket in /tmp/private.\n"
            with self.subTest(status=status), mock.patch(
                "subprocess.run", return_value=subprocess.CompletedProcess([], 0, output, "")
            ) as run:
                self.assertEqual(self.backend.probe("ltc-abcd1234"), OwnerState.ALIVE)
                self.assertEqual(run.call_args.kwargs["env"]["LC_ALL"], "C")
        other = "There is a screen on:\n\t1234.ltc-abcd1234-extra\t(Detached)\n1 Socket in /tmp/private.\n"
        with mock.patch("subprocess.run", return_value=subprocess.CompletedProcess([], 0, other, "")):
            self.assertEqual(self.backend.probe("ltc-abcd1234"), OwnerState.ABSENT)

    def test_only_explicit_empty_directory_or_dead_owner_establish_absence(self) -> None:
        for code, output in (
            (1, "No Sockets found in /tmp/private.\n"),
            (0, "There is a screen on:\n\t1234.ltc-abcd1234\t(Dead ???)\n1 Socket in /tmp/private.\n"),
        ):
            with self.subTest(output=output), mock.patch(
                "subprocess.run", return_value=subprocess.CompletedProcess([], code, output, "")
            ):
                self.assertEqual(self.backend.probe("ltc-abcd1234"), OwnerState.ABSENT)

    def test_screen_socket_permission_error_timeout_or_empty_output_is_unknown(self) -> None:
        for response in (
            subprocess.CompletedProcess([], 1, "", "Cannot open socket directory"),
            subprocess.CompletedProcess([], 0, "", ""),
            subprocess.CompletedProcess([], 1, "unrecognized localized error", ""),
        ):
            with self.subTest(response=response), mock.patch("subprocess.run", return_value=response):
                self.assertEqual(self.backend.probe("ltc-abcd1234"), OwnerState.UNKNOWN)
        for error in (PermissionError("socket directory"), subprocess.TimeoutExpired("screen", 1)):
            with self.subTest(error=error), mock.patch("subprocess.run", side_effect=error):
                self.assertEqual(self.backend.probe("ltc-abcd1234"), OwnerState.UNKNOWN)

    def test_launch_uses_literal_argv_and_does_not_attach_coordinator_pipes(self) -> None:
        arguments = ["/opt/LTC runtime/python", "/tmp/entry.py", "_screen-worker", "--task-file",
                     "/tmp/space $name;literal/task.json", "--token=fixture"]
        log = self.directory / "log with spaces"
        with mock.patch("subprocess.run", return_value=subprocess.CompletedProcess([], 0, "", "")) as run:
            self.backend.launch("ltc-abcd1234", arguments, self.directory, log)
        argv = run.call_args.args[0]
        self.assertEqual(argv, ["/usr/bin/screen", "-dmS", "ltc-abcd1234", "-L", "-Logfile", str(log), *arguments])
        self.assertFalse(run.call_args.kwargs.get("shell", False))
        self.assertEqual(run.call_args.kwargs["stdout"], subprocess.DEVNULL)
        self.assertEqual(run.call_args.kwargs["stderr"], subprocess.DEVNULL)
        self.assertEqual(run.call_args.kwargs["umask"], 0o077)
        self.assertGreater(run.call_args.kwargs.get("timeout", 0), 0)

    def test_timeout_and_nonzero_launch_are_ambiguous_but_preexec_failure_is_definitive(self) -> None:
        cases = ((subprocess.TimeoutExpired("screen", 1), True), (FileNotFoundError("screen"), False))
        for failure, uncertain in cases:
            with self.subTest(failure=failure), mock.patch("subprocess.run", side_effect=failure), self.assertRaises(LaunchError) as caught:
                self.backend.launch("ltc-abcd1234", ["/bin/true"], self.directory, self.directory / "log")
            self.assertEqual(caught.exception.uncertain, uncertain)
        with mock.patch("subprocess.run", return_value=subprocess.CompletedProcess([], 1)), self.assertRaises(LaunchError) as caught:
            self.backend.launch("ltc-abcd1234", ["/bin/true"], self.directory, self.directory / "log")
        self.assertTrue(caught.exception.uncertain)


if __name__ == "__main__":
    unittest.main()
