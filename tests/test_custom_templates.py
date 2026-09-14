"""Independent custom-template contract checks. Execution belongs to the parent."""
import argparse
import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

from long_task_callback import cli


class CustomTemplateTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.cwd = Path(temp.name)
        self.queue = self.cwd / "isolated-queue"
        self.directory = self.cwd / "profiles"
        self.directory.mkdir()
        env = mock.patch.dict(os.environ, {"LTC_TEMPLATE_DIR": str(self.directory)})
        env.start()
        self.addCleanup(env.stop)

    def write(self, body="version: 2\nprompt: Inspect the published contract.\n", name="review.yaml"):
        path = self.directory / name
        path.write_text(body, encoding="utf-8")
        return path

    def invoke(self, options=(), worker="codex", requirements="Reject negative balances."):
        output = io.StringIO()
        argv = ["ltc", "agent", worker, "--cwd", str(self.cwd), "--queue-dir", str(self.queue),
                "--agent", "claude", "--session", "custom-parent", "--task", "review contract",
                *options, "--", requirements]
        with mock.patch.object(sys, "argv", argv), \
                mock.patch.object(cli, "screen_binary", return_value="/usr/bin/screen"), \
                contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            code = cli.main()
        return code, output.getvalue()

    def task(self):
        paths = list(self.queue.glob("tasks/*/task.json"))
        self.assertEqual(len(paths), 1)
        return json.loads(paths[0].read_text())

    def assert_rejected(self, options, **kwargs):
        try:
            result, _ = self.invoke(options, **kwargs)
        except SystemExit as exc:
            self.assertNotEqual(exc.code, 0)
        else:
            self.assertNotEqual(result, 0)
        self.assertFalse(list(self.queue.glob("tasks/*/task.json")))

    def test_named_profile_persists_source_handoff_and_worker_defaults(self):
        path = self.write("version: 3\nprompt: Review the public API contract.\n"
                          "codex:\n  model: custom-codex\n  reasoning_effort: high\n"
                          "claude:\n  model: custom-claude\n"
                          "handoff: Parent must inspect the resulting report.\n")
        self.assertEqual(self.invoke(["--template", "review"])[0], 0)
        task = self.task()
        self.assertEqual(task["agent_template"], "review")
        self.assertEqual(task["agent_template_version"], 3)
        self.assertEqual(task["agent_template_source"], str(path.resolve()))
        self.assertEqual(task["agent_template_handoff"], "Parent must inspect the resulting report.")
        self.assertEqual(task["child_model"], "custom-codex")
        self.assertEqual(task["child_reasoning_effort"], "high")
        prompt = Path(task["agent_prompt_path"]).read_text()
        self.assertIn("Review the public API contract.", prompt)
        self.assertIn("Reject negative balances.", prompt)

    def test_cli_overrides_profile_but_worker_defaults_do_not_leak(self):
        self.write("version: 1\nprompt: Review.\ncodex:\n  model: codex-profile\n  reasoning_effort: max\n")
        for worker, options, model, effort in [
            ("codex", ["--model", "override", "--reasoning-effort", "low"], "override", "low"),
            ("claude", [], None, None),
            ("claude", ["--model", "claude-override"], "claude-override", None),
        ]:
            with self.subTest(worker=worker, options=options):
                self.queue = self.cwd / ("q-" + worker + "-" + str(len(options)))
                self.assertEqual(self.invoke(["--template", "review", *options], worker=worker)[0], 0)
                task = self.task()
                self.assertEqual(task.get("child_model"), model)
                self.assertEqual(task.get("child_reasoning_effort"), effort)
                if model:
                    self.assertIn(model, task["wrapped_command"])
                else:
                    self.assertNotIn("--model", task["wrapped_command"])
        self.write("version: 1\nprompt: Review.\ncodex:\n  model: codex-profile\n"
                   "claude:\n  model: claude-profile\n")
        self.queue = self.cwd / "q-claude-profile-default"
        self.assertEqual(self.invoke(["--template", "review"], worker="claude")[0], 0)
        task = self.task()
        self.assertEqual(task["child_model"], "claude-profile")
        self.assertIsNone(task.get("child_reasoning_effort"))
        command = task["wrapped_command"]
        self.assertEqual(command[command.index("--model") + 1], "claude-profile")

    def test_explicit_file_uses_stem_and_accepts_yml(self):
        path = self.write(name="external.yml")
        self.assertEqual(self.invoke(["--template-file", str(path)])[0], 0)
        task = self.task()
        self.assertEqual(task["agent_template"], "external")
        self.assertEqual(task["agent_template_source"], str(path.resolve()))
        self.queue = self.cwd / "relative-file-queue"
        original_cwd = Path.cwd()
        try:
            os.chdir(self.directory)
            # --cwd remains self.cwd, different from the submitting process directory.
            self.assertEqual(self.invoke(["--template-file", "external.yml"])[0], 0)
        finally:
            os.chdir(original_cwd)
        self.assertEqual(self.task()["agent_template_source"], str(path.resolve()))

    def test_named_yml_and_xdg_lookup(self):
        config = self.cwd / "config"
        directory = config / "ltc" / "templates"
        directory.mkdir(parents=True)
        path = directory / "review.yml"
        path.write_text("version: 1\nprompt: XDG contract.\n")
        with mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": str(config)}):
            os.environ.pop("LTC_TEMPLATE_DIR", None)
            self.assertEqual(self.invoke(["--template", "review"])[0], 0)
        self.assertEqual(self.task()["agent_template_source"], str(path.resolve()))

    def test_user_test_override_has_no_builtin_handoff(self):
        path = self.write("version: 4\nprompt: Summarize contracts only.\nhandoff: Read the summary.\n", "test.yaml")
        self.assertEqual(self.invoke(["--template", "test"])[0], 0)
        task = self.task()
        self.assertEqual(task["agent_template_source"], str(path.resolve()))
        self.assertIsNone(task.get("child_model"))
        task.update(state="completed", outcome="completed", exit_code=0)
        prompt = cli.managed_task_prompt(task, outcome="completed")
        self.assertIn("Read the summary.", prompt)
        self.assertNotIn("NOT a test execution result", prompt)
        self.assertNotIn("execute the tests", prompt)

    def test_builtin_fallback_source_is_explicit(self):
        self.assertEqual(self.invoke(["--template", "test"])[0], 0)
        self.assertEqual(self.task()["agent_template_source"], "builtin:test")

    def test_ambiguous_suffixes_invalid_names_and_conflicting_selectors_fail(self):
        path = self.write()
        self.write(name="review.yml")
        for options in [["--template", "review"], ["--template", "../review"],
                        ["--template", "missing"], ["--template", "_review"],
                        ["--template", "review", "--template-file", str(path)]]:
            with self.subTest(options=options):
                self.assert_rejected(options)

    def test_invalid_yaml_overrides_fail_closed_without_builtin_fallback(self):
        invalid = ["[]", "version: true\nprompt: x", "version: 0\nprompt: x",
                   "version: 1", "version: 1\nprompt: ' '", "version: 1\nprompt: 123",
                   "version: 1\nprompt: x\nextra: y", "version: 1\nprompt: x\nprompt: y",
                   "version: 1\nprompt: x\ncodex:\n  model: a\n  model: b",
                   "version: 1\nprompt: x\ncodex:\n  model: ''",
                   "version: 1\nprompt: x\ncodex:\n  reasoning_effort: impossible",
                   "version: 1\nprompt: x\nclaude:\n  reasoning_effort: high",
                   "version: 1\nprompt: x\nhandoff: false",
                   "version: 1\nprompt: !!python/object/apply:builtins.str [unsafe]"]
        for body in invalid:
            with self.subTest(body=body):
                path = self.write(body, "test.yaml")
                self.assert_rejected(["--template", "test"])
                self.assert_rejected(["--template-file", str(path)])

    def test_dry_run_displays_resolved_contract_without_queue(self):
        path = self.write("version: 2\nprompt: CUSTOM PROMPT MARKER\n"
                          "codex:\n  model: custom-model\n  reasoning_effort: high\n"
                          "handoff: CUSTOM HANDOFF MARKER\n")
        code, output = self.invoke(["--template", "review", "--dry-run"])
        self.assertEqual(code, 0)
        for expected in [str(path.resolve()), "custom-model", "high", "CUSTOM PROMPT MARKER",
                         "CUSTOM HANDOFF MARKER", "Reject negative balances."]:
            self.assertIn(expected, output)
        self.assertFalse(self.queue.exists())
        self.assert_rejected(["--template", "review"], requirements=" ")

    def test_worker_and_callback_use_snapshot_after_custom_file_changes(self):
        path = self.write("version: 8\nprompt: ORIGINAL PROMPT\n"
                          "codex:\n  model: original-model\n  reasoning_effort: high\n"
                          "handoff: ORIGINAL HANDOFF\n")
        capture = self.cwd / "capture.json"
        child = self.cwd / "fake-child"
        child.write_text(f"#!{sys.executable}\nimport sys,json\nfrom pathlib import Path\n"
                         f"Path({str(capture)!r}).write_text(json.dumps({{'argv':sys.argv[1:],'prompt':sys.stdin.read()}}))\n"
                         "Path(sys.argv[sys.argv.index('-o')+1]).write_text('Authored report')\n")
        child.chmod(0o700)
        with mock.patch.dict(os.environ, {"CODEX_LONG_TASK_WAKEUP_CODEX_BIN": str(child)}):
            self.assertEqual(self.invoke(["--template", "review"])[0], 0)
        task = self.task()
        frozen_prompt = Path(task["agent_prompt_path"]).read_text()
        path.write_text("version: 9\nprompt: REPLACEMENT\nhandoff: REPLACEMENT\n")
        task["state"] = "launching"
        cli.write_managed_task(self.queue, task)
        task_path = self.queue / "tasks" / task["id"] / "task.json"
        with mock.patch.dict(os.environ, {"STY": "test-screen"}, clear=True):
            self.assertEqual(cli.run_screen_worker(argparse.Namespace(task_file=str(task_path), token=task["token"])), 0)
        received = json.loads(capture.read_text())
        self.assertEqual(received["prompt"], frozen_prompt)
        self.assertIn("original-model", received["argv"])
        self.assertIn('model_reasoning_effort="high"', received["argv"])
        callback = json.loads((self.queue / "pending" / f"{task['id']}.json").read_text())
        self.assertEqual(callback["agent_template_version"], 8)
        self.assertEqual(callback["agent_template_source"], str(path.resolve()))
        self.assertEqual(callback["agent_template_handoff"], "ORIGINAL HANDOFF")
        self.assertEqual(callback["target"], {"kind": "session", "value": "custom-parent"})
        self.assertEqual(callback["agent"], "claude")
        self.assertIn("ORIGINAL HANDOFF", callback["prompt"])
        self.assertNotIn("REPLACEMENT", callback["prompt"])


if __name__ == "__main__":
    unittest.main()
