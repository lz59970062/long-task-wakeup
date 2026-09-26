"""Opt-in macOS lifetime tests with disposable jobs, queues and fake agents.

No installed coordinator is touched; all launchd registrations are removed.
Run with LTC_TEST_LAUNCHD=1 in a logged-in macOS GUI session.
"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from unittest import mock

from long_task_callback import cli, diagnostics, launchd_service
from long_task_callback.platforms import LaunchdBackend, OwnerState, macos


@unittest.skipUnless(sys.platform == "darwin" and os.environ.get("LTC_TEST_LAUNCHD") == "1",
                     "set LTC_TEST_LAUNCHD=1 for real macOS jobs")
class RealLaunchdTests(unittest.TestCase):
    def wait_until(self, predicate, message, timeout=12):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.05)
        self.fail(message)

    def remove_job(self, owner):
        macos.launchctl(["bootout", f"{macos.gui_domain()}/{owner}"])

    def test_task_survives_coordinator_exit_with_one_result_and_callback(self):
        self.assertTrue(macos.manager_available(), "macOS GUI domain is required")
        backend = LaunchdBackend()
        with tempfile.TemporaryDirectory(prefix="ltc-launchd-", dir="/tmp") as temporary:
            directory = Path(temporary).resolve()
            root = directory / "queue"
            started, release, executions = (directory / name for name in ("started", "release", "executions"))
            workload = directory / "workload.py"
            workload.write_text(textwrap.dedent("""\
                import sys, time
                from pathlib import Path
                started, release, executions = map(Path, sys.argv[1:])
                with executions.open('a') as stream:
                    stream.write('executed\\n')
                started.touch()
                deadline = time.monotonic() + 30
                while not release.exists():
                    if time.monotonic() > deadline:
                        raise SystemExit(99)
                    time.sleep(0.05)
                print('finished after coordinator exit', flush=True)
                raise SystemExit(7)
                """))
            args = argparse.Namespace(backend="launchd", agent="codex", cwd=str(directory),
                task="macOS lifetime fixture", command=None, message=None, session="fixture-no-real-agent",
                last=False, queue_dir=str(root), approvals_reviewer="auto_review", approval_policy="on-request",
                sandbox_mode="workspace-write", strict=False, dry_run=False,
                wrapped_command=[sys.executable, str(workload), str(started), str(release), str(executions)])
            # Sandboxed submitters may lack kern.bootsessionuuid access. The
            # unrestricted coordinator must supply its own launch boot binding.
            with mock.patch.object(cli, "current_boot_id", return_value=None):
                self.assertEqual(cli.run(args), 0)
            task_path = next(cli.managed_tasks_root(root).glob("*/task.json"))
            task = cli.load_managed_task(task_path)
            task_owner = backend.owner_name(task["id"], 1, root)
            coordinator_owner = backend.owner_name("test-coordinator", 1, root)
            coordinator = directory / "coordinator.py"
            source = str(Path(cli.__file__).resolve().parents[1])
            coordinator.write_text(textwrap.dedent(f"""\
                import sys, time
                from pathlib import Path
                sys.path.insert(0, {source!r})
                from long_task_callback import cli
                cli.recover_managed_tasks(Path(sys.argv[1]))
                time.sleep(40)
                """))
            try:
                backend.launch(coordinator_owner, [sys.executable, str(coordinator), str(root)],
                               directory, directory / "coordinator.log")
                self.wait_until(started.exists, "launchd task did not start")
                self.remove_job(coordinator_owner)
                self.assertEqual(backend.probe(task_owner), OwnerState.ALIVE)
                cli.recover_managed_tasks(root)
                self.assertEqual(executions.read_text().splitlines(), ["executed"])
                self.assertEqual(diagnostics.runtime_issues(root), [])
                release.touch()
                callback_path = cli.request_path(root, "pending", task["id"])
                self.wait_until(callback_path.exists, "task failed to publish callback")
                self.wait_until(lambda: backend.probe(task_owner) == OwnerState.ABSENT, "task owner did not exit")
                self.assertEqual(json.loads(cli.managed_result_path(root, task["id"]).read_text())["exit_code"], 7)
                self.assertIn("finished after coordinator exit", Path(task["log_path"]).read_text())
                cli.recover_managed_tasks(root)
                cli.recover_managed_tasks(root)
                self.assertTrue(cli.load_managed_task(task_path).get("owner_collected_at"))
                self.assertEqual(macos.job_status(task_owner), (OwnerState.ABSENT, None, False))
                self.assertEqual(executions.read_text().splitlines(), ["executed"])
                self.assertEqual(len(list((root / "pending").glob("*.json"))), 1)
                self.assertEqual(cli.load_request(callback_path)["target"], {"kind": "session", "value": "fixture-no-real-agent"})
            finally:
                self.remove_job(task_owner)
                self.remove_job(coordinator_owner)

    def test_launchagent_setup_doctor_and_reload_use_isolated_profile(self):
        self.assertTrue(macos.manager_available(), "macOS GUI domain is required")
        with tempfile.TemporaryDirectory(prefix="ltc-agent-", dir="/tmp") as temporary:
            directory = Path(temporary).resolve()
            queue = directory / "queue"
            name = LaunchdBackend().owner_name("test-daemon", 1, queue)
            home = directory / "codex"
            fake = directory / "fake-codex"
            fake.write_text("#!/bin/sh\nexit 97\n")
            fake.chmod(0o700)
            env = {"CODEX_HOME": str(home), "CLAUDE_CONFIG_DIR": str(directory / "claude"),
                   cli.TARGET_LOCK_DIR_ENV: str(directory / "locks"), cli.DESKTOP_APP_SERVER_ENV: "0",
                   "CODEX_LONG_TASK_WAKEUP_CODEX_BIN": str(fake)}
            argv = ["ltc", "setup", "--callback-only", "--name", name, "--queue-dir", str(queue),
                    "--service", "launchd", "--skill-target", "codex", "--codex-bin", str(fake),
                    "--interval", "0.05", "--force", "--enable", "--now"]
            with mock.patch.dict(os.environ, env), mock.patch.object(
                launchd_service, "agent_directory", return_value=directory / "LaunchAgents"
            ), mock.patch.object(cli, "report_claude_agent_readiness"), contextlib.redirect_stdout(io.StringIO()):
                try:
                    with mock.patch.object(sys, "argv", argv):
                        self.assertEqual(cli.main(), 0)
                    self.wait_until(lambda: diagnostics.coordinator_issue(queue) is None, "coordinator not ready")
                    before = cli.read_daemon_runtime()
                    args = argparse.Namespace(mode="doctor", operation="done", agent="codex", backend="auto",
                                              session="fixture-original", last=False, queue_dir=str(queue))
                    self.assertEqual(diagnostics.inspect(args)["status"], "ready")
                    with mock.patch.object(sys, "argv", argv):
                        self.assertEqual(cli.main(), 0)

                    def reloaded():
                        after = cli.read_daemon_runtime()
                        return after is not None and after["started_at"] > before["started_at"]

                    self.wait_until(reloaded, "coordinator did not reload")
                    self.assertEqual(cli.read_daemon_runtime()["pid"], before["pid"])
                    self.assertEqual(diagnostics.coordinator_issue(queue), None)
                finally:
                    self.remove_job(name)


if __name__ == "__main__":
    unittest.main()
