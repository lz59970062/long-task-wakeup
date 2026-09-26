"""Native Windows storage/process integration with disposable, model-free work."""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

from long_task_callback import cli
from long_task_callback.platforms import LaunchError, OwnerState
from long_task_callback.platforms.windows import WindowsBackend
from long_task_callback.platforms import windows_process


@unittest.skipUnless(os.name == "nt", "requires native Windows locks, ACLs and processes")
class WindowsLifecycleTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="ltc Windows 生命周期 ")
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name).resolve()
        self.root = self.directory / "queue 中文 space & 'quote"
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(mock.patch.dict(os.environ, {
            "CODEX_HOME": str(self.directory / "codex-profile"),
            "CLAUDE_CONFIG_DIR": str(self.directory / "claude-profile"),
            cli.TARGET_LOCK_DIR_ENV: str(self.directory / "global-target-locks"),
            "PYTHONPATH": str(Path(cli.__file__).resolve().parents[1]),
            "PYTHONIOENCODING": "utf-8",
        }))
        self.stack.enter_context(mock.patch.object(WindowsBackend, "available", return_value=True))
        self.stack.enter_context(mock.patch.object(cli, "current_machine_id", return_value="fixture-host"))
        self.stack.enter_context(mock.patch.object(cli, "current_boot_id", return_value="fixture-boot"))
        self.stack.enter_context(mock.patch.object(WindowsBackend, "collect", return_value=False))

    def submit(self):
        args = argparse.Namespace(
            backend="windows-task", agent="codex", cwd=str(self.directory), task="native Windows fixture",
            command=None, exit_code=None, message=None, session="windows-original-session", last=False,
            via_daemon=False, queue_dir=str(self.root), approvals_reviewer="auto_review",
            approval_policy="on-request", sandbox_mode="workspace-write", dry_run=False,
            strict=False, wrapped_command=[sys.executable, "-c", "raise SystemExit(7)"],
        )
        with contextlib.redirect_stderr(io.StringIO()) as errors:
            self.assertEqual(cli.run(args), 0, errors.getvalue())
        return next(cli.managed_tasks_root(self.root).glob("*/task.json"))

    def launching(self, path):
        with mock.patch.object(WindowsBackend, "launch") as launch:
            cli.recover_managed_tasks(self.root)
        self.assertEqual(launch.call_count, 1)
        return cli.load_managed_task(path)

    def request(self, root, ident, state="pending"):
        request = {
            "version": 1, "id": ident, "created_at": time.time(), "cwd": str(self.directory),
            "agent": "codex", "target": {"kind": "session", "value": "windows-original-session"},
            "target_source": "explicit", "prompt": "Model-free Windows lifecycle fixture",
            "queue_dir": str(root), "lifecycle_state": state, "attempts": 0,
        }
        cli.ensure_daemon_dirs(root)
        cli.write_request(cli.request_path(root, state, ident), request)
        return request

    def wait_for(self, predicate, timeout=10):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.025)
        self.fail("timed out waiting for the Windows fixture")

    def test_admission_binds_queue_attempt_owner_host_and_boot(self):
        path = self.submit()
        task = self.launching(path)
        args = argparse.Namespace(task_file=str(path), attempt=1)
        changes = [
            {"queue_dir": str(self.directory / "another-queue")},
            {"id": "foreign-task"}, {"state": "running"}, {"launch_attempt_count": 2},
            {"machine_id": "foreign-host"}, {"launch_boot_id": "old-boot"},
            {"launch_boot_id": None}, {"execution_owner": "foreign-owner"}, {},
        ]
        for change in changes:
            with self.subTest(change=change), mock.patch.object(WindowsBackend, "admits_worker", return_value=True), mock.patch.object(
                cli, "run_managed_worker_locked", return_value=7
            ) as workload, mock.patch.object(windows_process, "enter_worker_job") as enter, contextlib.redirect_stderr(io.StringIO()):
                cli.write_request(path, dict(task, **change))
                self.assertEqual(cli.run_task_worker(args), 125 if change else 7)
                self.assertEqual(workload.call_count, 0 if change else 1)
                self.assertEqual(enter.call_count, 0 if change else 1)

    def test_scheduler_context_and_unknown_current_boot_refuse_worker(self):
        path = self.submit()
        self.launching(path)
        for admitted, boot in ((False, "fixture-boot"), (True, None)):
            with self.subTest(admitted=admitted, boot=boot), mock.patch.object(WindowsBackend, "admits_worker", return_value=admitted), mock.patch.object(
                cli, "current_boot_id", return_value=boot
            ), mock.patch.object(cli, "run_managed_worker_locked") as workload, contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(cli.run_task_worker(argparse.Namespace(task_file=str(path), attempt=1)), 125)
                workload.assert_not_called()

    def test_unknown_and_ambiguous_launch_never_replay_business_work(self):
        path = self.submit()
        with mock.patch.object(WindowsBackend, "launch", side_effect=LaunchError("RPC reply lost", uncertain=True)) as launch, mock.patch.object(
            WindowsBackend, "probe", return_value=OwnerState.UNKNOWN
        ), contextlib.redirect_stderr(io.StringIO()):
            cli.recover_managed_tasks(self.root)
            cli.recover_managed_tasks(self.root)
        self.assertEqual(launch.call_count, 1)
        task = cli.load_managed_task(path)
        self.assertTrue(task["launch_uncertain"])
        self.assertEqual(task["state"], "launching")
        with mock.patch.object(WindowsBackend, "launch") as relaunch, mock.patch.object(
            WindowsBackend, "probe", return_value=OwnerState.ABSENT
        ), mock.patch.object(cli, "MANAGED_WORKER_HANDSHAKE_SECONDS", 0), contextlib.redirect_stderr(io.StringIO()):
            cli.recover_managed_tasks(self.root)
            cli.recover_managed_tasks(self.root)
        relaunch.assert_not_called()
        recovered = cli.load_managed_task(path)
        self.assertEqual((recovered["state"], recovered["outcome"]), ("interrupted", "unknown"))
        self.assertEqual(len(list((self.root / "pending").glob("*.json"))), 1)

    def test_running_owner_unknown_and_unknown_boot_preserve_work(self):
        path = self.submit()
        task = self.launching(path)
        task.update(state="running", boot_id="fixture-boot")
        cli.write_managed_task(self.root, task)
        with mock.patch.object(WindowsBackend, "launch") as launch, mock.patch.object(
            WindowsBackend, "probe", return_value=OwnerState.UNKNOWN
        ), mock.patch.object(cli, "current_boot_id", return_value=None):
            cli.recover_managed_tasks(self.root)
            cli.recover_managed_tasks(self.root)
        launch.assert_not_called()
        self.assertEqual(cli.load_managed_task(path)["state"], "running")
        self.assertEqual(list((self.root / "pending").glob("*.json")), [])

    def test_result_survives_callback_failure_and_recovers_same_id_without_replay(self):
        path = self.submit()
        task = self.launching(path)
        environment = dict(os.environ)
        with mock.patch.object(cli, "queue_managed_task_callback", side_effect=OSError("callback write failed")), mock.patch.object(
            cli.subprocess, "run", return_value=subprocess.CompletedProcess([], 7)
        ) as workload, contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(cli.run_managed_worker_locked(self.root, task), 7)
        # The worker restores its captured environment; isolate that behavior
        # from the test runner just as the real worker process is isolated.
        os.environ.clear()
        os.environ.update(environment)
        self.assertEqual(workload.call_count, 1)
        result = json.loads(cli.managed_result_path(self.root, str(task["id"])).read_text(encoding="utf-8"))
        self.assertEqual(result["exit_code"], 7)
        self.assertEqual(result["outcome"], "completed")
        # Also model the crash after result publication but before metadata.
        task.update(state="running", boot_id="fixture-boot")
        cli.write_managed_task(self.root, task)
        with mock.patch.object(WindowsBackend, "launch") as launch, mock.patch.object(
            WindowsBackend, "probe", return_value=OwnerState.UNKNOWN
        ) as probe, contextlib.redirect_stderr(io.StringIO()):
            cli.recover_managed_tasks(self.root)
            cli.recover_managed_tasks(self.root)
        launch.assert_not_called()
        probe.assert_not_called()
        callbacks = list((self.root / "pending").glob("*.json"))
        self.assertEqual(len(callbacks), 1)
        callback = cli.load_request(callbacks[0])
        self.assertEqual(callback["id"], task["id"])
        self.assertEqual(callback["exit_code"], 7)
        self.assertEqual(callback["target"], {"kind": "session", "value": "windows-original-session"})

    def test_ack_is_durable_when_global_retained_lease_is_not_writable(self):
        request = self.request(self.root, "queue-only-ack", state="failed")
        cli.retain_target_lease(self.root, request)
        lease = cli.retained_target_lease_path(request)
        with mock.patch.object(cli, "acquire_retained_target_lease_lock", side_effect=PermissionError("global lease denied")), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(cli.ack(argparse.Namespace(queue_dir=str(self.root), id=request["id"], message="inspected")), 0)
        self.assertTrue(cli.ack_path(self.root, request["id"]).exists())
        self.assertTrue(cli.request_path(self.root, "done", request["id"]).exists())
        self.assertTrue(lease.exists())
        cli.reconcile_acknowledged_retained_leases(self.root)
        self.assertFalse(lease.exists())

    def test_generated_ack_runs_in_powershell_with_chinese_spaces_and_quotes(self):
        powershell = shutil.which("powershell.exe") or shutil.which("pwsh.exe")
        self.assertIsNotNone(powershell)
        request = self.request(self.root, "powershell-ack", state="running")
        command = cli.control_command("ack", "--queue-dir", str(self.root), "--id", request["id"])
        result = subprocess.run([powershell, "-NoProfile", "-NonInteractive", "-Command", command],
                                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=20,
                                **windows_process.background_popen_kwargs())
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(cli.ack_path(self.root, request["id"]).exists())
        self.assertIn("'quote", str(self.root))

    def test_coordinator_death_leaves_delivery_and_cross_queue_lease_alive(self):
        owner = self.request(self.root, "owner", state="running")
        other_root = self.directory / "other queue"
        contender = self.request(other_root, "contender")
        launched = self.directory / "launches.jsonl"
        release = self.directory / "release"
        finished = self.directory / "finished"
        fixture = self.directory / "fake agent 中文.py"
        fixture.write_text(
            "import json,os,sys,time\nfrom pathlib import Path\n"
            "prompt=sys.stdin.read()\n"
            f"with Path({str(launched)!r}).open('a',encoding='utf-8') as log: log.write(json.dumps({{'pid':os.getpid(),'worker':os.getppid(),'prompt':prompt}})+'\\n')\n"
            f"deadline=time.monotonic()+20\nwhile not Path({str(release)!r}).exists() and time.monotonic()<deadline: time.sleep(0.025)\n"
            f"Path({str(finished)!r}).write_text('done',encoding='utf-8')\n",
            encoding="utf-8",
        )
        coordinator_program = (
            "import argparse,json,sys\nfrom pathlib import Path\nfrom long_task_callback import cli\n"
            f"root=Path({str(self.root)!r})\nrequest=json.loads({json.dumps(owner)!r})\n"
            f"cli.resume_command=lambda request:[sys.executable,{str(fixture)!r}]\n"
            "cli.run_resume_until_exit_or_ack(root,'owner',request,argparse.Namespace(resume_timeout=25))\n"
        )
        log_path = self.directory / "coordinator.log"
        worker_pid = None
        with log_path.open("w", encoding="utf-8") as log:
            coordinator = subprocess.Popen([sys.executable, "-c", coordinator_program], stdout=subprocess.DEVNULL,
                                           stderr=log, **windows_process.background_popen_kwargs())
            try:
                self.wait_for(lambda: launched.exists() and bool(launched.read_text(encoding="utf-8").strip()))
                first = json.loads(launched.read_text(encoding="utf-8").splitlines()[0])
                worker_pid = first["worker"]
                self.assertIn("Model-free Windows", first["prompt"])
                coordinator.kill()
                coordinator.wait(timeout=5)
                self.assertTrue(cli.delivery_lock_is_held(self.root, "owner"))
                self.assertTrue(cli.target_lock_is_held(contender))
                self.assertIsNone(cli.select_pending(other_root, time.time()))
                cli.recover_running(self.root)
                self.assertTrue(cli.request_path(self.root, "running", "owner").exists())
                with mock.patch.object(cli, "resume_command", return_value=[sys.executable, str(fixture)]):
                    with self.assertRaises(cli.TargetLeaseUnavailable):
                        cli.run_resume_until_exit_or_ack(other_root, "contender", contender, argparse.Namespace(resume_timeout=5))
                self.assertEqual(len(launched.read_text(encoding="utf-8").splitlines()), 1)
                self.assertFalse(finished.exists())
                release.touch()
                self.wait_for(lambda: finished.exists() and not cli.delivery_lock_is_held(self.root, "owner"))
                self.assertFalse(cli.target_lock_is_held(contender))
                self.assertEqual(cli.select_pending(other_root, time.time()).stem, "contender")
            finally:
                release.touch()
                if coordinator.poll() is None:
                    coordinator.kill()
                coordinator.wait(timeout=5)
                # Wait for the short fixture to finish and the native delivery
                # worker to close its inherited handles before temp cleanup.
                deadline = time.monotonic() + 5
                while worker_pid is not None and time.monotonic() < deadline and cli.delivery_lock_is_held(self.root, "owner"):
                    time.sleep(0.025)


if __name__ == "__main__":
    unittest.main()
