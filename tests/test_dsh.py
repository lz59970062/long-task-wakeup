"""DeepSeek Harness integration contracts: child work, binding and live callbacks."""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from long_task_callback import cli
from long_task_callback import claude_code as live_session
from long_task_callback import dsh_code
from long_task_callback.agents import AGENTS, CHILD_AGENT_NAMES, ChildOptions, get_agent, get_child_agent
from long_task_callback import diagnostics


class DshAdapterTests(unittest.TestCase):
    def options(self, **overrides):
        values = dict(cwd="/work", result_path=Path("/results/report.md"),
                      sandbox_mode="workspace-write", permission_mode="auto")
        values.update(overrides)
        return ChildOptions(**values)

    def test_registered_as_child_worker_and_callback_parent(self):
        self.assertIn("dsh", AGENTS.names)
        self.assertIn("dsh", CHILD_AGENT_NAMES)
        self.assertIs(get_agent("dsh"), get_child_agent("dsh"))

    def test_detection_uses_the_injected_shell_marker_and_outranks_parent_markers(self):
        for environment in (
            {"DSH_SESSION_ID": "session-abc"},
            {"DSH_SHELL": "1"},
            {"DSH_SESSION_ID": "session-abc", "CODEX_THREAD_ID": "codex"},
            {"DSH_SHELL": "true", "CLAUDE_CODE_SESSION_ID": "claude", "PI_SESSION_ID": "pi"},
        ):
            with self.subTest(environment=environment):
                self.assertEqual(AGENTS.detect(environment), "dsh")
                with mock.patch.dict(os.environ, environment, clear=True):
                    self.assertEqual(cli.resolve_agent(), "dsh")
        self.assertEqual(cli.resolve_agent(argparse.Namespace(agent="dsh")), "dsh")

    def test_parent_markers_are_stripped_from_dsh_children_but_credentials_survive(self):
        environment = {
            "DSH_SESSION_ID": "session-parent", "DSH_SHELL": "1", "DSH_WEB_URL": "http://127.0.0.1:1",
            "DSH_PROFILE": "desktop", "DSH_HOME": "/home/user/.dsh",
            "DEEPSEEK_API_KEY": "fixture-key", "PATH": "/fixture/bin",
        }
        before = dict(environment)
        child = AGENTS.child_environment(environment)
        self.assertEqual(environment, before)
        self.assertEqual(child, {
            "DSH_PROFILE": "desktop", "DSH_HOME": "/home/user/.dsh",
            "DEEPSEEK_API_KEY": "fixture-key", "PATH": "/fixture/bin",
        })

    def test_child_command_is_a_fresh_headless_run_with_the_prompt_on_stdin(self):
        environment = {"LONG_TASK_WAKEUP_DSH_BIN": "/opt/DeepSeek Harness/dsh"}
        before = dict(environment)
        command = get_child_agent("dsh").child_command(self.options(), environment)
        self.assertEqual(command, ["/opt/DeepSeek Harness/dsh", "--profile", "headless"])
        self.assertEqual(environment, before)
        for unsupported in ({"model": "deepseek-chat"}, {"reasoning_effort": "high"},
                            {"system_prompt_path": Path("/prompt.md")}):
            with self.subTest(unsupported=unsupported), self.assertRaises(ValueError):
                get_child_agent("dsh").child_command(self.options(**unsupported), environment)

    def test_there_is_no_headless_callback_resume(self):
        adapter = get_agent("dsh")
        self.assertEqual(adapter.callback_delivery, "live")
        with self.assertRaises(ValueError) as caught:
            adapter.resume_command({"target": {"kind": "session", "value": "session-abc"}}, {})
        self.assertIn("live session", str(caught.exception))
        with self.assertRaises(ValueError):
            adapter.resume_command({"target": {"kind": "session", "value": ""}}, {})
        self.assertTrue(cli.is_live_delivery({"agent": "dsh"}))
        self.assertFalse(cli.is_live_delivery({"agent": "claude"}))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "abc.json"
            path.write_text(json.dumps({
                "version": 2, "id": "abc", "agent": "dsh",
                "target": {"kind": "session", "value": "session-abc"},
                "cwd": "/work", "prompt": "wake", "callback_mode": "cli",
            }), encoding="utf-8")
            self.assertEqual(cli.load_request(path)["agent"], "dsh")

    def test_binding_and_acknowledgement_name_the_live_session(self):
        args = argparse.Namespace(
            cwd="/work", session=None, last=False, agent=None, task="train",
            command=None, exit_code=None, message=None, callback_format="compact",
            approvals_reviewer="auto_review", approval_policy="on-request",
            sandbox_mode="workspace-write", callback_mode="auto",
        )
        with mock.patch.dict(os.environ, {"DSH_SESSION_ID": "session-abc"}, clear=True):
            request = cli.make_request(args, "wake up")
        self.assertEqual(request["agent"], "dsh")
        self.assertEqual(request["target"], {"kind": "session", "value": "session-abc"})
        note = cli.build_acknowledgement_text("ltc ack --id abc", agent="dsh")
        self.assertIn("live DeepSeek Harness session", note)
        self.assertIn("no headless resume", note)


class DshSessionTests(unittest.TestCase):
    def test_projection_cache_supplies_the_starting_directory(self):
        with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as started:
            cache = Path(home) / "storages" / "session_projcache" / "sessions" / "session-abc.json"
            cache.parent.mkdir(parents=True)
            cache.write_text(json.dumps({"record": {"identity": {"cwd": started}}}), encoding="utf-8")
            self.assertEqual(dsh_code.session_cwd("session-abc", {"DSH_HOME": home}), started)
            self.assertTrue(dsh_code.session_exists("session-abc", {"DSH_HOME": home}))
            self.assertIsNone(dsh_code.session_cwd("../escape", {"DSH_HOME": home}))
            self.assertIsNone(dsh_code.session_cwd("session-missing", {"DSH_HOME": home}))

    def test_launcher_discovery_prefers_configuration_then_path(self):
        found = dsh_code.discover_executable(
            {"LONG_TASK_WAKEUP_DSH_BIN": "/custom/dsh"}, which=lambda name: None)
        self.assertEqual(found.path, "/custom/dsh")
        found = dsh_code.discover_executable({}, which=lambda name: "/usr/bin/dsh")
        self.assertEqual((found.path, found.source), ("/usr/bin/dsh", "path"))
        with mock.patch.object(dsh_code, "STANDALONE_LOCATIONS", ()):
            self.assertIsNone(dsh_code.discover_executable({}, which=lambda name: None))


class DshLiveWaitTests(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.TemporaryDirectory()
        self.addCleanup(self.home.cleanup)
        self.root = Path(self.home.name) / "queue"
        cli.ensure_daemon_dirs(self.root)
        patcher = mock.patch.dict(os.environ, {
            "DSH_HOME": str(Path(self.home.name) / ".dsh"),
            "LTC_HOME": str(Path(self.home.name) / ".ltc"),
            "DSH_SESSION_ID": "session-abc",
        }, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def write_request(self, callback_id="task-c1"):
        request = {
            "version": 2,
            "id": callback_id,
            "callback_mode": "cli",
            "agent": "dsh",
            "target": {"kind": "session", "value": "session-abc"},
            "prompt": "[long-task-callback] callback",
            "prompt_format": "full",
            "task": "train",
            "cwd": "/work",
        }
        path = self.root / "pending" / f"{callback_id}.json"
        path.write_text(json.dumps(request), encoding="utf-8")
        return path

    def test_live_delivery_skips_the_daemon_and_a_wait_claims_the_callback(self):
        self.write_request()
        request = json.loads((self.root / "pending" / "task-c1.json").read_text())
        self.assertIsNone(cli.select_pending(self.root, 10**12))
        self.assertFalse(live_session.live_watcher_is_held(request))

        args = argparse.Namespace(queue_dir=str(self.root), task=None, session=None,
                                  agent=None, timeout=5, poll_interval=0.05)
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(io.StringIO()):
            code = live_session.wait(args)
        self.assertEqual(code, 0)
        self.assertIn("[long-task-callback] callback", output.getvalue())
        self.assertTrue((self.root / "pending" / "task-c1.json").exists())

    def test_a_live_watcher_blocks_daemon_selection(self):
        request = json.loads(self.write_request().read_text())
        watcher = live_session.register_live_watcher("session-abc", agent="dsh")
        self.addCleanup(live_session.release_live_watcher, watcher)
        self.assertTrue(live_session.live_watcher_is_held(request))

    def test_launcher_paths_are_isolated_per_agent(self):
        claude_dir = live_session.live_watcher_dir("shared", agent="claude")
        dsh_dir = live_session.live_watcher_dir("shared", agent="dsh")
        self.assertNotEqual(claude_dir, dsh_dir)


class DshInstallTests(unittest.TestCase):
    def test_setup_installs_the_dsh_skill_under_dsh_home(self):
        with tempfile.TemporaryDirectory() as home:
            with mock.patch.dict(os.environ, {"DSH_HOME": home}, clear=True):
                code = cli.install_skill(
                    argparse.Namespace(path=None, target="dsh", force=False, keep_existing=False))
                self.assertEqual(code, 0)
                skill = Path(home) / "skills" / "long-task-callback" / "SKILL.md"
                self.assertTrue(skill.is_file())
                text = skill.read_text(encoding="utf-8")
                self.assertIn("DeepSeek Harness", text)
                self.assertIn("ltc wait", text)
                self.assertTrue((skill.parent / "REFERENCE.md").is_file())

    def test_doctor_describes_live_only_delivery(self):
        report = diagnostics.dsh_profile("/usr/local/bin/dsh")
        self.assertEqual(report["executable"], "/usr/local/bin/dsh")
        self.assertIn("background job", report["live_callback"])
        self.assertIn("unavailable", report["headless_fallback"])


if __name__ == "__main__":
    unittest.main()
