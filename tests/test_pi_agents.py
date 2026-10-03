"""Independent Pi child-worker contracts; execution belongs to the parent.

The process fixtures call no model APIs and touch only temporary queues. They
exercise submission and the real private worker, including its durable result,
captured environment, text artifacts and callback routing.
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
import unittest
from unittest import mock

from long_task_callback import cli, diagnostics
from long_task_callback.agents import AGENTS, CHILD_AGENT_NAMES, ChildOptions, get_agent, get_child_agent
from long_task_callback.runtime import worker_command
from test_cli import assert_private_file, executable_python_fixture, patch_fixture_commands


PI_THINKING_LEVELS = ("off", "minimal", "low", "medium", "high", "xhigh", "max")
SESSION_MARKERS = (
    "CODEX_THREAD_ID", "CLAUDE_CODE_SESSION_ID", "CLAUDECODE",
    "CODEX_INTERNAL_ORIGINATOR_OVERRIDE", "PI_SESSION_ID", "PI_SESSION_FILE",
)


class PiAdapterContractTests(unittest.TestCase):
    def options(self, *, model=None, effort=None, system_prompt_path=None):
        return ChildOptions(
            cwd="/work with spaces", result_path=Path("/results/report with spaces.md"),
            sandbox_mode="workspace-write", permission_mode="auto", model=model,
            reasoning_effort=effort, system_prompt_path=system_prompt_path,
        )

    def test_pi_is_a_child_only_integration_and_never_detected_as_a_callback(self):
        self.assertEqual(set(AGENTS.names), {"codex", "claude"})
        self.assertEqual(set(CHILD_AGENT_NAMES), {"codex", "claude", "pi"})
        self.assertEqual(get_child_agent("pi").name, "pi")
        with self.assertRaises(ValueError):
            get_agent("pi")
        for environment, expected in (
            ({"PI_SESSION_ID": "pi-parent", "PI_SESSION_FILE": "/pi/session.jsonl"}, "codex"),
            ({"PI_SESSION_ID": "pi-parent", "CODEX_THREAD_ID": "codex-parent"}, "codex"),
            ({"PI_SESSION_ID": "pi-parent", "CLAUDE_CODE_SESSION_ID": "claude-parent"}, "claude"),
            ({"PI_PROVIDER": "fixture", "PI_MODEL": "fixture-model", "PI_REASONING_LEVEL": "high"}, "codex"),
        ):
            with self.subTest(environment=environment):
                self.assertEqual(AGENTS.detect(environment), expected)
                with mock.patch.dict(os.environ, environment, clear=True):
                    self.assertEqual(cli.resolve_agent(), expected)
        with self.assertRaises(SystemExit):
            cli.resolve_agent(argparse.Namespace(agent="pi"))

    def test_pi_command_is_pure_fresh_print_text_mode_with_no_positional_prompt(self):
        environment = {"LONG_TASK_WAKEUP_PI_BIN": "/opt/my Pi/bin/pi", "PI_PROVIDER": "fixture"}
        before = dict(environment)
        command = get_child_agent("pi").child_command(
            self.options(model="provider/model with spaces", effort="high"), environment
        )
        self.assertEqual(command, [
            "/opt/my Pi/bin/pi", "--print", "--mode", "text", "--no-session",
            "--model", "provider/model with spaces", "--thinking", "high",
        ])
        self.assertEqual(environment, before)
        self.assertEqual(get_child_agent("pi").child_command(self.options(), {}),
                         ["pi", "--print", "--mode", "text", "--no-session"])
        self.assertEqual(get_child_agent("pi").child_result_mode, "stdout")

    def test_every_supported_pi_thinking_level_maps_to_native_thinking_flag(self):
        for effort in PI_THINKING_LEVELS:
            with self.subTest(effort=effort):
                command = get_child_agent("pi").child_command(self.options(effort=effort), {})
                self.assertEqual(command, ["pi", "--print", "--mode", "text", "--no-session",
                                           "--thinking", effort])

    def test_pi_adapter_passes_the_system_prompt_as_a_separate_file_argument(self):
        snapshot = Path("/managed task/agent-system-prompt.md")
        command = get_child_agent("pi").child_command(self.options(system_prompt_path=snapshot), {})
        self.assertEqual(command, ["pi", "--print", "--mode", "text", "--no-session",
                                   "--system-prompt", str(snapshot)])
        self.assertNotIn("--append-system-prompt", command)

    def test_fresh_children_strip_all_session_markers_and_keep_pi_config_and_credentials(self):
        environment = {name: "inherited-parent-value" for name in SESSION_MARKERS}
        preserved = {
            "PI_CODING_AGENT_DIR": "/fixture/pi profile", "PI_PROVIDER": "custom-provider",
            "PI_MODEL": "custom-model", "PI_REASONING_LEVEL": "high",
            "OPENAI_API_KEY": "fixture-openai-key", "ANTHROPIC_API_KEY": "fixture-anthropic-key",
            "PATH": "/fixture/bin", "CUSTOM_WORKLOAD_SETTING": "preserve-me",
        }
        environment.update(preserved)
        before = dict(environment)
        self.assertEqual(cli.child_agent_environment(environment), preserved)
        self.assertEqual(environment, before)


class PiAgentContractTests(unittest.TestCase):
    def setUp(self):
        # Submission uses the screen contract in these portable fixtures. The
        # worker still uses native Windows storage/process support on Windows.
        if os.name == "nt":
            platform = mock.patch.object(sys, "platform", "linux")
            platform.start()
            self.addCleanup(platform.stop)
        patch_fixture_commands(self)
        temporary = tempfile.TemporaryDirectory(prefix="ltc Pi 中文 ")
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name).resolve()
        self.queue = self.directory / "queue with spaces"
        self.templates = self.directory / "templates"
        self.templates.mkdir()
        self.codex_home = self.directory / "codex-profile"
        self.claude_home = self.directory / "claude-profile"
        for home in (self.codex_home, self.claude_home):
            skill = home / "skills" / "long-task-callback" / "SKILL.md"
            skill.parent.mkdir(parents=True)
            skill.write_text("Fixture LTC skill", encoding="utf-8")
        environment = mock.patch.dict(os.environ, {
            "LTC_TEMPLATE_DIR": str(self.templates), "CODEX_HOME": str(self.codex_home),
            "CLAUDE_CONFIG_DIR": str(self.claude_home),
            cli.TARGET_LOCK_DIR_ENV: str(self.directory / "target-locks"),
            "PYTHONIOENCODING": "utf-8",
        })
        environment.start()
        self.addCleanup(environment.stop)

    def invoke(self, options=(), *, parent="codex", prompt="Inspect the public Pi child contract."):
        argv = ["ltc", "agent", "pi", "--cwd", str(self.directory),
                "--queue-dir", str(self.queue), "--backend", "screen", "--callback-mode", "cli",
                "--agent", parent, "--session", f"parent-{parent}", "--task", "Pi contract fixture",
                *options, "--", prompt]
        output = io.StringIO()
        with mock.patch.object(sys, "argv", argv), \
                mock.patch.object(cli, "screen_binary", return_value="/fixture/screen"), \
                mock.patch.object(diagnostics, "emit_if_needed"), \
                contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            result = cli.main()
        return result, output.getvalue()

    def task(self):
        paths = list(cli.managed_tasks_root(self.queue).glob("*/task.json"))
        self.assertEqual(len(paths), 1)
        return paths[0], cli.load_managed_task(paths[0])

    def assert_rejected(self, options=(), **kwargs):
        try:
            result, _ = self.invoke(options, **kwargs)
        except SystemExit as exc:
            self.assertNotEqual(exc.code, 0)
        else:
            self.assertNotEqual(result, 0)
        self.assertFalse(cli.managed_tasks_root(self.queue).exists())

    def write_template(self, body, name="review.yaml"):
        path = self.templates / name
        path.write_text(body, encoding="utf-8")
        return path

    def fake_pi(self):
        script = self.directory / "fake Pi child.py"
        script.write_text(textwrap.dedent(f"""\
            #!{sys.executable}
            import json
            import os
            from pathlib import Path
            import sys

            # Record process admission before reading any optional prompt file.
            with Path(os.environ['LTC_TEST_PI_EXECUTIONS']).open('a', encoding='utf-8') as stream:
                stream.write('executed\\n')
            names = {SESSION_MARKERS + ('PI_CODING_AGENT_DIR', 'PI_PROVIDER', 'PI_MODEL', 'PI_REASONING_LEVEL', 'OPENAI_API_KEY', 'CUSTOM_WORKLOAD_SETTING')!r}
            payload = {{'argv': sys.argv[1:], 'prompt': sys.stdin.read(), 'cwd': os.getcwd(),
                       'environment': {{name: os.environ.get(name) for name in names}}}}
            if '--system-prompt' in sys.argv:
                system_path = Path(sys.argv[sys.argv.index('--system-prompt') + 1])
                payload['system_prompt_path'] = str(system_path)
                payload['system_prompt'] = system_path.read_text(encoding='utf-8')
            else:
                payload['system_prompt_path'] = None
                payload['system_prompt'] = None
            Path(os.environ['LTC_TEST_PI_CAPTURE']).write_text(json.dumps(payload), encoding='utf-8')
            sys.stderr.write('fixture Pi diagnostic\\n')
            sys.stdout.write(os.environ['LTC_TEST_PI_REPORT'])
            raise SystemExit(int(os.environ['LTC_TEST_PI_EXIT']))
            """), encoding="utf-8")
        return executable_python_fixture(script)

    def fixture_environment(self, executable, capture, *, report, exit_code):
        return {
            "LONG_TASK_WAKEUP_PI_BIN": str(executable), "LTC_TEST_PI_CAPTURE": str(capture),
            "LTC_TEST_PI_EXECUTIONS": str(capture.with_suffix(".executions")),
            "LTC_TEST_PI_REPORT": report, "LTC_TEST_PI_EXIT": str(exit_code),
            "CODEX_THREAD_ID": "submitter-codex", "CLAUDE_CODE_SESSION_ID": "submitter-claude",
            "CLAUDECODE": "1", "CODEX_INTERNAL_ORIGINATOR_OVERRIDE": "submitter-desktop",
            "PI_SESSION_ID": "submitter-pi", "PI_SESSION_FILE": "/fixture/parent-session.jsonl",
            "PI_CODING_AGENT_DIR": str(self.directory / "Pi config with spaces"),
            "PI_PROVIDER": "custom-provider", "PI_MODEL": "custom-model", "PI_REASONING_LEVEL": "high",
            "OPENAI_API_KEY": "fixture-preserved-secret", "CUSTOM_WORKLOAD_SETTING": "submission-value",
        }

    def run_worker(self, path, task):
        task["state"] = "launching"
        cli.write_managed_task(self.queue, task)
        command = worker_command("_screen-worker", "--task-file", str(path), f"--token={task['token']}")
        # This is the log redirection normally supplied by the execution owner;
        # no actual screen session or coordinator is needed to run admission.
        environment = dict(os.environ, STY="fixture-screen", PYTHONIOENCODING="utf-8",
                           LONG_TASK_WAKEUP_PI_BIN="daemon-must-not-replace-frozen-executable",
                           PI_CODING_AGENT_DIR="daemon-must-not-replace-submitter-config",
                           CUSTOM_WORKLOAD_SETTING="daemon-must-not-leak")
        with Path(task["log_path"]).open("wb") as log:
            return subprocess.run(command, cwd=self.directory, env=environment,
                                  stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                                  check=False, timeout=15).returncode

    def assert_system_prompt_worker_rejected(self, path, task, capture):
        code = self.run_worker(path, task)
        self.assertNotEqual(code, 0, Path(task["log_path"]).read_text(encoding="utf-8"))
        self.assertFalse(capture.exists())
        self.assertFalse(capture.with_suffix(".executions").exists(), "Pi must not start before snapshot validation")
        completed = cli.load_managed_task(path)
        self.assertEqual((completed["state"], completed["outcome"]), ("interrupted", "unknown"))
        reason = completed["recovery_reason"]
        self.assertIn("system prompt", reason.lower().replace("_", " ").replace("-", " "))
        callbacks = list((self.queue / "pending").glob("*.json"))
        self.assertEqual(len(callbacks), 1)
        callback = cli.load_request(callbacks[0])
        self.assertEqual(callback["agent"], task["agent"])
        self.assertEqual(callback["target"], task["target"])
        self.assertEqual(callback["agent_worker"], "pi")
        self.assertEqual(callback["outcome"], "unknown")
        self.assertEqual(callback["recovery_reason"], reason)

    def test_public_submission_and_real_worker_preserve_text_environment_and_parent_routes(self):
        executable = self.fake_pi()
        for parent, exit_code, report in (
            ("codex", 0, "Pi result: 中文 ✓\nsecond line without a final newline"),
            ("claude", 7, "Partial Pi result\nfailed after useful evidence\n"),
        ):
            with self.subTest(parent=parent, exit_code=exit_code):
                self.queue = self.directory / f"worker-queue-{parent}"
                capture = self.directory / f"capture-{parent}.json"
                prompt = "Inspect --model literally in this prompt.\nPreserve 中文 and report evidence."
                environment = self.fixture_environment(executable, capture, report=report, exit_code=exit_code)
                with mock.patch.dict(os.environ, environment), mock.patch.object(cli.subprocess, "run") as process:
                    self.assertEqual(self.invoke(["--model", "fixture/provider-model", "--reasoning-effort", "high"],
                                                 parent=parent, prompt=prompt)[0], 0)
                    process.assert_not_called()
                path, task = self.task()
                self.assertEqual(task["state"], "submitted")
                self.assertEqual(task["agent_worker"], "pi")
                self.assertIsNone(task.get("agent_system_prompt_path"))
                self.assertIsNone(task.get("agent_system_prompt_source"))
                self.assertNotIn("--system-prompt", task["wrapped_command"])
                self.assertEqual(task["agent"], parent)
                self.assertEqual(task["target"], {"kind": "session", "value": f"parent-{parent}"})
                self.assertEqual(Path(task["agent_prompt_path"]).read_bytes(), prompt.encode("utf-8"))
                self.assertNotIn(prompt, path.read_text(encoding="utf-8"))
                assert_private_file(self, Path(task["agent_prompt_path"]))
                assert_private_file(self, Path(task["environment_path"]))
                self.assertEqual(self.run_worker(path, task), exit_code,
                                 Path(task["log_path"]).read_text(encoding="utf-8"))
                received = json.loads(capture.read_text(encoding="utf-8"))
                self.assertEqual(received["prompt"], prompt)
                self.assertIsNone(received["system_prompt"])
                self.assertEqual(received["cwd"], str(self.directory))
                self.assertEqual(received["argv"], ["--print", "--mode", "text", "--no-session",
                                                    "--model", "fixture/provider-model", "--thinking", "high"])
                for marker in SESSION_MARKERS:
                    self.assertIsNone(received["environment"][marker], marker)
                for name in ("PI_CODING_AGENT_DIR", "PI_PROVIDER", "PI_MODEL", "PI_REASONING_LEVEL",
                             "OPENAI_API_KEY", "CUSTOM_WORKLOAD_SETTING"):
                    self.assertEqual(received["environment"][name], environment[name], name)
                self.assertEqual(capture.with_suffix(".executions").read_text(encoding="utf-8"), "executed\n")
                completed = cli.load_managed_task(path)
                self.assertEqual((completed["state"], completed["outcome"], completed["exit_code"]),
                                 ("completed", "completed", exit_code))
                result_path = Path(completed["agent_result_path"])
                self.assertEqual(result_path.read_bytes(), report.encode("utf-8"))
                assert_private_file(self, result_path)
                self.assertFalse(Path(task["environment_path"]).exists())
                durable = json.loads(cli.managed_result_path(self.queue, task["id"]).read_text(encoding="utf-8"))
                self.assertEqual(durable["exit_code"], exit_code)
                log = Path(task["log_path"]).read_text(encoding="utf-8")
                self.assertIn(report, log)
                self.assertIn("fixture Pi diagnostic", log)
                self.assertNotIn("fixture-preserved-secret", log)
                pending = list((self.queue / "pending").glob("*.json"))
                self.assertEqual(len(pending), 1)
                callback = cli.load_request(pending[0])
                self.assertEqual(callback["agent"], parent)
                self.assertEqual(callback["agent_worker"], "pi")
                self.assertEqual(callback["target"], task["target"])
                self.assertEqual(callback["exit_code"], exit_code)
                self.assertEqual(callback["agent_result_path"], str(result_path))
                details = Path(callback["prompt_details_path"]).read_text(encoding="utf-8")
                self.assertIn("Child agent: PI Agent", details)
                self.assertIn(str(result_path), details)
                self.assertIn(task["log_path"], details)
                self.assertNotIn("fixture-preserved-secret", details)

    def test_empty_failed_stdout_is_saved_as_an_empty_report_without_losing_exit_status(self):
        executable = self.fake_pi()
        capture = self.directory / "empty-failed.json"
        environment = self.fixture_environment(executable, capture, report="", exit_code=1)
        with mock.patch.dict(os.environ, environment):
            self.assertEqual(self.invoke()[0], 0)
        path, task = self.task()
        self.assertEqual(self.run_worker(path, task), 1)
        self.assertEqual(Path(task["agent_result_path"]).read_bytes(), b"")
        self.assertEqual(cli.load_managed_task(path)["exit_code"], 1)
        callback = cli.load_request(self.queue / "pending" / f"{task['id']}.json")
        self.assertEqual(callback["exit_code"], 1)
        self.assertEqual(callback["agent"], "codex")

    def test_all_pi_cli_thinking_choices_are_supported_including_off_and_max(self):
        for effort in PI_THINKING_LEVELS:
            with self.subTest(effort=effort):
                self.queue = self.directory / f"effort-{effort}"
                self.assertEqual(self.invoke(["--reasoning-effort", effort])[0], 0)
                _, task = self.task()
                self.assertEqual(task["child_reasoning_effort"], effort)
                command = task["wrapped_command"]
                self.assertEqual(command[command.index("--thinking") + 1], effort)

    def test_invalid_cli_options_fail_before_creating_any_task_directory(self):
        for options, prompt in (
            (["--reasoning-effort", "ultra"], "requirements"),
            (["--reasoning-effort", "unknown"], "requirements"),
            (["--reasoning-effort", ""], "requirements"),
            (["--model", " "], "requirements"),
            ([], " \n "),
        ):
            with self.subTest(options=options, prompt=prompt):
                self.assert_rejected(options, prompt=prompt)

    def test_pi_cannot_be_selected_as_callback_parent_in_public_commands(self):
        for arguments in (
            ["run", "--", sys.executable, "-c", "pass"],
            ["done", "--exit-code", "0"],
            ["doctor", "--operation", "agent", "--agent-worker", "pi"],
            ["agent", "pi", "--", "inspect contract"],
        ):
            # Common options precede the remainder prompt or command.
            separator = arguments.index("--") if "--" in arguments else len(arguments)
            argv = ["ltc", *arguments[:separator], "--queue-dir", str(self.queue),
                    "--agent", "pi", "--session", "invalid-pi-parent", *arguments[separator:]]
            with self.subTest(arguments=arguments), mock.patch.object(sys, "argv", argv), \
                    contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as rejected:
                cli.main()
            self.assertNotEqual(rejected.exception.code, 0)
            self.assertFalse(cli.managed_tasks_root(self.queue).exists())

    def test_custom_pi_defaults_and_cli_overrides_do_not_leak_other_workers_settings(self):
        self.write_template("version: 3\nprompt: Review the published contract.\n"
                            "codex:\n  model: codex-only\n  reasoning_effort: ultra\n"
                            "claude:\n  model: claude-only\n"
                            "pi:\n  model: custom-provider/custom-model\n  reasoning_effort: max\n")
        for options, model, effort in (
            (["--template", "review"], "custom-provider/custom-model", "max"),
            (["--template", "review", "--model", "override/model", "--reasoning-effort", "off"],
             "override/model", "off"),
        ):
            with self.subTest(options=options):
                self.queue = self.directory / ("profile-default" if effort == "max" else "profile-override")
                self.assertEqual(self.invoke(options)[0], 0)
                _, task = self.task()
                self.assertEqual(task["child_model"], model)
                self.assertEqual(task["child_reasoning_effort"], effort)
                command = task["wrapped_command"]
                self.assertEqual(command[command.index("--model") + 1], model)
                self.assertEqual(command[command.index("--thinking") + 1], effort)
                self.assertNotIn("codex-only", command)
                self.assertNotIn("claude-only", command)

    def test_invalid_pi_template_settings_fail_closed_before_task_persistence(self):
        for settings in (
            "  reasoning_effort: ultra\n", "  reasoning_effort: impossible\n",
            "  reasoning_effort: ''\n", "  model: ' '\n", "  thinking: high\n",
        ):
            with self.subTest(settings=settings):
                path = self.write_template("version: 1\nprompt: Review.\npi:\n" + settings)
                self.assert_rejected(["--template", "review"])
                self.assert_rejected(["--template-file", str(path)])

    def test_custom_template_command_prompt_and_handoff_are_frozen_at_submission(self):
        template = self.write_template("version: 8\nprompt: ORIGINAL PI PROMPT\n"
                                       "pi:\n  model: original-provider/original-model\n  reasoning_effort: high\n"
                                       "handoff: ORIGINAL HANDOFF requires parent inspection.\n")
        executable = self.fake_pi()
        capture = self.directory / "frozen-profile.json"
        environment = self.fixture_environment(executable, capture, report="Frozen Pi report\n", exit_code=0)
        with mock.patch.dict(os.environ, environment):
            self.assertEqual(self.invoke(["--template-file", str(template)], parent="claude")[0], 0)
        path, task = self.task()
        submitted_prompt = Path(task["agent_prompt_path"]).read_text(encoding="utf-8")
        submitted_command = list(task["wrapped_command"])
        template.write_text("version: 9\nprompt: REPLACEMENT PROMPT\n"
                            "pi:\n  model: replacement/model\n  reasoning_effort: off\n"
                            "handoff: REPLACEMENT HANDOFF\n", encoding="utf-8")
        self.assertEqual(self.run_worker(path, task), 0)
        received = json.loads(capture.read_text(encoding="utf-8"))
        self.assertEqual(received["prompt"], submitted_prompt)
        self.assertEqual(received["argv"], ["--print", "--mode", "text", "--no-session",
                                            "--model", "original-provider/original-model", "--thinking", "high"])
        self.assertEqual(cli.load_managed_task(path)["wrapped_command"], submitted_command)
        callback = cli.load_request(self.queue / "pending" / f"{task['id']}.json")
        self.assertEqual(callback["agent_template"], "review")
        self.assertEqual(callback["agent_template_version"], 8)
        self.assertEqual(callback["agent_template_source"], str(template.resolve()))
        self.assertEqual(callback["child_model"], "original-provider/original-model")
        self.assertEqual(callback["child_reasoning_effort"], "high")
        self.assertEqual(callback["agent_template_handoff"], "ORIGINAL HANDOFF requires parent inspection.")
        self.assertEqual(callback["agent"], "claude")
        self.assertEqual(callback["target"], {"kind": "session", "value": "parent-claude"})
        details = Path(callback["prompt_details_path"]).read_text(encoding="utf-8")
        self.assertIn("ORIGINAL HANDOFF", details)
        self.assertNotIn("REPLACEMENT", details)

    def test_builtin_test_template_preserves_test_authoring_contract_without_codex_defaults(self):
        self.assertEqual(self.invoke(["--template", "test"])[0], 0)
        _, task = self.task()
        self.assertIsNone(task.get("child_model"))
        self.assertIsNone(task.get("child_reasoning_effort"))
        self.assertNotIn("--model", task["wrapped_command"])
        self.assertNotIn("--thinking", task["wrapped_command"])
        self.assertIn("Do not run tests", Path(task["agent_prompt_path"]).read_text(encoding="utf-8"))
        task.update(state="completed", outcome="completed", exit_code=7)
        handoff = cli.managed_task_prompt(task, outcome="completed")
        self.assertIn("NOT a test execution result", handoff)
        self.assertIn("execute the tests", handoff)
        self.assertIn("exit code: 7", handoff)

    def test_dry_run_resolves_pi_profile_without_a_queue_or_process(self):
        self.write_template("version: 2\nprompt: PI DRY RUN PROMPT\n"
                            "pi:\n  model: dry-provider/dry-model\n  reasoning_effort: xhigh\n"
                            "handoff: PI DRY RUN HANDOFF\n")
        with mock.patch.object(cli.subprocess, "run") as process:
            result, output = self.invoke(["--template", "review", "--dry-run"])
        self.assertEqual(result, 0)
        for expected in ("PI Agent", "dry-provider/dry-model", "xhigh", "PI DRY RUN PROMPT",
                         "PI DRY RUN HANDOFF", "Inspect the public Pi child contract."):
            self.assertIn(expected, output)
        process.assert_not_called()
        self.assertFalse(self.queue.exists())

    def test_cli_system_prompt_uses_the_submitting_directory_and_a_private_literal_snapshot(self):
        submitter = self.directory / "submitting shell"
        child = self.directory / "different child cwd"
        submitter.mkdir()
        child.mkdir()
        source = submitter / "Pi role 中文.md"
        system_text = "Use 中文 instructions.\nKeep $PI_MODEL, ${HOME}, $(echo literal), and {{role}} literal.\n"
        # A valid UTF-8 BOM is removed as Pi does; all remaining text is literal.
        source.write_bytes(b"\xef\xbb\xbf" + system_text.encode("utf-8"))
        user_prompt = "This is the user task, separate from the Pi system instructions."
        original_cwd = Path.cwd()
        try:
            os.chdir(submitter)
            with mock.patch.object(cli.subprocess, "run") as process:
                self.assertEqual(self.invoke(["--cwd", str(child), "--system-prompt-file", source.name],
                                             prompt=user_prompt)[0], 0)
                process.assert_not_called()
        finally:
            os.chdir(original_cwd)
        path, task = self.task()
        snapshot = Path(task["agent_system_prompt_path"])
        self.assertEqual(task["agent_system_prompt_source"], str(source.resolve()))
        self.assertEqual(snapshot, path.parent / "agent-system-prompt.md")
        self.assertNotEqual(snapshot, source)
        self.assertEqual(snapshot.read_bytes(), system_text.encode("utf-8"))
        assert_private_file(self, snapshot)
        command = task["wrapped_command"]
        self.assertEqual(command[command.index("--system-prompt") + 1], str(snapshot))
        self.assertNotIn(str(source), command)
        self.assertNotIn(system_text, path.read_text(encoding="utf-8"))
        self.assertEqual(Path(task["agent_prompt_path"]).read_text(encoding="utf-8"), user_prompt)
        self.assertEqual(task["cwd"], str(child.resolve()))

    def test_yaml_system_prompt_is_relative_to_the_template_and_cli_file_overrides_it(self):
        prompts = self.templates / "prompts"
        prompts.mkdir()
        default = prompts / "Pi role.md"
        default.write_text("System instructions supplied by the YAML profile.\n", encoding="utf-8")
        template = self.write_template("version: 4\nprompt: YAML user-task instructions.\n"
                                       "pi:\n  system_prompt_file: prompts/Pi role.md\n")
        submitter = self.directory / "submission directory"
        child = self.directory / "child directory"
        submitter.mkdir()
        child.mkdir()
        override = submitter / "CLI override.md"
        override.write_text("System instructions from the CLI override.\n", encoding="utf-8")
        # A decoy with the same relative name catches resolution against the
        # submission shell rather than the YAML source directory.
        (submitter / "prompts").mkdir()
        (submitter / "prompts" / default.name).write_text("Wrong relative base.\n", encoding="utf-8")
        for extra, expected in (([], default), (["--system-prompt-file", override.name], override)):
            with self.subTest(source=expected):
                self.queue = self.directory / ("yaml-system-default" if expected == default else "cli-system-override")
                original_cwd = Path.cwd()
                try:
                    os.chdir(submitter)
                    self.assertEqual(self.invoke(["--cwd", str(child), "--template-file", str(template), *extra])[0], 0)
                finally:
                    os.chdir(original_cwd)
                _, task = self.task()
                snapshot = Path(task["agent_system_prompt_path"])
                self.assertEqual(task["agent_system_prompt_source"], str(expected.resolve()))
                self.assertEqual(snapshot.read_bytes(), expected.read_bytes())
                self.assertEqual(task["agent_template_source"], str(template.resolve()))
                self.assertIn("YAML user-task instructions.",
                              Path(task["agent_prompt_path"]).read_text(encoding="utf-8"))

    def test_invalid_system_prompt_files_fail_before_any_task_persistence(self):
        invalid = {"empty.md": b"", "whitespace.md": b" \n\t", "invalid-utf8.md": b"\xff\xfe",
                   "bom-only.md": b"\xef\xbb\xbf", "bom-whitespace.md": b"\xef\xbb\xbf \n"}
        candidates = [self.directory / "missing.md", self.templates]
        for name, content in invalid.items():
            path = self.directory / name
            path.write_bytes(content)
            candidates.append(path)
        for source in candidates:
            for selector in ("cli", "yaml"):
                with self.subTest(source=source.name, selector=selector):
                    if selector == "cli":
                        options = ["--system-prompt-file", str(source)]
                    else:
                        template = self.write_template("version: 1\nprompt: Valid user-task profile.\n"
                                                       "pi:\n  system_prompt_file: " + json.dumps(str(source)) + "\n")
                        options = ["--template-file", str(template)]
                    self.assert_rejected(options)
        self.assert_rejected(["--system-prompt-file", str(self.directory / "missing.md"), "--dry-run"])

    def test_worker_reads_the_saved_system_prompt_after_source_rewrite_or_deletion(self):
        executable = self.fake_pi()
        original_system = "ORIGINAL PI SYSTEM INSTRUCTIONS\nKeep ${PI_MODEL} literal. 中文\n"
        user_prompt = "USER TASK: inspect artifacts using the saved instructions."
        for change in ("rewrite", "delete"):
            with self.subTest(change=change):
                self.queue = self.directory / f"frozen-system-{change}"
                source = self.directory / f"source-system-{change}.md"
                source.write_text(original_system, encoding="utf-8")
                capture = self.directory / f"system-capture-{change}.json"
                environment = self.fixture_environment(executable, capture, report="Saved-system report\n", exit_code=0)
                with mock.patch.dict(os.environ, environment):
                    self.assertEqual(self.invoke(["--system-prompt-file", str(source)],
                                                 parent="claude", prompt=user_prompt)[0], 0)
                path, task = self.task()
                if change == "rewrite":
                    source.write_text("REPLACEMENT SYSTEM INSTRUCTIONS\n", encoding="utf-8")
                else:
                    source.unlink()
                self.assertEqual(self.run_worker(path, task), 0,
                                 Path(task["log_path"]).read_text(encoding="utf-8"))
                received = json.loads(capture.read_text(encoding="utf-8"))
                self.assertEqual(received["system_prompt"], original_system)
                self.assertEqual(received["system_prompt_path"], task["agent_system_prompt_path"])
                self.assertEqual(received["prompt"], user_prompt)
                snapshot = Path(task["agent_system_prompt_path"])
                self.assertEqual(snapshot.read_text(encoding="utf-8"), original_system)
                assert_private_file(self, snapshot)
                callback = cli.load_request(self.queue / "pending" / f"{task['id']}.json")
                self.assertEqual(callback["agent"], "claude")
                self.assertEqual(callback["target"], {"kind": "session", "value": "parent-claude"})
                self.assertEqual(callback["exit_code"], 0)

    def test_worker_refuses_missing_or_blank_private_system_prompt_before_starting_pi(self):
        executable = self.fake_pi()
        source = self.directory / "valid-original-system.md"
        source.write_text("Valid original system instructions.\n", encoding="utf-8")
        for damage in ("missing", "blank"):
            with self.subTest(damage=damage):
                self.queue = self.directory / f"damaged-system-{damage}"
                capture = self.directory / f"damaged-system-capture-{damage}.json"
                environment = self.fixture_environment(executable, capture, report="Must never execute.\n", exit_code=0)
                with mock.patch.dict(os.environ, environment):
                    self.assertEqual(self.invoke(["--system-prompt-file", str(source)])[0], 0)
                path, task = self.task()
                snapshot = Path(task["agent_system_prompt_path"])
                if damage == "missing":
                    snapshot.unlink()
                else:
                    snapshot.write_text(" \n\t", encoding="utf-8")
                self.assert_system_prompt_worker_rejected(path, task, capture)

    def test_worker_refuses_a_system_prompt_record_pointing_outside_its_task_directory(self):
        executable = self.fake_pi()
        source = self.directory / "outside-managed-task-system.md"
        source.write_text("Readable source is not the managed task snapshot.\n", encoding="utf-8")
        capture = self.directory / "wrong-system-path-capture.json"
        environment = self.fixture_environment(executable, capture, report="Must never execute.\n", exit_code=0)
        with mock.patch.dict(os.environ, environment):
            self.assertEqual(self.invoke(["--system-prompt-file", str(source)], parent="claude")[0], 0)
        path, task = self.task()
        task["agent_system_prompt_path"] = str(source)
        self.assert_system_prompt_worker_rejected(path, task, capture)

    def test_system_prompt_dry_run_reports_the_resolved_source_without_writes_or_execution(self):
        source = self.templates / "dry-run-system.md"
        source.write_text("Use this system prompt only after durable submission.\n", encoding="utf-8")
        template = self.write_template("version: 2\nprompt: Dry-run task profile.\n"
                                       "pi:\n  system_prompt_file: dry-run-system.md\n")
        for options in (["--system-prompt-file", str(source)], ["--template-file", str(template)]):
            with self.subTest(options=options), mock.patch.object(cli.subprocess, "run") as process:
                result, output = self.invoke([*options, "--dry-run"])
            self.assertEqual(result, 0)
            self.assertIn(str(source.resolve()), output)
            process.assert_not_called()
            self.assertFalse(self.queue.exists())
            self.assertFalse(list(self.directory.rglob("agent-system-prompt.md")))

    def test_system_prompt_file_cli_option_is_available_only_for_pi_children(self):
        source = self.directory / "valid-system.md"
        source.write_text("Valid system instructions.\n", encoding="utf-8")
        for worker in ("codex", "claude"):
            argv = ["ltc", "agent", worker, "--queue-dir", str(self.queue), "--agent", "codex",
                    "--session", "parent-codex", "--system-prompt-file", str(source), "--", "User task."]
            with self.subTest(worker=worker), mock.patch.object(sys, "argv", argv), \
                    mock.patch.object(cli, "agent", return_value=0) as submit, \
                    contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as rejected:
                cli.main()
            self.assertNotEqual(rejected.exception.code, 0)
            submit.assert_not_called()
            self.assertFalse(cli.managed_tasks_root(self.queue).exists())

    def test_doctor_checks_pi_child_executable_separately_from_callback_agent(self):
        environment = {"CODEX_LONG_TASK_WAKEUP_CODEX_BIN": "fixture-codex",
                       "LONG_TASK_WAKEUP_CLAUDE_BIN": "fixture-claude",
                       "LONG_TASK_WAKEUP_PI_BIN": "fixture-pi"}
        for parent, available in (("codex", True), ("claude", False)):
            with self.subTest(parent=parent, pi_available=available):
                output = io.StringIO()
                argv = ["ltc", "doctor", "--operation", "agent", "--agent-worker", "pi",
                        "--agent", parent, "--session", f"parent-{parent}", "--backend", "screen",
                        "--callback-mode", "cli", "--queue-dir", str(self.queue)]

                def which(name):
                    return None if name == "fixture-pi" and not available else "/fixture/bin/" + name

                with mock.patch.dict(os.environ, environment), mock.patch.object(sys, "argv", argv), \
                        mock.patch.object(diagnostics, "queue_writable", return_value=True), \
                        mock.patch.object(diagnostics, "coordinator_issue", return_value=None), \
                        mock.patch.object(diagnostics, "runtime_issues", return_value=[]), \
                        mock.patch.object(diagnostics, "delivery_issues", return_value=[]), \
                        mock.patch.object(diagnostics.ScreenBackend, "available", return_value=True), \
                        mock.patch.object(diagnostics.callback_transport, "inspect_route", return_value={"status": "ready"}), \
                        mock.patch.object(diagnostics.shutil, "which", side_effect=which) as probe, \
                        contextlib.redirect_stdout(output):
                    result = cli.main()
                report = json.loads(output.getvalue())
                codes = {issue["code"] for issue in report["issues"]}
                self.assertEqual(result, 0 if available else 1)
                self.assertEqual("child_agent_unavailable" in codes, not available)
                self.assertNotIn("callback_agent_unavailable", codes)
                self.assertNotIn("child_agent_unspecified", codes)
                self.assertNotIn("diagnostics_failed", codes)
                probe.assert_any_call("fixture-pi")
                recheck = report["recheck_command"]
                self.assertEqual(recheck[recheck.index("--agent-worker") + 1], "pi")
                self.assertEqual(recheck[recheck.index("--agent") + 1], parent)
                self.assertEqual(recheck[recheck.index("--session") + 1], f"parent-{parent}")
                repair = report["repair_command"]
                self.assertEqual(repair[repair.index("--skill-target") + 1], parent)
                self.assertFalse(self.queue.exists())


if __name__ == "__main__":
    unittest.main()
