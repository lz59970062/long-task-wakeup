"""Opt-in Linux/container test with an unavailable systemd user bus.

The coordinator is a disposable standalone process. GNU screen owns the real
workload after the coordinator is killed. No installed daemon is changed and no
Agent delivery occurs. The test also runs inside a container that has screen.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path
from unittest import mock

from long_task_callback import cli, diagnostics
from long_task_callback.platforms import SystemdUserBackend


@unittest.skipUnless(os.environ.get("LTC_TEST_SCREEN") == "1", "set LTC_TEST_SCREEN=1 for real screen/container test")
class RealStandaloneScreenTests(unittest.TestCase):
    def wait_until(self, predicate, explanation: str, timeout: float = 15.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.05)
        self.fail(explanation)

    def test_real_standalone_start_ignores_pid_reused_by_unrelated_live_process(self) -> None:
        if sys.platform != "linux":
            self.skipTest("Linux process identity is required")
        with tempfile.TemporaryDirectory(prefix="ltc-stale-pid-") as temporary:
            directory = Path(temporary)
            home = directory / "codex-home"
            root = directory / "queue"
            environment = dict(os.environ, CODEX_HOME=str(home),
                               CODEX_LONG_TASK_WAKEUP_TARGET_LOCK_DIR=str(directory / "target-locks"),
                               CODEX_LONG_TASK_WAKEUP_DESKTOP_APP_SERVER="0")
            sleeper = subprocess.Popen(
                [sys.executable, "-c", "import time; time.sleep(60)"],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            original_popen = subprocess.Popen
            launched = []

            def capture_owned_daemon(*arguments, **keywords):
                process = original_popen(*arguments, **keywords)
                launched.append(process)
                return process

            try:
                with mock.patch.dict(os.environ, environment, clear=True):
                    state = cli.daemon_state_dir()
                    state.mkdir(parents=True)
                    (state / "daemon.pid").write_text(str(sleeper.pid))
                    runtime = cli.daemon_runtime_path()
                    runtime.write_text(json.dumps({
                        "pid": sleeper.pid, "reload_protocol": cli.RELOAD_PROTOCOL_VERSION,
                        "queue_dir": str(root), "process_identity": {
                            "boot_id": "prior-container-boot", "machine_id": "prior-machine",
                            "pid_namespace": "pid:[1]", "start_ticks": 1,
                        },
                    }))
                    args = argparse.Namespace(
                        queue_dir=str(root), interval=0.1, retries=1, retry_delay=1.0,
                        retry_backoff=1.0, resume_timeout=10.0, exec_start=None,
                        codex_bin="/unused-no-agent-dispatch", claude_bin="/unused-no-agent-dispatch",
                        path=os.environ.get("PATH", ""),
                    )
                    with mock.patch.object(cli.subprocess, "Popen", side_effect=capture_owned_daemon):
                        self.assertEqual(cli.start_standalone_daemon(args), 0)
                    self.assertIsNone(sleeper.poll(), "stale PID must never receive a reload signal")
                    self.assertEqual(len(launched), 1)
                    daemon = launched[0]
                    self.assertNotEqual(daemon.pid, sleeper.pid)

                    def runtime_ready():
                        payload = json.loads(runtime.read_text())
                        return payload.get("pid") == daemon.pid and bool(payload.get("process_identity"))

                    self.wait_until(runtime_ready, "new coordinator did not publish verified runtime identity")
                    self.assertIsNone(daemon.poll())
                    self.assertIsNone(sleeper.poll())
                    self.assertEqual(int((state / "daemon.pid").read_text()), daemon.pid)
                    self.assertTrue(cli.daemon_supports_hot_reload(daemon.pid, expected_queue=root))
                    lock = cli.acquire_owner_lock(root, "daemon-singleton", blocking=False)
                    if lock is not None:
                        cli.release_owner_lock(lock, remove=False)
                    self.assertIsNone(lock, "new coordinator must own the requested queue")
                    self.assertEqual(list((root / "pending").glob("*.json")), [])
            finally:
                # Use only handles created by this test; never trust disk PIDs
                # when cleaning up a test for stale persisted identities.
                for process in [*launched, sleeper]:
                    if process.poll() is None:
                        process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=5)

    def test_killing_standalone_coordinator_preserves_screen_work_without_systemd(self) -> None:
        screen = shutil.which("screen")
        if sys.platform != "linux" or screen is None:
            self.skipTest("Linux and GNU screen are required")
        with tempfile.TemporaryDirectory(prefix="ltc-real-screen-") as temporary:
            directory = Path(temporary)
            sockets = directory / "screen-sockets"
            runtime = directory / "runtime"
            sockets.mkdir(mode=0o700)
            runtime.mkdir(mode=0o700)
            environment = dict(os.environ, SCREENDIR=str(sockets), XDG_RUNTIME_DIR=str(runtime),
                               DBUS_SESSION_BUS_ADDRESS=f"unix:path={directory}/absent-user-bus")
            root = directory / "queue"
            started = directory / "workload-started"
            release = directory / "allow-finish"
            executions = directory / "executions.txt"
            output = directory / "artifact.txt"
            workload = directory / "workload.py"
            workload.write_text(textwrap.dedent("""\
                import os
                import sys
                import time
                from pathlib import Path
                started, release, executions, output = map(Path, sys.argv[1:])
                with executions.open("a") as stream:
                    stream.write("executed\\n")
                started.write_text(str(os.getpid()))
                deadline = time.monotonic() + 45
                while not release.exists():
                    if time.monotonic() >= deadline:
                        raise SystemExit(99)
                    time.sleep(0.05)
                output.write_text("finished after standalone coordinator was killed")
                raise SystemExit(7)
                """), encoding="utf-8")
            args = argparse.Namespace(
                backend="auto", agent="codex", cwd=str(directory), task="real standalone screen fixture",
                command=None, exit_code=None, message=None, session="fixture-no-real-agent-session",
                last=False, via_daemon=False, queue_dir=str(root), approvals_reviewer="auto_review",
                approval_policy="on-request", sandbox_mode="workspace-write", dry_run=False,
                strict=False, wrapped_command=[sys.executable, str(workload), str(started), str(release),
                                               str(executions), str(output)],
            )
            with mock.patch.dict(os.environ, environment, clear=True):
                self.assertFalse(SystemdUserBackend().available())
                self.assertEqual(cli.run(args), 0)
            task_path = next(cli.managed_tasks_root(root).glob("*/task.json"))
            task = cli.load_managed_task(task_path)
            self.assertEqual(task["execution_backend"], "screen")
            ready = directory / "coordinator-ready"
            coordinator = directory / "coordinator.py"
            source = str(Path(cli.__file__).resolve().parents[1])
            coordinator.write_text(textwrap.dedent(f"""\
                import sys
                import time
                from pathlib import Path
                sys.path.insert(0, {source!r})
                from long_task_callback import cli
                cli.recover_managed_tasks(Path(sys.argv[1]))
                Path(sys.argv[2]).write_text("launched")
                time.sleep(60)
                """), encoding="utf-8")
            process = None
            with (directory / "coordinator.log").open("wb") as log:
                try:
                    process = subprocess.Popen(
                        [sys.executable, str(coordinator), str(root), str(ready)], env=environment,
                        cwd=directory, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                        start_new_session=True,
                    )
                    self.wait_until(lambda: ready.exists() and started.exists(), "screen worker did not start")
                    self.assertIsNone(process.poll())
                    process.kill()
                    process.wait(timeout=5)
                    os.kill(int(started.read_text()), 0)
                    with mock.patch.dict(os.environ, environment, clear=True):
                        self.assertTrue(cli.screen_session_exists(task["screen_session"]))
                        self.assertEqual(diagnostics.runtime_issues(root), [])
                        cli.recover_managed_tasks(root)
                    self.assertEqual(executions.read_text().splitlines(), ["executed"])
                    self.assertFalse(output.exists())

                    release.write_text("finish", encoding="utf-8")
                    callback_path = cli.request_path(root, "pending", str(task["id"]))
                    self.wait_until(lambda: callback_path.exists(), "screen workload did not produce durable callback")
                    result = json.loads(cli.managed_result_path(root, str(task["id"])).read_text())
                    self.assertEqual(result["exit_code"], 7)
                    self.assertEqual(output.read_text(), "finished after standalone coordinator was killed")
                    with mock.patch.dict(os.environ, environment, clear=True):
                        self.wait_until(
                            lambda: cli.screen_owner_state(task["screen_session"]) == cli.OwnerState.ABSENT,
                            "completed screen owner did not exit",
                        )
                        self.assertEqual(diagnostics.runtime_issues(root), [])
                        cli.recover_managed_tasks(root)
                    self.assertEqual(executions.read_text().splitlines(), ["executed"])
                    self.assertEqual(len(list((root / "pending").glob("*.json"))), 1)
                    callback = cli.load_request(callback_path)
                    self.assertEqual(callback["exit_code"], 7)
                    self.assertEqual(callback["target"], {"kind": "session", "value": "fixture-no-real-agent-session"})
                    self.assertTrue(Path(callback["prompt_details_path"]).is_file())
                finally:
                    if process is not None and process.poll() is None:
                        process.kill()
                        process.wait(timeout=5)
                    subprocess.run(
                        [screen, "-S", task["screen_session"], "-X", "quit"], env=environment,
                        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                        timeout=10, check=False,
                    )


if __name__ == "__main__":
    unittest.main()
