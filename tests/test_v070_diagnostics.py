"""Independent contracts for concise, actionable LTC configuration feedback."""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from long_task_callback import cli, diagnostics


class DiagnosticBoundaryTests(unittest.TestCase):
    def test_task_return_status_and_validation_exit_survive_diagnostic_reporting(self):
        args = argparse.Namespace(mode="run", dry_run=False)
        with mock.patch.object(diagnostics, "emit_if_needed") as emit:
            self.assertEqual(cli.invoke_with_diagnostics(args, lambda _: 125), 125)
            emit.assert_called_once_with(args)
        failure = SystemExit("original task validation failure")
        with mock.patch.object(diagnostics, "emit_if_needed") as emit:
            with self.assertRaises(SystemExit) as caught:
                cli.invoke_with_diagnostics(args, mock.Mock(side_effect=failure))
            self.assertIs(caught.exception, failure)
            emit.assert_called_once_with(args)

    def test_dry_run_does_not_probe_or_emit(self):
        args = argparse.Namespace(mode="run", dry_run=True)
        with mock.patch.object(diagnostics, "emit_if_needed") as emit:
            self.assertEqual(cli.invoke_with_diagnostics(args, lambda _: 0), 0)
        emit.assert_not_called()

    def test_public_task_modes_report_after_dispatch_preserving_existing_output(self):
        for argv, handler in (
            (["run", "--session", "bound", "--", "echo", "fixture"], "run"),
            (["agent", "claude", "--session", "bound", "--", "fixture requirement"], "agent"),
            (["done", "--session", "bound", "--exit-code", "7"], "done"),
        ):
            events = []

            def operation(args):
                events.append("operation")
                print("original task output")
                return 7

            def emit(args):
                events.append("diagnostics")

            with self.subTest(handler=handler), mock.patch.object(sys, "argv", ["ltc", *argv]), mock.patch.object(
                cli, handler, side_effect=operation
            ), mock.patch.object(diagnostics, "emit_if_needed", side_effect=emit), contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(cli.main(), 7)
            self.assertEqual(events, ["operation", "diagnostics"])
            self.assertEqual(output.getvalue(), "original task output\n")

    def test_help_version_ack_cancel_and_internal_workers_do_not_trigger_checks(self):
        for argv, handler in (
            (["ack", "--id", "fixture"], "ack"),
            (["cancel", "--id", "fixture"], "cancel"),
            (["_screen-worker", "--task-file", "/fixture/task.json", "--token=fixture"], "run_screen_worker"),
            (["_task-worker", "--task-file", "/fixture/task.json", "--attempt", "1"], "run_task_worker"),
            (["_delivery-worker"], "delivery_worker_main"),
        ):
            with self.subTest(argv=argv), mock.patch.object(sys, "argv", ["ltc", *argv]), mock.patch.object(
                cli, handler, return_value=0
            ), mock.patch.object(diagnostics, "emit_if_needed") as emit:
                self.assertEqual(cli.main(), 0)
                emit.assert_not_called()
        for flag in ("--help", "--version"):
            with self.subTest(flag=flag), mock.patch.object(sys, "argv", ["ltc", flag]), mock.patch.object(
                diagnostics, "emit_if_needed"
            ) as emit, contextlib.redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as caught:
                cli.main()
            self.assertEqual(caught.exception.code, 0)
            emit.assert_not_called()

    def test_unexpected_health_failure_cannot_replace_original_exit_or_leak_exception_text(self):
        args = argparse.Namespace(mode="run", dry_run=False, queue_dir="/fixture/queue")
        with mock.patch.object(diagnostics, "inspect", side_effect=RuntimeError("SECRET_IN_EXCEPTION")), contextlib.redirect_stderr(
            io.StringIO()
        ) as errors:
            self.assertEqual(cli.invoke_with_diagnostics(args, lambda _: 23), 23)
        self.assertNotIn("SECRET_IN_EXCEPTION", errors.getvalue())
        self.assertIn("[ltc-status]", errors.getvalue())


class HealthReportTests(unittest.TestCase):
    def setUp(self):
        if os.name == "nt":
            platform = mock.patch.object(sys, "platform", "linux")
            platform.start()
            self.addCleanup(platform.stop)
        temporary = tempfile.TemporaryDirectory(prefix="ltc health ")
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.queue = self.directory / "queue with spaces"
        self.codex_home = self.directory / "codex"
        self.claude_home = self.directory / "claude"
        for home in (self.codex_home, self.claude_home):
            skill = home / "skills" / "long-task-callback" / "SKILL.md"
            skill.parent.mkdir(parents=True)
            skill.write_text("Fixture LTC skill", encoding="utf-8")
        self.patches = contextlib.ExitStack()
        self.addCleanup(self.patches.close)
        self.patches.enter_context(mock.patch.dict(os.environ, {
            "CODEX_HOME": str(self.codex_home), "CLAUDE_CONFIG_DIR": str(self.claude_home),
            "CODEX_LONG_TASK_WAKEUP_CODEX_BIN": "fixture-codex", "LONG_TASK_WAKEUP_CLAUDE_BIN": "fixture-claude",
            "API_KEY": "SECRET_ENV_SENTINEL", "PATH": "/fixture/bin",
        }, clear=True))
        self.patches.enter_context(mock.patch.object(diagnostics, "queue_writable", return_value=True))
        self.patches.enter_context(mock.patch.object(diagnostics, "coordinator_issue", return_value=None))
        self.patches.enter_context(mock.patch.object(diagnostics.ScreenBackend, "available", return_value=True))
        self.patches.enter_context(mock.patch.object(diagnostics.shutil, "which", side_effect=lambda name: "/fixture/bin/" + Path(name).name))

    def arguments(self, **updates):
        values = dict(mode="run", backend="screen", agent="codex", session="original-bound-session", last=False,
                      queue_dir=str(self.queue), dry_run=False, task="SECRET_TASK_SENTINEL",
                      wrapped_command=["echo", "SECRET_COMMAND_SENTINEL"], _health_backend="screen")
        values.update(updates)
        return argparse.Namespace(**values)

    def issue_codes(self, report):
        return {issue["code"] for issue in report["issues"]}

    def test_ready_is_silent_implicitly_and_does_not_claim_authentication_or_delivery(self):
        args = self.arguments()
        with mock.patch.object(cli, "select_execution_backend") as select, mock.patch.object(cli.subprocess, "run") as external:
            report = diagnostics.inspect(args)
            with contextlib.redirect_stdout(io.StringIO()) as output, contextlib.redirect_stderr(io.StringIO()) as errors:
                diagnostics.emit_if_needed(args)
        self.assertEqual(report["schema"], "ltc.health.v1")
        self.assertEqual(report["status"], "ready")
        self.assertEqual(report["scope"], "local_prerequisites")
        self.assertIn("authentication", report["unverified"])
        self.assertIn("session_delivery", report["unverified"])
        self.assertEqual(report["issues"], [])
        self.assertEqual(output.getvalue(), "")
        self.assertEqual(errors.getvalue(), "")
        select.assert_not_called()
        external.assert_not_called()

    def test_unhealthy_block_has_pinned_repair_and_recheck_without_secret_task_dump(self):
        args = self.arguments(_health_work={"state": "task_persisted", "id": "persisted-1234"})
        with mock.patch.object(diagnostics, "coordinator_issue", return_value={
            "code": "daemon_not_running", "action": "Start the coordinator for this queue."
        }), contextlib.redirect_stderr(io.StringIO()) as errors, contextlib.redirect_stdout(io.StringIO()) as output:
            diagnostics.emit_if_needed(args)
        block = errors.getvalue()
        self.assertEqual(block.count("[ltc-status]"), 1)
        self.assertEqual(block.count("[/ltc-status]"), 1)
        report = json.loads(block.split("[ltc-status]", 1)[1].split("[/ltc-status]", 1)[0])
        self.assertEqual(report["status"], "needs_configuration")
        self.assertEqual(report["work"], {"state": "task_persisted", "id": "persisted-1234"})
        self.assertEqual(report["queue_dir"], str(self.queue))
        self.assertEqual(output.getvalue(), "")
        for sentinel in ("SECRET_ENV_SENTINEL", "SECRET_TASK_SENTINEL", "SECRET_COMMAND_SENTINEL"):
            self.assertNotIn(sentinel, block)
        for key, operation in (("repair_command", "setup"), ("recheck_command", "doctor")):
            command = report[key]
            self.assertIsInstance(command, list)
            self.assertEqual(command[0], sys.executable)
            self.assertEqual(Path(command[1]), Path(cli.__file__).with_name("_entry.py"))
            self.assertIn(operation, command)
            self.assertEqual(command[command.index("--queue-dir") + 1], str(self.queue))
            self.assertEqual(command[command.index("--backend") + 1], "screen")
            self.assertNotIn("--last", command)
        repair = report["repair_command"]
        self.assertIn("--now", repair)
        self.assertEqual(repair[repair.index("--skill-target") + 1], "codex")
        recheck = report["recheck_command"]
        self.assertEqual(recheck[recheck.index("--session") + 1], "original-bound-session")
        self.assertEqual(recheck[recheck.index("--agent") + 1], "codex")
        self.assertRegex(report["instruction"].lower(), r"(never|do not) resubmit")

    def test_real_run_admission_is_reported_as_persisted_without_resubmitting(self):
        argv = ["ltc", "run", "--queue-dir", str(self.queue), "--backend", "screen",
                "--agent", "codex", "--session", "original-bound-session", "--", "/bin/true"]
        with mock.patch.object(sys, "argv", argv), mock.patch.object(cli, "screen_binary", return_value="/fixture/screen"), mock.patch.object(
            diagnostics, "coordinator_issue", return_value={"code": "daemon_not_running", "action": "Start coordinator."}
        ), mock.patch.object(cli.subprocess, "Popen") as child, contextlib.redirect_stderr(io.StringIO()) as errors:
            self.assertEqual(cli.main(), 0)
        child.assert_not_called()
        tasks = list(cli.managed_tasks_root(self.queue).glob("*/task.json"))
        self.assertEqual(len(tasks), 1)
        task = json.loads(tasks[0].read_text())
        report = json.loads(errors.getvalue().split("[ltc-status]", 1)[1].split("[/ltc-status]", 1)[0])
        self.assertEqual(report["work"], {"state": "task_persisted", "id": task["id"]})
        self.assertEqual(task["state"], "submitted")
        self.assertEqual(report["status"], "needs_configuration")

    def test_real_done_keeps_queued_callback_and_exit_semantics_when_daemon_needs_repair(self):
        argv = ["ltc", "done", "--queue-dir", str(self.queue), "--agent", "codex",
                "--session", "original-bound-session", "--exit-code", "7"]
        with mock.patch.object(sys, "argv", argv), mock.patch.object(
            cli, "select_execution_backend", side_effect=AssertionError("done must not select a task backend")
        ), mock.patch.object(diagnostics, "coordinator_issue", return_value={
            "code": "daemon_not_running", "action": "Start coordinator."
        }), contextlib.redirect_stderr(io.StringIO()) as errors:
            self.assertEqual(cli.main(), 0)
        callbacks = list((self.queue / "pending").glob("*.json"))
        self.assertEqual(len(callbacks), 1)
        callback = json.loads(callbacks[0].read_text())
        report = json.loads(errors.getvalue().split("[ltc-status]", 1)[1].split("[/ltc-status]", 1)[0])
        self.assertEqual(report["work"], {"state": "callback_queued", "id": callback["id"]})
        self.assertEqual(callback["exit_code"], 7)
        self.assertIn("--callback-only", report["repair_command"])

    def test_real_validation_failure_emits_not_submitted_without_creating_task(self):
        argv = ["ltc", "run", "--queue-dir", str(self.queue), "--backend", "screen",
                "--agent", "codex", "--session", "original-bound-session"]
        with mock.patch.object(sys, "argv", argv), mock.patch.object(cli, "screen_binary", return_value="/fixture/screen"), mock.patch.object(
            diagnostics, "coordinator_issue", return_value={"code": "daemon_not_running", "action": "Start coordinator."}
        ), contextlib.redirect_stderr(io.StringIO()) as errors, self.assertRaises(SystemExit):
            cli.main()
        report = json.loads(errors.getvalue().split("[ltc-status]", 1)[1].split("[/ltc-status]", 1)[0])
        self.assertEqual(report["work"], {"state": "not_submitted"})
        self.assertFalse(cli.managed_tasks_root(self.queue).exists())

    def test_done_reports_callback_prerequisites_without_requiring_execution_backend(self):
        args = self.arguments(mode="done", backend="systemd", _health_work={"state": "callback_queued", "id": "done-1234"})
        del args._health_backend
        with mock.patch.object(cli, "select_execution_backend", side_effect=AssertionError("done must not select backend")) as select:
            report = diagnostics.inspect(args)
        select.assert_not_called()
        self.assertEqual(report["status"], "ready")
        self.assertEqual(report["work"]["state"], "callback_queued")
        self.assertIn("--callback-only", report["repair_command"])

    def test_unknown_admission_requires_record_inspection_even_when_configuration_is_ready(self):
        args = self.arguments(_health_work={"state": "unknown", "id": "reserved-task", "secret": "EXTRA_SECRET"})
        report = diagnostics.inspect(args)
        self.assertEqual(report["status"], "needs_configuration")
        self.assertIn("submission_unverified", self.issue_codes(report))
        self.assertEqual(report["work"], {"state": "unknown", "id": "reserved-task"})
        self.assertNotIn("EXTRA_SECRET", json.dumps(report))

    def test_task_storage_readiness_is_checked_separately_from_queue_storage(self):
        tasks = cli.managed_tasks_root(self.queue)
        with mock.patch.object(diagnostics, "queue_writable", side_effect=lambda path: path != tasks):
            report = diagnostics.inspect(self.arguments())
        self.assertIn("tasks_unwritable", self.issue_codes(report))
        self.assertNotIn("queue_unwritable", self.issue_codes(report))

    def test_failed_backend_admission_is_not_reprobed_or_exposed_and_broken_screen_is_not_ready(self):
        args = self.arguments(_health_backend_error="SECRET_BACKEND_FAILURE")
        with mock.patch.object(cli, "select_execution_backend", side_effect=AssertionError("cached failure must not repeat")) as select:
            report = diagnostics.inspect(args)
        select.assert_not_called()
        self.assertIn("backend_unavailable", self.issue_codes(report))
        self.assertNotIn("SECRET_BACKEND_FAILURE", json.dumps(report))
        with mock.patch.object(diagnostics.ScreenBackend, "available", return_value=False):
            report = diagnostics.inspect(self.arguments())
        self.assertEqual(report["status"], "needs_configuration")
        self.assertIn("backend_unavailable", self.issue_codes(report))

    def test_doctor_agent_operation_needs_explicit_child_selection(self):
        report = diagnostics.inspect(self.arguments(mode="doctor", operation="agent", agent_worker=None))
        self.assertIn("child_agent_unspecified", self.issue_codes(report))
        self.assertEqual(report["work"], {"state": "not_applicable"})

    def test_child_cli_and_callback_cli_are_checked_separately(self):
        args = self.arguments(mode="agent", agent_worker="claude")
        with mock.patch.object(diagnostics.shutil, "which", side_effect=lambda name: None if name == "fixture-claude" else "/fixture/codex"):
            report = diagnostics.inspect(args)
        self.assertIn("child_agent_unavailable", self.issue_codes(report))
        self.assertNotIn("callback_agent_unavailable", self.issue_codes(report))
        with mock.patch.object(diagnostics.shutil, "which", side_effect=lambda name: None if name == "fixture-codex" else "/fixture/claude"):
            report = diagnostics.inspect(args)
        self.assertIn("callback_agent_unavailable", self.issue_codes(report))
        self.assertNotIn("child_agent_unavailable", self.issue_codes(report))

    def test_unbound_session_reports_capture_requirement_without_inventing_last_target(self):
        report = diagnostics.inspect(self.arguments(session=None))
        self.assertIn("session_unbound", self.issue_codes(report))
        self.assertNotIn("--last", report["recheck_command"])
        self.assertNotIn("--session", report["recheck_command"])
        self.assertIn("invoking AI", report["instruction"])

    def test_missing_skill_and_unwritable_queue_are_actionable_without_repair_side_effects(self):
        (self.codex_home / "skills" / "long-task-callback" / "SKILL.md").unlink()
        with mock.patch.object(diagnostics, "queue_writable", return_value=False), mock.patch.object(
            cli, "setup"
        ) as repair, mock.patch.object(cli.subprocess, "Popen") as launch:
            report = diagnostics.inspect(self.arguments())
        self.assertTrue({"skill_missing", "queue_unwritable"}.issubset(self.issue_codes(report)))
        self.assertTrue(all(issue.get("action") for issue in report["issues"]))
        repair.assert_not_called()
        launch.assert_not_called()
        self.assertFalse(self.queue.exists())

    def test_explicit_doctor_json_exit_status_matches_readiness(self):
        for healthy in (True, False):
            args = self.arguments(mode="doctor", operation="run")
            issue = None if healthy else {"code": "daemon_not_running", "action": "Start coordinator."}
            with self.subTest(healthy=healthy), mock.patch.object(diagnostics, "coordinator_issue", return_value=issue), contextlib.redirect_stdout(
                io.StringIO()
            ) as output:
                self.assertEqual(diagnostics.doctor(args), 0 if healthy else 1)
            self.assertEqual(json.loads(output.getvalue())["status"], "ready" if healthy else "needs_configuration")
            self.assertEqual(json.loads(output.getvalue())["work"], {"state": "not_applicable"})


class SkillRepairPreservationTests(unittest.TestCase):
    def test_keep_existing_preserves_customized_tree_even_with_force(self):
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "long-task-callback"
            (target / "agents").mkdir(parents=True)
            (target / "SKILL.md").write_text("User-maintained custom LTC workflow", encoding="utf-8")
            (target / "agents" / "openai.yaml").write_text("custom: preserved", encoding="utf-8")
            (target / "local-notes.txt").write_text("Private local customization", encoding="utf-8")
            original = {path.relative_to(target): path.read_bytes() for path in target.rglob("*") if path.is_file()}
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(cli.install_skill_tree(target, include_codex_plugin=True, force=True, keep_existing=True), 0)
            resulting = {path.relative_to(target): path.read_bytes() for path in target.rglob("*") if path.is_file()}
            self.assertEqual(resulting, original)

    def test_keep_existing_still_installs_missing_bundled_skill(self):
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "missing" / "long-task-callback"
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(cli.install_skill_tree(target, include_codex_plugin=True, force=True, keep_existing=True), 0)
            bundled = Path(cli.__file__).parent / "skill"
            self.assertEqual((target / "SKILL.md").read_bytes(), (bundled / "SKILL.md").read_bytes())
            self.assertTrue((target / "agents" / "openai.yaml").is_file())


class DiagnosticFilesystemTests(unittest.TestCase):
    def test_queue_probe_preserves_existing_contents_and_does_not_create_missing_queue(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            missing = directory / "not-created" / "queue"
            self.assertTrue(diagnostics.queue_writable(missing))
            self.assertEqual(list(directory.iterdir()), [])
            queue = directory / "queue"
            (queue / "pending").mkdir(parents=True)
            marker = queue / "pending" / "retained.json"
            marker.write_text('{"preserve": true}', encoding="utf-8")
            original = {str(path.relative_to(queue)): path.read_bytes() for path in queue.rglob("*") if path.is_file()}
            self.assertTrue(diagnostics.queue_writable(queue))
            after = {str(path.relative_to(queue)): path.read_bytes() for path in queue.rglob("*") if path.is_file()}
            self.assertEqual(after, original)
            self.assertEqual(sorted(path.name for path in queue.iterdir()), ["pending"])
            with mock.patch.object(diagnostics.os, "fsync", side_effect=OSError("probe disk failure")):
                self.assertFalse(diagnostics.queue_writable(queue))
            self.assertEqual(sorted(path.name for path in queue.iterdir()), ["pending"])
            self.assertEqual(marker.read_text(), '{"preserve": true}')

    def test_non_directory_operational_path_is_not_reported_writable(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "queue"
            root.mkdir()
            (root / "pending").write_text("unexpected file")
            self.assertFalse(diagnostics.queue_writable(root))
            self.assertEqual((root / "pending").read_text(), "unexpected file")

    def test_missing_or_released_singleton_does_not_create_or_claim_coordinator(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "queue"
            self.assertEqual(diagnostics.coordinator_issue(root)["code"], "daemon_not_running")
            self.assertFalse(root.exists())
            lock = cli.acquire_owner_lock(root, "daemon-singleton", blocking=True)
            cli.release_owner_lock(lock, remove=False)
            with mock.patch.object(cli, "read_daemon_runtime") as runtime:
                self.assertEqual(diagnostics.coordinator_issue(root)["code"], "daemon_not_running")
            runtime.assert_not_called()

    def test_held_singleton_needs_verified_matching_version_before_ready(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "queue"
            lock = cli.acquire_owner_lock(root, "daemon-singleton", blocking=True)
            try:
                record = {"pid": 123, "queue_dir": str(root.resolve()), "version": "older-version"}
                with mock.patch.object(cli, "read_daemon_runtime", return_value=record), mock.patch.object(
                    cli, "daemon_supports_hot_reload", return_value=False
                ):
                    self.assertEqual(diagnostics.coordinator_issue(root)["code"], "daemon_unverified")
                with mock.patch.object(cli, "read_daemon_runtime", return_value=record), mock.patch.object(
                    cli, "daemon_supports_hot_reload", return_value=True
                ):
                    self.assertEqual(diagnostics.coordinator_issue(root)["code"], "daemon_version_mismatch")
                    record["version"] = cli.__version__
                    self.assertIsNone(diagnostics.coordinator_issue(root))
            finally:
                cli.release_owner_lock(lock, remove=False)


if __name__ == "__main__":
    unittest.main()
