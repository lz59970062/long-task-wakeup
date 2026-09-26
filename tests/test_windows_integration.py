"""Opt-in real Scheduler/runner/coordinator tests; no Agent model calls.

Use LTC_TEST_WINDOWS_TASK=1 in an ordinary logged-in Windows user session.
Every task, profile and queue is temporary; no production service is touched.
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

from long_task_callback import cli, diagnostics, windows_service
from long_task_callback.platforms import OwnerState
from long_task_callback.platforms.windows import WindowsBackend, scheduler_call, process_identity
from long_task_callback.platforms import windows_io
from long_task_callback.runtime import worker_command


@unittest.skipUnless(sys.platform == "win32" and os.environ.get("LTC_TEST_WINDOWS_TASK") == "1",
                     "set LTC_TEST_WINDOWS_TASK=1 for real Windows scheduled tasks")
class WindowsIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="ltc Windows 中文 ")
        self.directory = Path(self.temporary.name).resolve()
        self.root = self.directory / "queue"
        self.profile = self.directory / "profile"
        self.environment = mock.patch.dict(os.environ, {
            "CODEX_HOME": str(self.profile), "CLAUDE_CONFIG_DIR": str(self.directory / "claude"),
            cli.TARGET_LOCK_DIR_ENV: str(self.directory / "target locks"),
            "CODEX_LONG_TASK_WAKEUP_CODEX_BIN": sys.executable,
        })
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.addCleanup(self.temporary.cleanup)
        self.owners = []
        self.addCleanup(self.cleanup_owners)

    def cleanup_owners(self):
        for owner in self.owners:
            try:
                scheduler_call("stop", {"owner": owner})
                scheduler_call("delete", {"owner": owner})
            except Exception:
                pass
        time.sleep(0.2)

    def wait_until(self, predicate, message, timeout=25):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.1)
        self.fail(message)

    def submit(self, script: Path, *arguments: str):
        args = argparse.Namespace(backend="windows-task", agent="codex", cwd=str(self.directory),
            task="Windows lifecycle fixture", command=None, message=None, session="fixture-no-model",
            last=False, queue_dir=str(self.root), strict=False, dry_run=False,
            wrapped_command=[sys.executable, str(script), *arguments])
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(cli.run(args), 0)
        path = next(cli.managed_tasks_root(self.root).glob("*/task.json"))
        task = cli.load_managed_task(path)
        self.owners.append(WindowsBackend().owner_name(task["id"], 1, self.root))
        return path, task

    def test_terminal_and_coordinator_exit_preserve_exactly_one_task(self):
        script = self.directory / "workload.py"
        started, release, executions = (self.directory / name for name in ("started", "release", "executions"))
        script.write_text(textwrap.dedent("""\
            import sys, time
            from pathlib import Path
            started, release, executions = map(Path, sys.argv[1:])
            with executions.open('a') as stream: stream.write('run\\n')
            started.touch()
            deadline = time.monotonic() + 40
            while not release.exists() and time.monotonic() < deadline: time.sleep(.05)
            print('completed after coordinator exit', flush=True)
            raise SystemExit(7)
            """), encoding="utf-8")
        task_path, task = self.submit(script, str(started), str(release), str(executions))
        coordinator_code = "from pathlib import Path; from long_task_callback import cli; import sys,time; cli.recover_managed_tasks(Path(sys.argv[1])); time.sleep(40)"
        # This submitter/control process can disappear; Task Scheduler owns the runner.
        coordinator = subprocess.Popen([sys.executable, "-c", coordinator_code, str(self.root)],
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            self.wait_until(started.exists, "scheduled worker did not execute")
            coordinator.terminate()
            coordinator.wait(timeout=5)
            self.assertEqual(WindowsBackend().probe(self.owners[0]), OwnerState.ALIVE)
            cli.recover_managed_tasks(self.root)
            self.assertEqual(executions.read_text(encoding="utf-8").splitlines(), ["run"])
            release.touch()
            callback = cli.request_path(self.root, "pending", task["id"])
            self.wait_until(callback.exists, "runner did not publish its callback")
            result = json.loads(cli.managed_result_path(self.root, task["id"]).read_text(encoding="utf-8"))
            self.assertEqual(result["exit_code"], 7)
            self.assertEqual(cli.load_managed_task(task_path)["state"], "completed")
            self.assertIn("completed after coordinator exit", Path(task["log_path"]).read_text(encoding="utf-8"))
            self.assertEqual(json.loads(callback.read_text(encoding="utf-8"))["target"]["value"], "fixture-no-model")
            self.wait_until(lambda: WindowsBackend().probe(self.owners[0]) == OwnerState.ABSENT,
                            "runner failed to exit")
            cli.recover_managed_tasks(self.root)
            self.assertTrue(cli.load_managed_task(task_path).get("owner_collected_at"))
            self.assertEqual(executions.read_text(encoding="utf-8").splitlines(), ["run"])
        finally:
            if coordinator.poll() is None:
                coordinator.terminate()
                coordinator.wait(timeout=5)

    def test_coordinator_install_reload_and_remove(self):
        name = "ltc-test-service"
        self.owners.append(windows_service.service_owner(name, self.profile))
        arguments = ["setup", "--name", name, "--queue-dir", str(self.root),
                     "--callback-only", "--service", "windows-task", "--keep-skill",
                     "--force", "--now", "--interval", "0.1", "--codex-bin", sys.executable]
        with mock.patch.object(sys, "argv", ["ltc", *arguments]), \
                mock.patch.object(cli, "report_claude_agent_readiness"), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(), 0)
        self.wait_until(lambda: diagnostics.coordinator_issue(self.root) is None,
                        "installed coordinator did not acquire its queue")
        before = cli.read_daemon_runtime()
        self.assertIsNotNone(process_identity(before["pid"]))
        with mock.patch.object(sys, "argv", ["ltc", *arguments]), \
                mock.patch.object(cli, "report_claude_agent_readiness"), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(), 0)
        self.wait_until(lambda: bool((after := cli.read_daemon_runtime()) and after["pid"] != before["pid"]),
                        "Windows coordinator was not replaced after reload")
        self.assertIsNone(diagnostics.coordinator_issue(self.root))
        after = cli.read_daemon_runtime()
        self.assertNotEqual(after["process_identity"], before["process_identity"])
        self.assertEqual(windows_service.uninstall(argparse.Namespace(name=name)), 0)
        self.wait_until(lambda: cli.read_daemon_runtime() is None,
                        "removed coordinator did not drain and stop")
        self.wait_until(lambda: not windows_io.probe_existing_lock(
            windows_service.config_path(name).with_suffix(".supervisor.lock")),
            "coordinator supervisor did not release its registration lock")
        self.assertEqual(WindowsBackend().probe(self.owners[0]), OwnerState.ABSENT)


if __name__ == "__main__":
    unittest.main()
