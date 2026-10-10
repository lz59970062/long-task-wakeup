from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from long_task_callback import claude_code, cli

SRC = str(Path(__file__).resolve().parents[1] / "src")
SESSION = "3f0c9a52-claude-session"


def make_executable(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n", encoding="utf-8")
    path.chmod(0o755)
    return path


def no_which(_name: str) -> None:
    return None


class ExecutableDiscoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name)
        self.environment = {"HOME": str(self.home), "PATH": "/nonexistent"}

    def bundle(self, version: str) -> Path:
        return make_executable(
            self.home / "Library/Application Support/Claude/claude-code" / version / "build"
            / "claude.app/Contents/MacOS/claude"
        )

    def test_nothing_installed(self) -> None:
        self.assertIsNone(claude_code.discover_executable(self.environment, which=no_which))
        resolved = claude_code.with_resolved_executable(self.environment, which=no_which)
        self.assertNotIn(claude_code.CLAUDE_BIN_ENV, resolved)

    def test_path_lookup_is_preferred_to_known_locations(self) -> None:
        make_executable(self.home / ".local/bin/claude")
        found = claude_code.discover_executable(self.environment, which=lambda name: "/usr/bin/claude")
        self.assertEqual(found, claude_code.Executable("/usr/bin/claude", "path"))

    def test_standalone_install_is_found_without_path(self) -> None:
        standalone = make_executable(self.home / ".local/bin/claude")
        self.bundle("2.1.0")
        found = claude_code.discover_executable(self.environment, which=no_which)
        self.assertEqual(found, claude_code.Executable(str(standalone), "standalone"))

    def test_session_execpath_then_newest_desktop_bundle(self) -> None:
        old = self.bundle("2.1.0")
        new = self.bundle("2.2.0")
        os.utime(old, (1, 1))
        found = claude_code.discover_executable(self.environment, which=no_which)
        self.assertEqual(found, claude_code.Executable(str(new), "desktop-bundle"))
        session_binary = make_executable(self.home / "custom/claude")
        environment = dict(self.environment, CLAUDE_CODE_EXECPATH=str(session_binary))
        found = claude_code.discover_executable(environment, which=no_which)
        self.assertEqual(found, claude_code.Executable(str(session_binary), "session"))

    def test_explicit_path_is_honored_even_when_missing(self) -> None:
        environment = dict(self.environment, **{claude_code.CLAUDE_BIN_ENV: "/opt/claude"})
        found = claude_code.discover_executable(environment, which=no_which)
        self.assertEqual(found, claude_code.Executable("/opt/claude", "configured"))

    def test_explicit_name_is_honored_but_the_bare_default_is_rediscovered(self) -> None:
        standalone = make_executable(self.home / ".local/bin/claude")
        named = dict(self.environment, **{claude_code.CLAUDE_BIN_ENV: "claude-nightly"})
        self.assertEqual(claude_code.discover_executable(named, which=no_which),
                         claude_code.Executable("claude-nightly", "configured"))
        default = dict(self.environment, **{claude_code.CLAUDE_BIN_ENV: "claude"})
        self.assertEqual(claude_code.discover_executable(default, which=no_which),
                         claude_code.Executable(str(standalone), "standalone"))

    def test_vanished_desktop_bundle_is_rediscovered(self) -> None:
        current = self.bundle("2.2.0")
        stale = str(current).replace("2.2.0", "2.1.0")
        environment = dict(self.environment, **{claude_code.CLAUDE_BIN_ENV: stale})
        found = claude_code.discover_executable(environment, which=no_which)
        self.assertEqual(found, claude_code.Executable(str(current), "desktop-bundle"))

    def test_newest_nvm_install_is_found_without_path(self) -> None:
        old = make_executable(self.home / ".nvm/versions/node/v20.1.0/bin/claude")
        new = make_executable(self.home / ".nvm/versions/node/v22.3.0/bin/claude")
        os.utime(old, (1, 1))
        found = claude_code.discover_executable(self.environment, which=no_which)
        self.assertEqual(found, claude_code.Executable(str(new), "standalone"))

    def test_linux_desktop_remote_cli_is_found_and_rediscovered(self) -> None:
        # The desktop app's SSH sessions install a versioned CLI under ~/.claude/remote.
        remote = self.home / ".claude/remote/ccd-cli"
        old = make_executable(remote / "2.1.293-aaaa")
        new = make_executable(remote / "2.1.295-bbbb")
        os.utime(old, (1, 1))
        found = claude_code.discover_executable(self.environment, which=no_which)
        self.assertEqual(found, claude_code.Executable(str(new), "desktop-remote"))
        self.assertFalse(claude_code.is_desktop_bundle(new))  # It shares ~/.claude sign-in.
        session = dict(self.environment, CLAUDE_CODE_EXECPATH=str(new))
        self.assertEqual(claude_code.discover_executable(session, which=no_which),
                         claude_code.Executable(str(new), "desktop-remote"))
        stale = dict(self.environment, **{claude_code.CLAUDE_BIN_ENV: str(remote / "2.1.200-gone")})
        self.assertEqual(claude_code.discover_executable(stale, which=no_which),
                         claude_code.Executable(str(new), "desktop-remote"))
        standalone = make_executable(self.home / ".local/bin/claude")
        self.assertEqual(claude_code.discover_executable(self.environment, which=no_which),
                         claude_code.Executable(str(standalone), "standalone"))

    def test_resume_command_uses_discovered_binary(self) -> None:
        request = {"agent": "claude", "target": {"kind": "session", "value": SESSION}, "cwd": "/tmp", "prompt": "x"}
        with mock.patch.object(cli.claude_code, "discover_executable",
                               return_value=claude_code.Executable("/found/claude", "standalone")):
            self.assertEqual(cli.resume_command(request)[0], "/found/claude")


class SessionDirectoryTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.config = self.root / "claude-config"
        self.project = self.root / "project"
        self.project.mkdir()
        self.environment = {"HOME": str(self.root), "CLAUDE_CONFIG_DIR": str(self.config)}

    def write_session(self, *records: dict[str, object]) -> Path:
        path = self.config / "projects" / "-slug-of-project" / f"{SESSION}.jsonl"
        path.parent.mkdir(parents=True)
        lines = ["not json"] + [json.dumps(record) for record in records]
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return path

    def test_first_recorded_cwd_wins_over_later_cd(self) -> None:
        later = self.project / "subdir"
        later.mkdir()
        self.write_session({"type": "queue-operation"}, {"cwd": str(self.project)}, {"cwd": str(later)})
        self.assertEqual(claude_code.session_project_cwd(SESSION, self.environment), str(self.project))

    def test_missing_unsafe_or_deleted_sessions_have_no_cwd(self) -> None:
        self.assertIsNone(claude_code.session_project_cwd(SESSION, self.environment))
        self.assertIsNone(claude_code.session_project_cwd("../escape", self.environment))
        self.write_session({"cwd": str(self.root / "deleted")})
        self.assertIsNone(claude_code.session_project_cwd(SESSION, self.environment))

    def test_daemon_resumes_claude_where_the_session_started(self) -> None:
        self.write_session({"cwd": str(self.project)})
        claude = {"agent": "claude", "target": {"kind": "session", "value": SESSION}, "cwd": "/task/cwd"}
        codex = dict(claude, agent="codex")
        last = dict(claude, target={"kind": "last"})
        with mock.patch.dict(os.environ, self.environment):
            self.assertEqual(cli.resume_cwd(claude), str(self.project))
            self.assertEqual(cli.resume_cwd(codex), "/task/cwd")
            self.assertEqual(cli.resume_cwd(last), "/task/cwd")


class LiveWaiterTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="ltc live ")
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.root = self.directory / "queue"
        self.environment = {
            "CODEX_HOME": str(self.directory / "codex-home"),
            cli.TARGET_LOCK_DIR_ENV: str(self.directory / "target-locks"),
        }
        patcher = mock.patch.dict(os.environ, self.environment)
        patcher.start()
        self.addCleanup(patcher.stop)
        cli.ensure_daemon_dirs(self.root)

    def enqueue(self, ident: str, *, session: str = SESSION, agent: str = "claude", **extra: object) -> Path:
        args = argparse.Namespace(
            agent=agent, session=session, last=False, task="train model", cwd=str(self.directory),
            command="python train.py", exit_code=0, message=None, queue_dir=str(self.root), _callback_id=ident,
        )
        prompt = cli.build_prompt(args, duration=12.0)
        request = cli.make_request(args, prompt)
        request.update(extra)
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(cli.enqueue_existing_request(self.root, request, prompt), 0)
        return cli.request_path(self.root, "pending", ident)

    def run_wait(self, **overrides: object) -> tuple[int, str, str]:
        values = dict(queue_dir=str(self.root), session=SESSION, task=None, timeout=None, poll_interval=0.05)
        values.update(overrides)
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            status = claude_code.wait(argparse.Namespace(**values))
        return status, stdout.getvalue(), stderr.getvalue()

    def ack(self, ident: str) -> None:
        with contextlib.redirect_stderr(io.StringIO()):
            cli.ack(argparse.Namespace(queue_dir=str(self.root), id=ident, message=None))

    def test_watcher_registration_is_visible_only_for_claude_and_stale_files_are_cleaned(self) -> None:
        claude = {"agent": "claude", "target": {"kind": "session", "value": SESSION}}
        codex = dict(claude, agent="codex")
        self.assertFalse(claude_code.live_watcher_is_held(claude))
        watcher = claude_code.register_live_watcher(SESSION)
        self.assertTrue(claude_code.live_watcher_is_held(claude))
        self.assertFalse(claude_code.live_watcher_is_held(codex))
        claude_code.release_live_watcher(watcher)
        self.assertFalse(claude_code.live_watcher_is_held(claude))
        stale = claude_code.live_watcher_dir(SESSION) / "999-dead.lock"
        stale.write_text("", encoding="utf-8")
        self.assertFalse(claude_code.live_watcher_is_held(claude))
        self.assertFalse(stale.exists())

    def test_daemon_leaves_only_claude_callbacks_to_a_live_waiter(self) -> None:
        claude = self.enqueue("cb-claude")
        codex = self.enqueue("cb-codex", agent="codex")
        watcher = claude_code.register_live_watcher(SESSION)
        try:
            self.assertEqual(cli.select_pending(self.root, time.time()), codex)
            codex.unlink()
            self.assertIsNone(cli.select_pending(self.root, time.time()))
        finally:
            claude_code.release_live_watcher(watcher)
        self.assertEqual(cli.select_pending(self.root, time.time()), claude)

    def test_wait_refuses_outside_claude_code_and_never_claims_other_agents(self) -> None:
        self.enqueue("cb-codex", agent="codex")
        with mock.patch.dict(os.environ, {"CODEX_THREAD_ID": SESSION}):
            os.environ.pop("CLAUDE_CODE_SESSION_ID", None)
            status, output, errors = self.run_wait(session=None)
        self.assertEqual((status, output), (2, ""))
        self.assertIn("only for Claude Code", errors)
        self.assertEqual(self.run_wait(timeout=0.2)[0], 3)
        self.assertTrue(cli.request_path(self.root, "pending", "cb-codex").exists())

    def test_wait_prints_callback_defers_daemon_and_ack_finishes_it(self) -> None:
        self.enqueue("cb-other", session="another-session")
        self.enqueue("cb-1")
        status, output, _ = self.run_wait()
        self.assertEqual(status, 0)
        self.assertIn("[long-task-callback] cb-1", output)
        self.assertIn("ack", output)
        self.assertNotIn("cb-other", output)

        pending = cli.request_path(self.root, "pending", "cb-1")
        request = json.loads(pending.read_text(encoding="utf-8"))
        self.assertGreater(request["next_attempt_at"], time.time() + 1000)
        self.assertEqual(cli.select_pending(self.root, time.time()).stem, "cb-other")
        self.assertFalse(any((self.root / "running").glob("*.json")))

        self.ack("cb-1")
        self.assertTrue(cli.request_path(self.root, "done", "cb-1").exists())
        self.assertFalse(pending.exists())

    def test_unacknowledged_callback_comes_back_until_acked(self) -> None:
        self.enqueue("cb-1")
        self.assertEqual(self.run_wait()[0], 0)
        status, output, _ = self.run_wait()
        self.assertEqual(status, 0)
        self.assertIn("cb-1", output)
        self.ack("cb-1")
        self.assertEqual(self.run_wait(timeout=0.2)[0], 3)

    def test_wait_filters_by_task_and_reports_acknowledged_task(self) -> None:
        cli.managed_task_dir(self.root, "abcd1234").mkdir(parents=True)
        cli.managed_task_path(self.root, "abcd1234").write_text("{}", encoding="utf-8")
        self.enqueue("cb-unrelated", managed_task_id="ffff0000")
        self.enqueue("cb-task", managed_task_id="abcd1234")
        status, output, _ = self.run_wait(task="abcd1234")
        self.assertEqual(status, 0)
        self.assertIn("cb-task", output)
        self.assertNotIn("cb-unrelated", output)

        self.ack("cb-task")
        status, output, errors = self.run_wait(task="abcd1234")
        self.assertEqual((status, output), (0, ""))
        self.assertIn("already acknowledged", errors)
        self.assertEqual(self.run_wait(task="00000000")[0], 2)

    def test_wait_recovers_a_callback_whose_headless_retries_failed(self) -> None:
        cli.move_request(self.enqueue("cb-failed"), self.root / "failed")
        status, output, _ = self.run_wait()
        self.assertEqual(status, 0)
        self.assertIn("cb-failed", output)
        self.assertTrue(cli.request_path(self.root, "pending", "cb-failed").exists())

    def test_wait_skips_callbacks_the_daemon_is_delivering(self) -> None:
        self.enqueue("cb-busy")
        target_lock = cli.acquire_target_lock({"target": {"kind": "session", "value": SESSION}}, blocking=False)
        try:
            self.assertEqual(self.run_wait(timeout=0.2)[0], 3)
        finally:
            cli.release_owner_lock(target_lock, remove=False)
        self.assertEqual(self.run_wait()[0], 0)

    def test_background_waiter_process_wakes_when_callback_arrives(self) -> None:
        environment = dict(os.environ, PYTHONPATH=SRC + os.pathsep + os.environ.get("PYTHONPATH", ""),
                           CLAUDE_CODE_SESSION_ID=SESSION, CLAUDECODE="1")
        process = subprocess.Popen(
            [sys.executable, "-m", "long_task_callback", "wait", "--queue-dir", str(self.root),
             "--poll-interval", "0.05", "--timeout", "30"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=environment,
        )
        request = {"agent": "claude", "target": {"kind": "session", "value": SESSION}}
        try:
            deadline = time.monotonic() + 10
            while not claude_code.live_watcher_is_held(request):
                if process.poll() is not None:
                    self.fail("waiter exited early: " + process.communicate()[1])
                self.assertLess(time.monotonic(), deadline, "waiter never registered")
                time.sleep(0.05)
            self.enqueue("cb-async")
            self.assertIsNone(cli.select_pending(self.root, time.time()))
            stdout, stderr = process.communicate(timeout=20)
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate()
        self.assertEqual(process.returncode, 0, stderr)
        self.assertIn("[long-task-callback] cb-async", stdout)
        self.assertFalse(claude_code.live_watcher_is_held(request))


class ControlCommandTests(unittest.TestCase):
    def test_ack_command_is_runnable_without_ltc_on_path(self) -> None:
        with mock.patch.object(cli, "console_script_path", return_value=None):
            command = cli.control_command("ack", "--id", "x")
        self.assertEqual(command.split()[:2], [sys.executable, str(Path(cli.__file__).with_name("_entry.py"))])
        self.assertTrue(command.endswith("ack --id x"))

if __name__ == "__main__":
    unittest.main()
