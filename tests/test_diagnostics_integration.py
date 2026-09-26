"""Opt-in local repair from doctor output to a real standalone coordinator.

This test owns temporary Agent profiles, a queue and every subprocess it starts.
It invokes no model, creates no business task or callback, and never consults an
installed daemon's PID file. Only the unrelated Claude authentication advisory is
mocked; skill installation, process startup and readiness evidence are real.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

from long_task_callback import cli
from long_task_callback.platforms import SystemdUserBackend
from long_task_callback.runtime import worker_command


@unittest.skipUnless(
    os.environ.get("LTC_TEST_DIAGNOSTICS") == "1",
    "set LTC_TEST_DIAGNOSTICS=1 for real standalone configuration repair",
)
class RealDiagnosticsRepairTests(unittest.TestCase):
    def invoke(self, arguments: list[str]) -> tuple[int, str, str]:
        with mock.patch.object(sys, "argv", ["ltc", *arguments]), contextlib.redirect_stdout(
            io.StringIO()
        ) as output, contextlib.redirect_stderr(io.StringIO()) as errors:
            status = cli.main()
        return status, output.getvalue(), errors.getvalue()

    def pinned_arguments(self, command: list[str], operation: str) -> list[str]:
        prefix = worker_command()
        self.assertEqual(command[:len(prefix)], prefix, "repair must use the inspected LTC installation")
        self.assertEqual(command[len(prefix)], operation)
        return command[len(prefix):]

    def test_generated_callback_only_repair_installs_skill_and_starts_real_coordinator(self) -> None:
        if sys.platform != "linux":
            self.skipTest("Linux process identity and POSIX queue locks are required")

        with tempfile.TemporaryDirectory(prefix="ltc doctor repair ") as temporary:
            directory = Path(temporary)
            root = directory / "isolated queue"
            codex_home = directory / "codex profile"
            claude_home = directory / "claude profile"
            config_home = directory / "config"
            runtime_directory = directory / "runtime"
            runtime_directory.mkdir(mode=0o700)
            fake_cli = directory / "fixture codex"
            unexpected_agent_invocation = directory / "agent-was-invoked"
            fake_cli.write_text(
                "#!/bin/sh\n"
                f": > {shlex.quote(str(unexpected_agent_invocation))}\n"
                "exit 97\n",
                encoding="utf-8",
            )
            fake_cli.chmod(0o700)
            # Exclude inherited Agent/auth/session settings entirely. The fixture
            # CLI is discovered through its absolute path and must never run.
            environment = {
                "PATH": "/usr/bin:/bin",
                "CODEX_HOME": str(codex_home),
                "CLAUDE_CONFIG_DIR": str(claude_home),
                "XDG_CONFIG_HOME": str(config_home),
                "XDG_RUNTIME_DIR": str(runtime_directory),
                "DBUS_SESSION_BUS_ADDRESS": f"unix:path={directory}/absent-user-bus",
                "CODEX_LONG_TASK_WAKEUP_CODEX_BIN": str(fake_cli),
                "LONG_TASK_WAKEUP_CLAUDE_BIN": str(directory / "absent-claude"),
                "LONG_TASK_WAKEUP_SCREEN_BIN": str(directory / "absent-screen"),
                "CODEX_LONG_TASK_WAKEUP_DESKTOP_APP_SERVER": "0",
                "CODEX_LONG_TASK_WAKEUP_TARGET_LOCK_DIR": str(directory / "target-locks"),
            }
            doctor_arguments = [
                "doctor", "--queue-dir", str(root), "--backend", "systemd",
                "--operation", "done", "--agent", "codex", "--session", "fixture-original-session",
            ]
            original_popen = subprocess.Popen
            owned_processes = []
            daemons = []

            def capture_owned_process(*arguments, **keywords):
                process = original_popen(*arguments, **keywords)
                owned_processes.append(process)
                command = arguments[0] if arguments else keywords.get("args")
                if isinstance(command, (list, tuple)):
                    command = [os.fspath(value) for value in command]
                    prefix = worker_command()
                    if command[:len(prefix)] == prefix and command[len(prefix):len(prefix) + 1] == ["daemon"]:
                        self.assertIn(str(root), command, "only the isolated queue may be started")
                        daemons.append(process)
                return process

            try:
                with mock.patch.dict(os.environ, environment, clear=True):
                    self.assertFalse(SystemdUserBackend().available())
                    initial_code, initial_output, initial_errors = self.invoke(doctor_arguments)
                    self.assertEqual(initial_code, 1, initial_errors)
                    initial = json.loads(initial_output)
                    self.assertEqual(initial["schema"], "ltc.health.v1")
                    self.assertEqual(initial["status"], "needs_configuration")
                    self.assertEqual(initial["work"], {"state": "not_applicable"})
                    issue_codes = {issue["code"] for issue in initial["issues"]}
                    self.assertTrue({"daemon_not_running", "skill_missing"}.issubset(issue_codes))
                    self.assertNotIn("backend_unavailable", issue_codes, "done needs only callback delivery")
                    self.assertFalse(root.exists(), "doctor must not create a queue while inspecting it")
                    self.assertFalse(codex_home.exists(), "doctor must not perform skill or daemon setup")

                    repair = initial["repair_command"]
                    repair_arguments = self.pinned_arguments(repair, "setup")
                    self.assertIn("--callback-only", repair_arguments)
                    self.assertIn("--keep-skill", repair_arguments)
                    self.assertEqual(repair[repair.index("--queue-dir") + 1], str(root))
                    self.assertEqual(repair[repair.index("--codex-bin") + 1], str(fake_cli))
                    self.assertNotIn("--last", repair)
                    with mock.patch.object(cli, "report_claude_agent_readiness") as auth_advisory, mock.patch.object(
                        cli.subprocess, "Popen", side_effect=capture_owned_process
                    ):
                        repair_code, repair_output, repair_errors = self.invoke(repair_arguments)
                    self.assertEqual(repair_code, 0, repair_output + repair_errors)
                    auth_advisory.assert_called_once()
                    self.assertEqual(len(daemons), 1, "repair must start one actual pinned coordinator")
                    daemon = daemons[0]
                    state = cli.daemon_state_dir()
                    log_path = state / "daemon.log"

                    deadline = time.monotonic() + 15.0
                    while time.monotonic() < deadline:
                        self.assertIsNone(
                            daemon.poll(),
                            log_path.read_text(encoding="utf-8") if log_path.exists() else "coordinator exited",
                        )
                        runtime = cli.read_daemon_runtime()
                        if (runtime is not None and runtime.get("pid") == daemon.pid
                                and cli.daemon_supports_hot_reload(daemon.pid, expected_queue=root)):
                            break
                        time.sleep(0.05)
                    else:
                        self.fail("coordinator did not publish verified runtime identity within 15 seconds")

                    self.assertEqual(runtime["queue_dir"], str(root.resolve()))
                    self.assertEqual(runtime["process_identity"], cli.daemon_process_identity(daemon.pid))
                    self.assertEqual(int((state / "daemon.pid").read_text()), daemon.pid)
                    lock = cli.acquire_owner_lock(root, "daemon-singleton", blocking=False)
                    if lock is not None:
                        cli.release_owner_lock(lock, remove=False)
                    self.assertIsNone(lock, "the actual coordinator must own this queue")
                    self.assertTrue((codex_home / "skills" / "long-task-callback" / "SKILL.md").is_file())
                    self.assertFalse((claude_home / "skills").exists(), "repair should install only the selected Agent skill")
                    self.assertFalse((config_home / "systemd").exists(), "standalone repair must not install a service unit")

                    recheck_arguments = self.pinned_arguments(initial["recheck_command"], "doctor")
                    self.assertEqual(
                        recheck_arguments[recheck_arguments.index("--session") + 1], "fixture-original-session",
                    )
                    final_code, final_output, final_errors = self.invoke(recheck_arguments)
                    self.assertEqual(final_code, 0, final_output + final_errors)
                    final = json.loads(final_output)
                    self.assertEqual(final["status"], "ready")
                    self.assertEqual(final["scope"], "local_prerequisites")
                    self.assertEqual(final["issues"], [])
                    self.assertEqual(final["work"], {"state": "not_applicable"})
                    self.assertTrue({"authentication", "session_delivery"}.issubset(final["unverified"]))
                    self.assertFalse(unexpected_agent_invocation.exists(), "local repair must not invoke an Agent")
                    self.assertEqual(list(root.rglob("task.json")), [])
                    self.assertEqual(list(root.rglob("result.json")), [])
                    for state_name in ("pending", "running", "done", "failed", "canceled", "acks"):
                        self.assertEqual(list((root / state_name).glob("*.json")), [], state_name)
                    self.assertIsNone(daemon.poll())
            finally:
                # These are only handles this test created. Never signal a PID
                # read from files when cleaning up a configuration repair test.
                for process in owned_processes:
                    if process.poll() is None:
                        process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=5)


if __name__ == "__main__":
    unittest.main()
