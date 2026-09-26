"""Codex child permission flags: independent regression checks, parent executed."""
import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from long_task_callback import cli


class CodexCompatibilityTests(unittest.TestCase):
    def setUp(self):
        if os.name == "nt":
            platform = mock.patch.object(sys, "platform", "linux")
            platform.start()
            self.addCleanup(platform.stop)

    def assert_permissions(self, command, sandbox):
        self.assertEqual(command[command.index("-s") + 1], sandbox)
        configs = [command[i + 1] for i, value in enumerate(command[:-1]) if value == "-c"]
        self.assertIn('approvals_reviewer="auto_review"', configs)
        self.assertIn('approval_policy="on-request"', configs)
        for forbidden in ("--approve-for-me", "--full-auto", "--yolo",
                          "--dangerously-bypass-approvals-and-sandbox"):
            self.assertNotIn(forbidden, command)

    def test_builder_preserves_requested_sandbox_with_explicit_auto_review(self):
        for sandbox in ("workspace-write", "read-only"):
            with self.subTest(sandbox=sandbox):
                command = cli.agent_wrapped_command("codex", cwd="/tmp", result_path=Path("/tmp/result"),
                                                    sandbox_mode=sandbox, permission_mode="auto")
                self.assert_permissions(command, sandbox)

    def test_generic_and_template_submissions_share_compatible_permissions(self):
        with tempfile.TemporaryDirectory() as tmp:
            for template in (False, True):
                for sandbox in ("workspace-write", "read-only"):
                    with self.subTest(template=template, sandbox=sandbox):
                        queue = Path(tmp) / f"queue-{template}-{sandbox}"
                        argv = ["ltc", "agent", "codex", "--agent", "codex", "--session", "compat-parent",
                                "--cwd", tmp, "--queue-dir", str(queue), "--task", "compatibility",
                                "--sandbox-mode", sandbox]
                        if template:
                            argv.extend(["--template", "test"])
                        argv.extend(["--", "Test the public contract."])
                        with mock.patch.object(sys, "argv", argv), \
                                mock.patch.dict(os.environ, {"LTC_TEMPLATE_DIR": str(Path(tmp) / "templates")}), \
                                mock.patch.object(cli, "screen_binary", return_value="/usr/bin/screen"), \
                                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                            self.assertEqual(cli.main(), 0)
                        task_path = next(queue.glob("tasks/*/task.json"))
                        task = json.loads(task_path.read_text())
                        self.assert_permissions(task["wrapped_command"], sandbox)
                        if template:
                            self.assertIn("gpt-5.6-luna", task["wrapped_command"])
                        else:
                            self.assertNotIn("--model", task["wrapped_command"])

    def test_installed_codex_accepts_flags_before_no_model_startup_guard(self):
        binary = shutil.which(cli.codex_command())
        if binary is None:
            self.skipTest("Codex executable not installed")
        with tempfile.TemporaryDirectory() as tmp:
            command = cli.agent_wrapped_command("codex", cwd=tmp, result_path=Path(tmp) / "result.txt",
                                                sandbox_mode="workspace-write", permission_mode="auto")
            command[0] = binary
            # Empty stdin stops before inference. A missing schema provides a second
            # startup guard if a future CLI changes its handling of empty stdin.
            command[-1:-1] = ["--output-schema", str(Path(tmp) / "missing-schema.json")]
            environment = dict(os.environ)
            profile = Path(tmp) / "codex-profile"
            profile.mkdir()
            environment["CODEX_HOME"] = str(profile)
            command = cli.process_command(command, environment)
            result = subprocess.run(command, input="", text=True, capture_output=True, timeout=20,
                                    cwd=tmp, env=environment)
            output = (result.stdout + result.stderr).lower()
            self.assertNotEqual(result.returncode, 0)
            self.assertNotIn("cannot be used with", output)
            self.assertNotIn("unexpected argument", output)
            self.assertNotIn("unrecognized option", output)
            self.assertTrue("schema" in output or "no prompt" in output or "empty" in output
                            or "no input" in output,
                            f"Codex did not reach the expected guarded startup stage: {output}")


if __name__ == "__main__":
    unittest.main()
