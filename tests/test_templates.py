"""Contract checks for preset authoring; written independently and run by parent."""

import argparse
import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import textwrap
import unittest
from unittest import mock

from long_task_callback import cli


class TemplateContractTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cwd = Path(self.tmp.name)
        self.queue = self.cwd / "test-queue"
        template_env = mock.patch.dict(os.environ, {"LTC_TEMPLATE_DIR": str(self.cwd / "templates")})
        template_env.start()
        self.addCleanup(template_env.stop)

    def invoke(self, worker="codex", options=(), prompt="Reject negative amounts; accept zero."):
        argv = ["ltc", "agent", worker, "--cwd", str(self.cwd),
                "--queue-dir", str(self.queue), "--session", "parent-session",
                "--agent", "claude", "--task", "independent contract tests",
                *options, "--", prompt]
        with mock.patch.object(sys, "argv", argv), \
                mock.patch.object(cli, "screen_binary", return_value="/usr/bin/screen"), \
                contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            return cli.main()

    def task(self):
        paths = list(self.queue.glob("tasks/*/task.json"))
        self.assertEqual(len(paths), 1)
        return json.loads(paths[0].read_text(encoding="utf-8"))

    def test_codex_test_defaults_are_frozen_with_original_requirements(self):
        requirements = "Reject negative amounts; accept zero.\nDo not round silently."
        self.assertEqual(self.invoke(options=["--template", "test"], prompt=requirements), 0)
        task = self.task()
        self.assertEqual(task["agent_template"], "test")
        self.assertEqual(task["agent_template_version"], 1)
        self.assertEqual(task["child_model"], "gpt-5.6-luna")
        self.assertEqual(task["child_reasoning_effort"], "max")
        self.assertEqual(task["agent"], "claude")
        self.assertEqual(task["target"], {"kind": "session", "value": "parent-session"})
        command = task["wrapped_command"]
        self.assertEqual(command[command.index("--model") + 1], "gpt-5.6-luna")
        self.assertIn('model_reasoning_effort="max"', command)
        prompt = Path(task["agent_prompt_path"]).read_text(encoding="utf-8")
        self.assertIn(requirements, prompt)
        self.assertIn("Do not run tests", prompt)
        self.assertIn("requirements", prompt)
        self.assertGreater(len(prompt), len(requirements))

    def test_explicit_model_and_effort_override_profile_without_changing_parent(self):
        self.assertEqual(self.invoke(options=["--template", "test", "--model", "custom-model",
                                              "--reasoning-effort", "high"]), 0)
        task = self.task()
        self.assertEqual(task["child_model"], "custom-model")
        self.assertEqual(task["child_reasoning_effort"], "high")
        self.assertEqual(task["agent"], "claude")
        self.assertIn("custom-model", task["wrapped_command"])
        self.assertIn('model_reasoning_effort="high"', task["wrapped_command"])
        self.assertNotIn("gpt-5.6-luna", task["wrapped_command"])

    def test_generic_prompt_beginning_with_test_is_not_a_template(self):
        prompt = "test this wording --model stays part of the prompt"
        self.assertEqual(self.invoke(prompt=prompt), 0)
        task = self.task()
        self.assertIsNone(task.get("agent_template"))
        self.assertIsNone(task.get("child_model"))
        self.assertIsNone(task.get("child_reasoning_effort"))
        self.assertNotIn("--model", task["wrapped_command"])
        self.assertNotIn("-c", task["wrapped_command"])
        self.assertEqual(Path(task["agent_prompt_path"]).read_text(encoding="utf-8"), prompt)

    def test_claude_template_inherits_cli_model_and_does_not_receive_codex_effort(self):
        self.assertEqual(self.invoke(worker="claude", options=["--template", "test"]), 0)
        task = self.task()
        self.assertIsNone(task.get("child_model"))
        self.assertIsNone(task.get("child_reasoning_effort"))
        self.assertNotIn("--model", task["wrapped_command"])
        self.assertNotIn("-c", task["wrapped_command"])

    def test_claude_explicit_model_is_used_without_codex_defaults(self):
        self.assertEqual(self.invoke(worker="claude", options=["--template", "test", "--model", "sonnet"]), 0)
        task = self.task()
        self.assertEqual(task["child_model"], "sonnet")
        command = task["wrapped_command"]
        self.assertEqual(command[command.index("--model") + 1], "sonnet")
        self.assertNotIn("gpt-5.6-luna", command)

    def test_invalid_requests_fail_before_persisting_a_task(self):
        cases = [("codex", ["--template", "unknown"], "requirements"),
                 ("codex", ["--template", "test"], "  \n"),
                 ("claude", ["--template", "test", "--reasoning-effort", "high"], "requirements"),
                 ("codex", ["--model", " "], "requirements")]
        for worker, options, prompt in cases:
            with self.subTest(worker=worker, options=options, prompt=prompt):
                with self.assertRaises(SystemExit):
                    self.invoke(worker=worker, options=options, prompt=prompt)
                self.assertEqual(list(self.queue.glob("tasks/*/task.json")), [])

    def test_dry_run_creates_no_queue(self):
        self.assertEqual(self.invoke(options=["--template", "test", "--dry-run"]), 0)
        self.assertFalse(self.queue.exists())

    def test_callback_keeps_authoring_separate_from_execution_even_on_failure(self):
        self.assertEqual(self.invoke(options=["--template", "test"]), 0)
        original = self.task()
        for exit_code in (0, 7):
            with self.subTest(exit_code=exit_code):
                task = dict(original, state="completed", outcome="completed", exit_code=exit_code)
                prompt = cli.managed_task_prompt(task, outcome="completed")
                request = cli.managed_callback_request(task, prompt)
                self.assertEqual(request["exit_code"], exit_code)
                self.assertEqual(request["agent_template"], "test")
                self.assertEqual(request["agent_template_version"], 1)
                self.assertEqual(request["child_model"], "gpt-5.6-luna")
                self.assertEqual(request["child_reasoning_effort"], "max")
                self.assertIn("NOT a test execution result", prompt)
                self.assertIn("parent agent", prompt)
                self.assertIn("execute the tests", prompt)
                self.assertIn("report is empty", prompt)
                self.assertIn("before running tests", prompt)
                self.assertIn(f"exit code: {exit_code}", prompt)

    def test_worker_uses_frozen_profile_and_routes_success_or_failure_to_parent(self):
        fake_child = self.cwd / "fake-codex"
        fake_child.write_text(textwrap.dedent(f"""\
            #!{sys.executable}
            import json
            import os
            from pathlib import Path
            import sys

            payload = {{"argv": sys.argv[1:], "prompt": sys.stdin.read()}}
            Path(os.environ["LTC_TEST_CAPTURE"]).write_text(json.dumps(payload))
            code = int(os.environ["LTC_TEST_EXIT"])
            if code == 0:
                Path(sys.argv[sys.argv.index("-o") + 1]).write_text("Tests have NOT been run. Authored.")
            sys.exit(code)
            """), encoding="utf-8")
        fake_child.chmod(0o700)
        for exit_code in (0, 7):
            with self.subTest(exit_code=exit_code):
                self.queue = self.cwd / f"worker-queue-{exit_code}"
                capture = self.cwd / f"capture-{exit_code}.json"
                environment = {"CODEX_LONG_TASK_WAKEUP_CODEX_BIN": str(fake_child),
                               "LTC_TEST_CAPTURE": str(capture), "LTC_TEST_EXIT": str(exit_code)}
                with mock.patch.dict(os.environ, environment):
                    self.assertEqual(self.invoke(options=["--template", "test", "--model", "contract-model",
                                                          "--reasoning-effort", "high"]), 0)
                task = self.task()
                task_path = self.queue / "tasks" / task["id"] / "task.json"
                submitted_prompt = Path(task["agent_prompt_path"]).read_text(encoding="utf-8")
                task["state"] = "launching"
                cli.write_managed_task(self.queue, task)
                worker = argparse.Namespace(task_file=str(task_path), token=task["token"])
                # A queued job must use its snapshot even if installed defaults change.
                with mock.patch.dict(os.environ, {"STY": "test-screen"}, clear=True), \
                        mock.patch.dict(cli.TEMPLATES, {"test": {"version": 999,
                                                               "codex_model": "changed-model",
                                                               "codex_effort": "low"}}):
                    self.assertEqual(cli.run_screen_worker(worker), exit_code)
                received = json.loads(capture.read_text(encoding="utf-8"))
                self.assertEqual(received["prompt"], submitted_prompt)
                argv = received["argv"]
                self.assertEqual(argv[argv.index("--model") + 1], "contract-model")
                self.assertIn('model_reasoning_effort="high"', argv)
                completed = json.loads(task_path.read_text(encoding="utf-8"))
                self.assertEqual(completed["state"], "completed")
                self.assertEqual(completed["exit_code"], exit_code)
                report = Path(completed["agent_result_path"]).read_text(encoding="utf-8")
                self.assertEqual(bool(report), exit_code == 0)
                callback = json.loads((self.queue / "pending" / f"{task['id']}.json").read_text(encoding="utf-8"))
                self.assertEqual(callback["target"], {"kind": "session", "value": "parent-session"})
                self.assertEqual(callback["agent"], "claude")
                self.assertEqual(callback["exit_code"], exit_code)
                self.assertEqual(callback["agent_template"], "test")
                self.assertEqual(callback["agent_template_version"], 1)
                self.assertEqual(callback["child_model"], "contract-model")
                self.assertEqual(callback["child_reasoning_effort"], "high")


if __name__ == "__main__":
    unittest.main()
