"""Read-only health evidence across task ownership and callback delivery."""
from __future__ import annotations

import argparse
import contextlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from long_task_callback import cli, diagnostics
from long_task_callback.platforms import OwnerState


class HealthArtifacts:
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="ltc runtime health ")
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.root = self.directory / "queue"
        self.root.mkdir()
        self.patches = contextlib.ExitStack()
        self.addCleanup(self.patches.close)
        self.patches.enter_context(mock.patch.object(cli, "current_machine_id", return_value="this-machine"))
        self.patches.enter_context(mock.patch.object(cli, "current_boot_id", return_value="this-boot"))
        self.patches.enter_context(mock.patch("long_task_callback.diagnostics.time.time", return_value=1000.0))

    def write_json(self, path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value), encoding="utf-8")

    def task(self, task_id="fixture-task", backend="systemd-user", **updates):
        task = {
            "version": 2 if backend == "systemd-user" else 1,
            "id": task_id, "queue_dir": str(self.root), "agent": "codex",
            "execution_backend": backend, "execution_owner": f"ltc-{task_id}-attempt1.service",
            "screen_session": f"ltc-{task_id}", "state": "running",
            "machine_id": "this-machine", "boot_id": "this-boot", "submission_boot_id": "this-boot",
            "launch_requested_at": 100.0, "started_at": 101.0,
            "cwd": str(self.directory), "task": "SECRET_TASK_CONTENT", "command": "SECRET_COMMAND_CONTENT",
            "target": {"kind": "session", "value": "bound-fixture-session"},
        }
        task.update(updates)
        self.write_json(cli.managed_task_path(self.root, task_id), task)
        return task

    def result(self, task, **updates):
        result = {"version": 1, "id": task["id"], "exit_code": 7, "completed_at": 1000.0, "outcome": "completed"}
        result.update(updates)
        self.write_json(cli.managed_result_path(self.root, task["id"]), result)

    def callback(self, callback_id="fixture-task", state="pending", **updates):
        value = {
            "version": 1, "id": callback_id, "cwd": str(self.directory), "agent": "codex",
            "target": {"kind": "session", "value": "bound-fixture-session"},
            "prompt": "SECRET_CALLBACK_CONTENT", "last_error": "SECRET_EXCEPTION_CONTENT",
        }
        value.update(updates)
        self.write_json(cli.request_path(self.root, state, callback_id), value)
        return value

    def snapshot(self):
        return {str(path.relative_to(self.directory)): path.read_bytes() if path.is_file() else None
                for path in self.directory.rglob("*")}

    def observe_tasks(self, owner=OwnerState.ABSENT):
        before = self.snapshot()
        with mock.patch.object(cli, "managed_owner_state", return_value=owner) as probe, mock.patch.object(
            cli, "launch_managed_task"
        ) as launch, mock.patch.object(cli, "queue_managed_task_callback") as queue, mock.patch.object(
            cli, "write_managed_task"
        ) as write:
            issues = diagnostics.runtime_issues(self.root)
        launch.assert_not_called()
        queue.assert_not_called()
        write.assert_not_called()
        self.assertEqual(self.snapshot(), before, "health inspection must preserve records and queue state")
        self.assertNotIn("SECRET_", json.dumps(issues))
        return issues, probe

    def assert_group(self, issues, code, backend, ids):
        groups = [issue for issue in issues if issue["code"] == code and issue["backend"] == backend]
        self.assertEqual(len(groups), 1, issues)
        group = groups[0]
        self.assertEqual(group["kind"], "runtime")
        self.assertEqual(group["count"], len(ids))
        self.assertLessEqual(len(group["task_ids"]), 5)
        self.assertTrue(set(group["task_ids"]).issubset(ids))
        if len(ids) <= 5:
            self.assertEqual(set(group["task_ids"]), set(ids))
        self.assertTrue(group["action"])


class RuntimeHealthTests(HealthArtifacts, unittest.TestCase):
    def test_idle_and_submitted_work_need_no_live_task_owner(self):
        issues, probe = self.observe_tasks()
        self.assertEqual(issues, [])
        probe.assert_not_called()
        self.task(state="submitted")
        issues, probe = self.observe_tasks()
        self.assertEqual(issues, [])
        probe.assert_not_called()

    def test_both_frozen_backends_are_probed_without_reselection(self):
        expected = [self.task("screen-work", "screen"), self.task("native-work", "systemd-user")]
        with mock.patch.object(cli, "select_execution_backend") as select:
            issues, probe = self.observe_tasks(OwnerState.ALIVE)
        self.assertEqual(issues, [])
        select.assert_not_called()
        self.assertCountEqual([call.args[0] for call in probe.call_args_list], expected)

    def test_missing_and_unverifiable_owners_remain_distinct_and_grouped(self):
        for backend in ("screen", "systemd-user"):
            for index in range(7):
                self.task(f"{backend}-{index}", backend)
        for owner, code in ((OwnerState.ABSENT, "task_owner_missing"), (OwnerState.UNKNOWN, "task_owner_unverified")):
            with self.subTest(owner=owner):
                issues, probe = self.observe_tasks(owner)
                self.assertEqual(len(issues), 2)
                self.assertEqual(probe.call_count, 14)
                for backend in ("screen", "systemd-user"):
                    self.assert_group(issues, code, backend, {f"{backend}-{index}" for index in range(7)})

    def test_unknown_owner_does_not_taint_healthy_tasks_on_the_same_backend(self):
        for backend in ("screen", "systemd-user"):
            with self.subTest(backend=backend):
                uncertain = self.task("a-uncertain", backend)
                healthy = self.task("b-healthy", backend)
                before = self.snapshot()
                with mock.patch.object(cli, "managed_owner_state", side_effect=[OwnerState.UNKNOWN, OwnerState.ALIVE]) as probe:
                    issues = diagnostics.runtime_issues(self.root)
                self.assertEqual(probe.call_args_list, [mock.call(uncertain), mock.call(healthy)])
                self.assertEqual(len(issues), 1)
                self.assert_group(issues, "task_owner_unverified", backend, {"a-uncertain"})
                self.assertEqual(self.snapshot(), before)

    def test_new_launch_has_time_to_establish_its_worker(self):
        self.task(state="launching", launch_requested_at=1000.0)
        issues, _ = self.observe_tasks(OwnerState.ABSENT)
        self.assertEqual(issues, [])

    def test_foreign_machine_and_old_boot_do_not_probe_unrelated_local_owners(self):
        self.task("foreign", "screen", machine_id="other-machine")
        self.task("old-boot", "systemd-user", boot_id="previous-boot")
        issues, probe = self.observe_tasks()
        probe.assert_not_called()
        self.assert_group(issues, "task_foreign_host", "screen", {"foreign"})
        self.assert_group(issues, "task_boot_changed", "systemd-user", {"old-boot"})

    def test_previously_reconciled_foreign_host_record_remains_visible_without_local_probe(self):
        self.task("already-foreign", "screen", state="foreign_host", machine_id="other-machine")
        issues, probe = self.observe_tasks()
        self.assert_group(issues, "task_foreign_host", "screen", {"already-foreign"})
        probe.assert_not_called()

    def test_durable_result_precedes_missing_owner_and_nonzero_is_not_infrastructure_failure(self):
        task = self.task()
        self.result(task, completed_at=100.0, exit_code=7)
        self.callback()
        issues, probe = self.observe_tasks()
        self.assertEqual(issues, [])
        probe.assert_not_called()

    def test_corrupt_result_and_task_record_are_visible_without_exposing_contents(self):
        task = self.task()
        self.result(task, id="different-task", exit_code="SECRET_INVALID_RESULT")
        invalid = cli.managed_task_path(self.root, "corrupt-record")
        invalid.parent.mkdir(parents=True)
        invalid.write_text("SECRET_INVALID_JSON", encoding="utf-8")
        issues, _ = self.observe_tasks()
        codes = {issue["code"] for issue in issues}
        self.assertIn("task_result_invalid", codes)
        self.assertIn("task_record_invalid", codes)
        self.assertNotIn("result_handoff_missing", codes)

    def test_completion_during_probe_is_rechecked_before_reporting_missing_or_unknown_owner(self):
        for owner in (OwnerState.ABSENT, OwnerState.UNKNOWN):
            with self.subTest(owner=owner):
                task = self.task()
                result_path = cli.managed_result_path(self.root, task["id"])
                result_path.unlink(missing_ok=True)

                def finish_during_probe(record):
                    self.result(task)
                    return owner

                with mock.patch.object(cli, "managed_owner_state", side_effect=finish_during_probe) as probe, mock.patch.object(
                    cli, "queue_managed_task_callback"
                ) as queue, mock.patch.object(cli, "launch_managed_task") as launch:
                    self.assertEqual(diagnostics.runtime_issues(self.root), [])
                probe.assert_called_once()
                queue.assert_not_called()
                launch.assert_not_called()

    def test_terminal_record_racing_owner_probe_is_reread(self):
        task = self.task()

        def finish_during_probe(record):
            task.update(state="completed", exit_code=0, completed_at=1000.0)
            self.write_json(cli.managed_task_path(self.root, task["id"]), task)
            return OwnerState.ABSENT

        with mock.patch.object(cli, "managed_owner_state", side_effect=finish_during_probe):
            self.assertEqual(diagnostics.runtime_issues(self.root), [])

    def test_result_without_callback_requires_handoff_only_after_grace(self):
        task = self.task()
        self.result(task)
        issues, probe = self.observe_tasks()
        self.assertEqual(issues, [])
        probe.assert_not_called()
        self.result(task, completed_at=100.0)
        issues, probe = self.observe_tasks()
        self.assert_group(issues, "result_handoff_missing", "systemd-user", {task["id"]})
        probe.assert_not_called()

    def test_terminal_states_require_actual_callback_evidence_not_queued_metadata(self):
        for state in ("completed", "interrupted", "launch_failed"):
            self.task(state, "screen", state=state, completed_at=100.0, callback_queued_at=101.0)
        issues, probe = self.observe_tasks()
        self.assert_group(issues, "result_handoff_missing", "screen", {"completed", "interrupted", "launch_failed"})
        probe.assert_not_called()

    def test_existing_callback_in_each_lifecycle_state_satisfies_result_handoff(self):
        for state in (cli.ACTIVE_STATE, "pending", "running", "done", "failed", "canceled", "acks"):
            task_id = f"callback-{state}"
            self.task(task_id, state="completed", completed_at=100.0)
            self.callback(task_id, state)
        issues, probe = self.observe_tasks()
        self.assertEqual(issues, [])
        probe.assert_not_called()

    def test_configured_container_can_still_have_broken_runtime_and_needs_no_reinstall(self):
        self.task(backend="screen")
        skill = self.directory / "skills" / "long-task-callback" / "SKILL.md"
        skill.parent.mkdir(parents=True)
        skill.write_text("fixture skill", encoding="utf-8")
        args = argparse.Namespace(mode="doctor", operation="run", agent="codex", session="actual-session",
                                  last=False, queue_dir=str(self.root), backend="screen", _health_backend="screen")
        with mock.patch.object(diagnostics, "environment_profile", return_value={
            "os": "linux", "container": True, "supported": True,
        }), mock.patch.object(diagnostics, "queue_writable", return_value=True), mock.patch.object(
            diagnostics, "coordinator_issue", return_value=None
        ), mock.patch.object(diagnostics.ScreenBackend, "available", return_value=True), mock.patch.object(
            diagnostics.shutil, "which", return_value="/fixture/codex"
        ), mock.patch.object(cli, "codex_home", return_value=self.directory), mock.patch.object(
            cli, "managed_owner_state", return_value=OwnerState.ABSENT
        ):
            report = diagnostics.inspect(args)
        self.assertEqual(report["status"], "needs_configuration")
        self.assertEqual(report["checks"], {"support": "supported", "configuration": "ready", "runtime": "needs_attention"})
        self.assertTrue(report["environment"]["container"])
        self.assertIsNone(report["repair_command"], "reinstalling services cannot restore a lost business process")
        self.assert_group(report["issues"], "task_owner_missing", "screen", {"fixture-task"})
        self.assertIn("actual-session", report["recheck_command"])


class DeliveryHealthTests(HealthArtifacts, unittest.TestCase):
    def observe_deliveries(self):
        before = self.snapshot()
        with mock.patch.object(cli, "retain_target_lease") as retain, mock.patch.object(
            cli, "write_request"
        ) as write:
            issues = diagnostics.delivery_issues(self.root)
        retain.assert_not_called()
        write.assert_not_called()
        self.assertEqual(self.snapshot(), before)
        self.assertNotIn("SECRET_", json.dumps(issues))
        return issues

    def test_failed_delivery_and_uncertain_target_outcome_have_separate_actions(self):
        self.callback("failed", "failed", exit_code=7)
        self.callback("uncertain", "failed", retain_target_lease=True, exit_code=0)
        self.callback("business-failure-delivered", "done", exit_code=7)
        issues = self.observe_deliveries()
        self.assertEqual({issue["code"] for issue in issues}, {"callback_delivery_failed", "callback_outcome_unknown"})
        for issue in issues:
            self.assertEqual(issue["kind"], "runtime")
            self.assertEqual(issue["component"], "callback_delivery")
            self.assertEqual(issue["agent"], "codex")
            self.assertEqual(issue["count"], 1)
            expected = "uncertain" if issue["code"] == "callback_outcome_unknown" else "failed"
            self.assertEqual(issue["callback_ids"], [expected])

    def test_acknowledged_and_canceled_failed_copies_are_not_actionable(self):
        for marker in ("acks", "canceled"):
            self.callback(marker, "failed", retain_target_lease=True)
            self.callback(marker, marker)
        self.assertEqual(self.observe_deliveries(), [])

    def test_failed_callback_group_is_bounded_without_losing_total_count(self):
        for index in range(8):
            self.callback(f"failed-{index}", "failed")
        issues = self.observe_deliveries()
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0]["count"], 8)
        self.assertEqual(len(issues[0]["callback_ids"]), 5)

    def test_corrupt_failed_record_is_unverified_instead_of_claimed_delivered_or_failed(self):
        path = cli.request_path(self.root, "failed", "invalid")
        path.parent.mkdir(parents=True)
        path.write_text("SECRET_BAD_JSON", encoding="utf-8")
        issues = self.observe_deliveries()
        self.assertEqual([issue["code"] for issue in issues], ["callback_record_unverified"])

    def test_ack_racing_failed_record_read_suppresses_obsolete_alert(self):
        self.callback("racing", "failed", retain_target_lease=True)
        original_load = cli.load_request

        def acknowledge_during_read(path):
            record = original_load(path)
            self.callback("racing", "acks")
            return record

        with mock.patch.object(cli, "load_request", side_effect=acknowledge_during_read):
            self.assertEqual(diagnostics.delivery_issues(self.root), [])


class EnvironmentHealthTests(unittest.TestCase):
    def test_environment_profile_does_not_infer_native_support_from_cli_presence(self):
        for platform, expected_os, supported in (("linux", "linux", True), ("darwin", "macos", True),
                                                 ("win32", "windows", False), ("freebsd14", "other", False)):
            with self.subTest(platform=platform), mock.patch.object(diagnostics.sys, "platform", platform), mock.patch.object(
                cli, "running_in_container", return_value=True
            ) as container:
                report = diagnostics.environment_profile()
            self.assertEqual(report, {"os": expected_os, "container": platform == "linux", "supported": supported})
            if platform != "linux":
                container.assert_not_called()

    def test_unsupported_native_os_stops_before_linux_checks_and_proposes_no_linux_repair(self):
        args = argparse.Namespace(mode="doctor", operation="run", backend="auto", agent="codex",
                                  session="actual-session", last=False, queue_dir="/fixture/queue")
        for platform in ("freebsd14", "win32"):
            with self.subTest(platform=platform), mock.patch.object(diagnostics.sys, "platform", platform), contextlib.ExitStack() as stack:
                probes = [stack.enter_context(mock.patch.object(module, name)) for module, name in (
                    (diagnostics, "queue_writable"), (diagnostics, "coordinator_issue"),
                    (diagnostics, "runtime_issues"), (diagnostics, "delivery_issues"),
                    (cli, "select_execution_backend"), (cli, "running_in_container"),
                    (cli, "current_machine_id"), (cli, "current_boot_id"), (diagnostics.shutil, "which"),
                )]
                report = diagnostics.inspect(args)
            self.assertEqual(report["status"], "needs_configuration")
            self.assertEqual([issue["code"] for issue in report["issues"]], ["platform_unsupported"])
            self.assertEqual(report["issues"][0]["kind"], "support")
            self.assertFalse(report["environment"]["supported"])
            self.assertIsNone(report["repair_command"])
            self.assertIn("doctor", report["recheck_command"])
            self.assertIn("actual-session", report["recheck_command"])
            for probe in probes:
                probe.assert_not_called()


if __name__ == "__main__":
    unittest.main()
