"""Public CLI queue transport: frozen identity, one publication and receiver ACK."""
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
import unittest
from unittest import mock

from long_task_callback import callback_transport as transport, cli, codex_queue

THREAD = "01a15842-e8a7-7630-9fee-869784c02260"
MESSAGE = "01a19796-8012-7ea3-bd98-60b2b6acbfbc"


class QueueTestCase(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="ltc-queue-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(mock.patch.dict(os.environ, {
            "CODEX_HOME": str(self.root / "profile"), "LTC_HOME": str(self.root / "ltc"),
            "CODEX_LONG_TASK_WAKEUP_TARGET_LOCK_DIR": str(self.root / "locks"),
            # An executable that exists: Windows resolves it before the mocked run.
            "CODEX_LONG_TASK_WAKEUP_CODEX_BIN": sys.executable, "PATH": os.environ.get("PATH", ""),
            **{key: os.environ[key] for key in ("SystemRoot", "USERPROFILE") if key in os.environ},
        }, clear=True))
        self.errors = io.StringIO()
        self.stack.enter_context(contextlib.redirect_stderr(self.errors))
        self.queue = self.root / "queue"
        cli.ensure_daemon_dirs(self.queue)
        self.request = {"version": codex_queue.CALLBACK_VERSION, "id": "native-c1", "agent": "codex",
                        "cwd": str(self.root), "created_at": 1.0, "target": {"kind": "session", "value": THREAD},
                        "target_source": "fixture", "prompt": "inspect and ACK native-c1",
                        "callback_mode": "codex-queue", "queue_dir": str(self.queue), "attempts": 0,
                        **codex_queue.route_fields()}
        self.payload = {"request": self.request, "queue_dir": str(self.queue), "timeout": 10,
                        "prompt": self.request["prompt"]}

    def accepted(self, thread=THREAD):
        return subprocess.CompletedProcess([], 0, f"Queued message {MESSAGE} for thread {thread}.\n", "")

    def publish(self, result=None):
        with mock.patch.object(codex_queue.subprocess, "run", return_value=result or self.accepted()) as run:
            value = codex_queue.publish(self.payload)
        return value, run

    def receipt(self):
        return codex_queue.read_receipt(self.queue, "native-c1")

    def retry(self, mode=None, callback_id="native-c1"):
        return cli.retry_callback(argparse.Namespace(queue_dir=str(self.queue), id=callback_id, callback_mode=mode))

    def worker_result(self, value=None):
        """process_one's delivery step, with the real publish in place of the worker process."""
        def send(*_):
            return subprocess.CompletedProcess([], (value or codex_queue.publish(self.payload))["returncode"]), False, False
        return mock.patch.object(cli, "run_resume_until_exit_or_ack", side_effect=send)


class RouteTests(QueueTestCase):
    def test_auto_desktop_uses_queue_but_server_cli_keeps_resume(self):
        args = argparse.Namespace(agent="codex", callback_mode="auto")
        self.assertEqual(transport.selection(args)["callback_mode"], "cli")
        with mock.patch.dict(os.environ, {"CODEX_INTERNAL_ORIGINATOR_OVERRIDE": "Codex Desktop"}):
            route = transport.selection(args)
        self.assertEqual(route["callback_mode"], "codex-queue")
        self.assertNotIn("callback_bridge_file", route)
        self.assertEqual(route["callback_codex_home"], str(self.root / "profile"))
        self.assertEqual(route["callback_codex_bin"], str(Path(sys.executable).absolute()))
        self.assertTrue(set(codex_queue.ROUTE_FIELDS) <= set(transport.ROUTE_FIELDS))

    def test_other_agents_on_desktop_keep_their_own_route(self):
        with mock.patch.dict(os.environ, {"CODEX_INTERNAL_ORIGINATOR_OVERRIDE": "Codex Desktop"}):
            route = transport.selection(argparse.Namespace(agent="claude", callback_mode="auto"))
        self.assertEqual(route["callback_mode"], "cli")
        with self.assertRaisesRegex(ValueError, "Codex only"):
            transport.selection(argparse.Namespace(agent="claude", callback_mode="codex-queue"))

    def test_new_work_cannot_select_the_retired_desktop_route(self):
        with self.assertRaisesRegex(ValueError, "codex-queue"):
            transport.selection(argparse.Namespace(agent="codex", callback_mode="desktop"))
        args = argparse.Namespace(agent="codex", callback_mode="desktop", session=THREAD, last=False)
        with mock.patch.object(cli, "select_execution_backend") as backend:
            self.assertEqual(cli.submit_managed_run(args), 125)
        backend.assert_not_called()
        self.assertIn("codex-queue", self.errors.getvalue())

    def test_done_with_the_retired_route_explains_itself(self):
        args = ["ltc", "done", "--agent", "codex", "--session", THREAD, "--callback-mode", "desktop",
                "--queue-dir", str(self.queue), "--task", "t", "--strict"]
        with mock.patch.object(sys, "argv", args):
            self.assertNotEqual(cli.main(), 0)
        self.assertIn("codex-queue", self.errors.getvalue())
        self.assertEqual(list((self.queue / "pending").iterdir()), [])

    def test_a_task_frozen_on_the_desktop_route_keeps_it(self):
        frozen = {"callback_mode": "desktop", "callback_origin": "desktop", "callback_bridge_file": "/saved/bridge.json"}
        args = argparse.Namespace(agent="codex", callback_mode="auto", _callback_route=frozen)
        self.assertEqual(transport.selection(args), frozen)

    def test_help_offers_only_live_modes(self):
        with mock.patch.object(sys, "argv", ["ltc", "run", "--help"]), contextlib.redirect_stdout(io.StringIO()) as out, \
                self.assertRaises(SystemExit):
            cli.main()
        self.assertIn("{auto,cli,codex-queue,manual}", out.getvalue())

    def test_preflight_only_checks_public_cli_capability(self):
        with mock.patch.object(codex_queue.subprocess, "run",
                               return_value=subprocess.CompletedProcess([], 0, "--thread --message", "")) as run:
            result = transport.inspect_route(self.request)
        self.assertEqual(result["status"], "available")
        self.assertFalse(result["end_to_end_verified"])
        self.assertEqual(run.call_args.args[0][-2:], ["queue", "--help"])

    def test_probe_uses_the_saved_session_environment(self):
        codex_queue.capture_environment(self.queue, "native-c1", {"PATH": "/session/node/bin"})
        with mock.patch.object(codex_queue.subprocess, "run",
                               return_value=subprocess.CompletedProcess([], 0, "--thread --message", "")) as run:
            transport.inspect_route(self.request)
        self.assertEqual(run.call_args.kwargs["env"]["PATH"], "/session/node/bin")

    def test_unsupported_or_missing_cli_is_blocked_before_submission(self):
        with mock.patch.object(codex_queue.subprocess, "run", return_value=subprocess.CompletedProcess([], 2, "", "unknown queue")):
            self.assertEqual(transport.inspect_route(self.request)["status"], "blocked")
        with mock.patch.object(codex_queue.subprocess, "run", side_effect=FileNotFoundError("codex")):
            self.assertEqual(transport.inspect_route(self.request)["status"], "blocked")

    def test_route_requires_the_thread_uuid(self):
        for value in ("my-session-name", "", None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                codex_queue.validate_route(dict(self.request, target={"kind": "session", "value": value}))
        with self.assertRaises(ValueError):
            codex_queue.validate_route(dict(self.request, target={"kind": "last"}))
        with self.assertRaises(ValueError):
            codex_queue.validate_route(dict(self.request, callback_codex_bin="codex"))


class VersionGateTests(QueueTestCase):
    tasks = 0

    def path(self, record):
        path = self.queue / "failed" / (record["id"] + ".json")
        cli.write_request(path, record)
        return path

    def test_new_callbacks_and_tasks_use_a_version_older_coordinators_reject(self):
        args = argparse.Namespace(agent="codex", callback_mode="codex-queue", cwd=str(self.root), task="t", session=THREAD,
                                  last=False, _callback_id="native-c1")
        request = cli.make_request(args, "prompt")
        self.assertEqual(request["version"], 5)
        self.assertEqual(cli.load_request(self.path(request))["version"], 5)
        self.assertEqual(cli.callback_route_version(dict(self.request, callback_mode="cli")), 2)
        self.assertEqual(cli.callback_route_version(dict(self.request, callback_mode="manual")), 2)

    def test_route_and_version_must_agree(self):
        with self.assertRaisesRegex(ValueError, "explicit mode"):
            cli.load_request(self.path(dict(self.request, version=2)))
        with self.assertRaisesRegex(ValueError, "version 5"):
            cli.load_request(self.path(dict(self.request, version=1)))
        with self.assertRaisesRegex(ValueError, "version 5"):
            cli.load_request(self.path(dict(self.request, callback_mode="cli")))

    def test_a_route_that_cannot_work_is_repairable_on_another_route(self):
        named = dict(self.request, target={"kind": "session", "value": "my-session-name"})
        cli.write_request(self.queue / "pending/native-c1.json", named)
        self.payload["request"] = named
        with mock.patch.object(codex_queue.subprocess, "run") as run, self.worker_result():
            self.assertTrue(cli.process_one(self.queue, argparse.Namespace(retries=3)))
        run.assert_not_called()
        record = cli.load_request(self.queue / "failed/native-c1.json")
        self.assertEqual(record["delivery_state"], "blocked")
        self.assertIn("thread UUID", record["last_error"])
        self.assertEqual(self.retry("manual"), 0)
        self.assertEqual(cli.load_request(self.queue / "pending/native-c1.json")["callback_mode"], "manual")

    def test_an_unknown_future_version_is_kept_for_the_coordinator_that_knows_it(self):
        path = self.queue / "pending/future.json"
        cli.write_request(path, dict(self.request, id="future", version=99))
        with mock.patch.object(codex_queue.subprocess, "run") as run:
            self.assertTrue(cli.process_one(self.queue, argparse.Namespace()))
        run.assert_not_called()
        self.assertEqual(json.loads((self.queue / "failed/future.json").read_text())["version"], 99)

    def task(self, **changes):
        task_id = "t" + str(self.tasks)
        self.tasks += 1
        path = cli.managed_task_path(self.queue, task_id)
        record = {"version": 6, "id": task_id, "agent": "codex", "execution_backend": "screen", "screen_session": "s",
                  "queue_dir": str(self.queue), "callback_mode": "codex-queue", "target": {"kind": "session", "value": THREAD},
                  **codex_queue.route_fields(), **changes}
        cli.write_request(path, record)
        return path

    def test_task_records_are_gated_the_same_way(self):
        self.assertEqual(cli.load_managed_task(self.task())["version"], 6)
        with self.assertRaisesRegex(ValueError, "explicit callback mode"):
            cli.load_managed_task(self.task(version=3))
        with self.assertRaisesRegex(ValueError, "version 6"):
            cli.load_managed_task(self.task(version=2))
        with self.assertRaisesRegex(ValueError, "version 6"):
            cli.load_managed_task(self.task(callback_mode="cli"))

    def test_a_saved_desktop_record_still_loads_and_keeps_its_route(self):
        saved = dict(self.request, version=2, callback_mode="desktop", callback_bridge_file="/saved/bridge.json")
        loaded = cli.load_request(self.path(saved))
        self.assertEqual(loaded["callback_mode"], "desktop")
        self.assertEqual(loaded["callback_bridge_file"], "/saved/bridge.json")


class PublishTests(QueueTestCase):
    def test_publish_is_native_queue_without_resume_and_freezes_profile(self):
        value, run = self.publish()
        self.assertEqual(value["returncode"], codex_queue.PUBLISHED_RETURN_CODE)
        command = run.call_args.args[0]
        self.assertEqual(command[1:], ["queue", "--thread", THREAD, "--message", self.request["prompt"]])
        self.assertNotIn("--resume", command)
        self.assertEqual(run.call_args.kwargs["env"]["CODEX_HOME"], self.request["callback_codex_home"])
        self.assertFalse(cli.desktop_submission_pending(self.queue, self.request))
        self.assertEqual(self.receipt()["message_id"], MESSAGE)
        self.assertEqual(self.receipt()["state"], "queued")

    def test_receipt_and_lease_are_durable_before_the_command_starts(self):
        def run(*_args, **_kwargs):
            self.assertEqual(self.receipt()["state"], "submitting")
            self.assertTrue(cli.desktop_submission_pending(self.queue, self.request))
            return self.accepted()
        with mock.patch.object(codex_queue.subprocess, "run", side_effect=run):
            self.assertEqual(codex_queue.publish(self.payload)["returncode"], codex_queue.PUBLISHED_RETURN_CODE)

    def test_second_publication_never_calls_codex(self):
        self.publish()
        value, run = self.publish()
        self.assertEqual(value["returncode"], codex_queue.PUBLISHED_RETURN_CODE)
        self.assertEqual(value["message_id"], MESSAGE)
        run.assert_not_called()

    def test_acknowledged_or_canceled_callback_is_not_sent(self):
        cli.write_request(cli.ack_path(self.queue, "native-c1"), {"id": "native-c1"})
        value, run = self.publish()
        self.assertEqual(value["skipped"], "acknowledged_or_canceled")
        run.assert_not_called()
        self.assertFalse(cli.desktop_submission_pending(self.queue, self.request))

    def assert_unknown_and_never_replayed(self, value):
        self.assertEqual(value["returncode"], 125)
        self.assertTrue(cli.desktop_submission_pending(self.queue, self.request))
        self.assertTrue(codex_queue.may_have_been_queued(self.queue, "native-c1"))
        _, run = self.publish()
        run.assert_not_called()

    def test_timeout_is_unknown(self):
        with mock.patch.object(codex_queue.subprocess, "run", side_effect=subprocess.TimeoutExpired("queue", 10)):
            self.assert_unknown_and_never_replayed(codex_queue.publish(self.payload))

    def test_kill_is_unknown(self):
        self.assert_unknown_and_never_replayed(self.publish(subprocess.CompletedProcess([], -9, "", ""))[0])

    def test_kill_reported_by_a_shell_wrapper_is_unknown(self):
        self.assert_unknown_and_never_replayed(self.publish(subprocess.CompletedProcess([], 137, "", "Killed"))[0])

    def test_exit_zero_with_unparseable_output_is_unknown(self):
        self.assert_unknown_and_never_replayed(self.publish(subprocess.CompletedProcess([], 0, "ok, probably", ""))[0])

    def test_exit_zero_for_another_thread_is_unknown(self):
        self.assert_unknown_and_never_replayed(self.publish(self.accepted("deadbeef-0000-0000-0000-000000000000"))[0])

    def test_failure_that_reports_acceptance_is_unknown(self):
        result = subprocess.CompletedProcess([], 1, f"Queued message {MESSAGE} for thread {THREAD}.", "then broke")
        self.assert_unknown_and_never_replayed(self.publish(result)[0])

    def test_unreadable_receipt_is_unknown(self):
        path = codex_queue.receipt_path(self.queue, "native-c1")
        path.parent.mkdir(parents=True)
        path.write_text("{", encoding="utf-8")
        value, run = self.publish()
        self.assertEqual(value["returncode"], 125)
        run.assert_not_called()

    def assert_repairable(self, value):
        self.assertEqual(value["returncode"], 126)
        self.assertFalse(cli.desktop_submission_pending(self.queue, self.request))
        self.assertEqual(self.receipt()["state"], "rejected")
        self.assertFalse(codex_queue.may_have_been_queued(self.queue, "native-c1"))

    def test_spawn_failure_is_repairable(self):
        with mock.patch.object(codex_queue.subprocess, "run", side_effect=FileNotFoundError("codex")):
            self.assert_repairable(codex_queue.publish(self.payload))

    def test_unresolvable_executable_on_windows_is_repairable(self):
        # prepare_command resolves the executable before the launch, and may refuse it.
        for error in (FileNotFoundError("codex"), ValueError("Unsupported Windows executable type")):
            with self.subTest(error=error), mock.patch.object(cli, "process_command", side_effect=error):
                self.assert_repairable(codex_queue.publish(self.payload))

    def test_cli_rejection_is_repairable_and_a_retry_sends_it(self):
        self.assert_repairable(self.publish(subprocess.CompletedProcess([], 1, "", "no rollout found for thread id"))[0])
        value, run = self.publish()
        self.assertEqual(value["returncode"], codex_queue.PUBLISHED_RETURN_CODE)
        run.assert_called_once()

    def test_a_node_script_without_node_is_repairable(self):
        self.assert_repairable(self.publish(subprocess.CompletedProcess([], 127, "", "env: node: No such file"))[0])

    def test_receipt_that_cannot_be_written_sends_nothing(self):
        with mock.patch.object(cli, "write_request", side_effect=OSError("disk full")), \
                mock.patch.object(cli, "retain_target_lease"), \
                mock.patch.object(codex_queue.subprocess, "run") as run:
            self.assertEqual(codex_queue.publish(self.payload)["returncode"], 126)
        run.assert_not_called()


class EnvironmentTests(QueueTestCase):
    def test_publish_runs_with_the_submitting_session_environment(self):
        codex_queue.capture_environment(self.queue, "native-c1", {"PATH": "/session/node/bin", "CODEX_HOME": "/somewhere/else",
                                                                  "SESSION_ONLY": "1"})
        _, run = self.publish()
        env = run.call_args.kwargs["env"]
        self.assertEqual(env["PATH"], "/session/node/bin")
        self.assertEqual(env["SESSION_ONLY"], "1")
        self.assertEqual(env["CODEX_HOME"], self.request["callback_codex_home"])

    def test_without_a_snapshot_the_coordinator_environment_is_used(self):
        _, run = self.publish()
        self.assertEqual(run.call_args.kwargs["env"]["PATH"], os.environ["PATH"])

    def test_snapshot_drops_markers_of_the_process_that_took_it(self):
        codex_queue.capture_environment(self.queue, "native-c1", {
            "PATH": "/p", "CODEX_THREAD_ID": THREAD, "CODEX_INTERNAL_ORIGINATOR_OVERRIDE": "Codex Desktop",
            "INVOCATION_ID": "x", "CODEX_LONG_TASK_DELIVERY_LOCK_FDS": "[3,4]"})
        self.assertEqual(codex_queue.load_environment(self.queue, "native-c1"), {"PATH": "/p"})
        with mock.patch.dict(os.environ, {"CODEX_THREAD_ID": "parent", "INVOCATION_ID": "unit"}):
            env = codex_queue.command_environment(self.request, {"PATH": "/p"})
        self.assertNotIn("CODEX_THREAD_ID", env)
        self.assertNotIn("INVOCATION_ID", env)

    @unittest.skipUnless(os.name == "posix", "POSIX file modes")
    def test_snapshot_is_private(self):
        codex_queue.capture_environment(self.queue, "native-c1", {"TOKEN": "secret"})
        path = codex_queue.environment_path(self.queue, "native-c1")
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)

    def assert_snapshot(self, expected):
        self.assertEqual(codex_queue.environment_path(self.queue, "native-c1").exists(), expected)

    def test_snapshot_goes_once_nothing_can_send_again(self):
        codex_queue.capture_environment(self.queue, "native-c1")
        self.publish(subprocess.CompletedProcess([], 1, "", "no such thread"))
        self.assert_snapshot(True)  # kept for `ltc retry`
        self.publish()
        self.assert_snapshot(False)

    def test_unknown_outcome_discards_the_snapshot(self):
        codex_queue.capture_environment(self.queue, "native-c1")
        self.publish(subprocess.CompletedProcess([], -9, "", ""))
        self.assert_snapshot(False)

    def test_ack_cancel_and_the_daemon_discard_the_snapshot(self):
        codex_queue.capture_environment(self.queue, "native-c1")
        self.assertEqual(cli.ack(argparse.Namespace(queue_dir=str(self.queue), id="native-c1", message=None)), 0)
        self.assert_snapshot(False)
        codex_queue.capture_environment(self.queue, "native-c2")
        cli.write_request(self.queue / "pending/native-c2.json", dict(self.request, id="native-c2"))
        self.assertTrue(cli.cancel_one(self.queue, "native-c2"))
        codex_queue.reconcile(self.queue)
        self.assertFalse(codex_queue.environment_path(self.queue, "native-c2").exists())

    def test_managed_run_captures_before_the_task_exists(self):
        seen = []
        real = cli.write_managed_task
        def write(root, task):
            seen.append(codex_queue.load_environment(root, task["id"]))
            real(root, task)
        args = argparse.Namespace(agent="codex", callback_mode="codex-queue", session=THREAD, last=False, cwd=str(self.root),
                                  queue_dir=str(self.queue), task="t", command=None, message=None, strict=False,
                                  wrapped_command=["true"], backend="screen", goal_id=None)
        with mock.patch.dict(os.environ, {"SESSION_ONLY": "1"}), \
                mock.patch.object(cli, "write_managed_task", side_effect=write), \
                mock.patch.object(cli, "select_execution_backend", return_value="screen"), \
                mock.patch.object(transport, "inspect_route", return_value={"status": "available"}), \
                mock.patch.object(cli, "ensure_daemon_dirs"), \
                mock.patch.object(cli, "launch_managed_task", create=True):
            self.assertEqual(cli.submit_managed_run(args), 0)
        self.assertEqual(seen[0]["SESSION_ONLY"], "1")
        task_id = args._health_work["id"]
        task = cli.load_managed_task(cli.managed_task_path(self.queue, task_id))
        self.assertEqual(task["version"], codex_queue.TASK_VERSION)
        self.assertEqual(task["callback_mode"], "codex-queue")

    def test_done_captures_and_a_failed_enqueue_removes_it(self):
        args = argparse.Namespace(agent="codex", callback_mode="codex-queue", session=THREAD, last=False, cwd=str(self.root),
                                  queue_dir=str(self.queue), task="t", command=None, message=None, exit_code=0,
                                  _callback_id="native-c1")
        with mock.patch.dict(os.environ, {"SESSION_ONLY": "1"}), \
                mock.patch.object(cli, "enqueue_existing_request", return_value=1):
            self.assertEqual(cli.enqueue_request(args, "prompt"), 1)
        self.assert_snapshot(False)
        with mock.patch.dict(os.environ, {"SESSION_ONLY": "1"}):
            self.assertEqual(cli.enqueue_request(args, "prompt"), 0)
        self.assertEqual(codex_queue.load_environment(self.queue, "native-c1")["SESSION_ONLY"], "1")
        self.assertEqual(cli.load_request(self.queue / "pending/native-c1.json")["version"], 5)


class LifecycleTests(QueueTestCase):
    def pending(self, callback_id="native-c1"):
        path = self.queue / "pending" / (callback_id + ".json")
        cli.write_request(path, dict(self.request, id=callback_id))
        return path

    def deliver(self, value=None):
        with mock.patch.object(codex_queue.subprocess, "run", return_value=value or self.accepted()), self.worker_result():
            self.assertTrue(cli.process_one(self.queue, argparse.Namespace(retries=3)))

    def test_unanswered_ack_does_not_hold_back_later_callbacks(self):
        first = self.pending()
        self.deliver()
        later = self.pending("native-c2")
        self.assertEqual(cli.select_pending(self.queue, 1e12), later)
        record = cli.load_request(first)
        self.assertEqual(record["delivery_state"], codex_queue.AWAITING_ACK)
        self.assertEqual(record["attempts"], 0)
        self.assertEqual(record["codex_queue_message_id"], MESSAGE)
        self.assertFalse(cli.target_has_live_resume(self.queue, record))

    def test_published_is_pending_until_ack_and_survives_restart_without_resend(self):
        path = self.pending()
        self.deliver()
        self.assertFalse((self.queue / "done/native-c1.json").exists())
        cli.recover_running(self.queue)
        self.assertIsNone(cli.select_pending(self.queue, 1e12))
        self.assertFalse(cli.process_one(self.queue, argparse.Namespace()))
        self.assertEqual(cli.ack(argparse.Namespace(queue_dir=str(self.queue), id="native-c1", message="received")), 0)
        self.assertTrue((self.queue / "done/native-c1.json").is_file())
        self.assertFalse(path.exists())
        self.assertFalse(cli.retained_target_lease_is_held(self.request))

    def test_ack_that_races_the_move_back_to_pending_still_finishes(self):
        self.pending()
        self.deliver()
        cli.write_request(cli.ack_path(self.queue, "native-c1"), {"id": "native-c1"})
        with mock.patch.object(codex_queue.subprocess, "run") as run:
            self.assertTrue(cli.process_one(self.queue, argparse.Namespace()))
        run.assert_not_called()
        self.assertTrue((self.queue / "done/native-c1.json").is_file())

    def test_ack_blocked_by_a_sandbox_explains_itself_and_changes_nothing(self):
        path = self.pending()
        self.deliver()
        with mock.patch.object(cli, "write_request", side_effect=PermissionError(13, "Read-only file system")):
            self.assertEqual(cli.ack(argparse.Namespace(queue_dir=str(self.queue), id="native-c1", message=None)), 1)
        self.assertIn("could not record the ACK", self.errors.getvalue())
        self.assertIn("approve this command", self.errors.getvalue())
        self.assertTrue(path.is_file())
        self.assertFalse(cli.ack_path(self.queue, "native-c1").exists())

    def test_status_shows_it_is_waiting_for_the_ack(self):
        self.pending()
        self.deliver()
        with contextlib.redirect_stdout(io.StringIO()) as out:
            cli.status(argparse.Namespace(queue_dir=str(self.queue), state=None, limit=10, quiet_empty=False))
        self.assertIn("in the Codex queue; waiting for the thread to ACK", out.getvalue())
        self.assertIn("native-c1", out.getvalue())

    def test_rejection_goes_to_failed_with_its_cause_and_retry_delivers_once(self):
        self.pending()
        self.deliver(subprocess.CompletedProcess([], 1, "", "no rollout found for thread id"))
        failed = self.queue / "failed/native-c1.json"
        record = cli.load_request(failed)
        self.assertEqual(record["delivery_state"], "blocked")
        self.assertIn("no rollout found", record["last_error"])
        self.assertIn("ltc retry --id native-c1", record["last_error"])
        self.assertNotIn("retain_target_lease", record)
        with mock.patch.object(transport, "inspect_route", return_value={"status": "available"}):
            self.assertEqual(self.retry(), 0)
        retried = cli.load_request(self.queue / "pending/native-c1.json")
        self.assertNotIn("delivery_state", retried)
        self.assertEqual(retried["retry_history"][0]["last_error"], record["last_error"])
        self.deliver()
        self.assertEqual(cli.load_request(self.queue / "pending/native-c1.json")["delivery_state"], codex_queue.AWAITING_ACK)

    def test_unknown_outcome_blocks_retry_and_the_thread_until_ack_or_cancel(self):
        self.pending()
        self.deliver(subprocess.CompletedProcess([], -9, "", ""))
        failed = self.queue / "failed/native-c1.json"
        record = cli.load_request(failed)
        self.assertIs(record["retain_target_lease"], True)
        self.assertIn("ltc ack or ltc cancel", record["last_error"])
        self.assertEqual(self.retry(), 2)
        self.assertEqual(self.retry("cli"), 2)
        self.assertTrue(failed.is_file())
        later = self.pending("native-c2")
        self.assertIsNone(cli.select_pending(self.queue, 1e12))
        self.assertTrue(cli.cancel_one(self.queue, "native-c1"))
        self.assertEqual(cli.select_pending(self.queue, 1e12), later)

    def test_retry_refuses_any_route_after_a_message_was_accepted(self):
        self.pending()
        self.deliver()
        record = cli.load_request(self.queue / "pending/native-c1.json")
        self.queue.joinpath("pending/native-c1.json").unlink()
        cli.write_request(self.queue / "failed/native-c1.json", record)
        self.assertEqual(self.retry("cli"), 2)
        self.assertIn("outcome is unknown", self.errors.getvalue())

    def test_retry_cannot_move_a_callback_back_to_desktop(self):
        cli.write_request(self.queue / "failed/native-c1.json", dict(self.request, version=2, callback_mode="cli"))
        self.assertEqual(self.retry("desktop"), 2)
        self.assertIn("codex-queue", self.errors.getvalue())
        self.assertEqual(cli.load_request(self.queue / "failed/native-c1.json")["callback_mode"], "cli")


class RecoveryTests(QueueTestCase):
    def running(self):
        path = self.queue / "running/native-c1.json"
        cli.write_request(path, self.request)
        return path

    def receipt_state(self, state, **fields):
        cli.write_request(codex_queue.receipt_path(self.queue, "native-c1"),
                          {"callback_id": "native-c1", "state": state, **fields})

    def test_worker_killed_during_the_command_is_unknown(self):
        self.running()
        cli.retain_target_lease(self.queue, self.request)
        self.receipt_state("submitting")
        cli.recover_running(self.queue)
        record = cli.load_request(self.queue / "failed/native-c1.json")
        self.assertIs(record["retain_target_lease"], True)
        self.assertEqual(record["last_error"], codex_queue.UNKNOWN_ERROR)
        self.assertTrue(cli.desktop_submission_pending(self.queue, self.request))

    def test_worker_killed_after_acceptance_keeps_the_acceptance(self):
        self.running()
        cli.retain_target_lease(self.queue, self.request)
        self.receipt_state("queued", message_id=MESSAGE)
        cli.recover_running(self.queue)
        record = cli.load_request(self.queue / "pending/native-c1.json")
        self.assertEqual(record["delivery_state"], codex_queue.AWAITING_ACK)
        self.assertEqual(record["codex_queue_message_id"], MESSAGE)
        self.assertFalse(cli.desktop_submission_pending(self.queue, self.request))
        self.assertIsNone(cli.select_pending(self.queue, 1e12))

    def test_worker_killed_before_the_receipt_sent_nothing(self):
        self.running()
        cli.retain_target_lease(self.queue, self.request)
        cli.recover_running(self.queue)
        self.assertTrue((self.queue / "pending/native-c1.json").is_file())
        self.assertFalse(cli.desktop_submission_pending(self.queue, self.request))
        self.assertIsNotNone(cli.select_pending(self.queue, 1e12))

    def test_worker_killed_while_recording_a_rejection_sent_nothing(self):
        self.running()
        cli.retain_target_lease(self.queue, self.request)
        self.receipt_state("rejected", error="no such thread")
        cli.recover_running(self.queue)
        self.assertTrue((self.queue / "pending/native-c1.json").is_file())
        self.assertFalse(cli.desktop_submission_pending(self.queue, self.request))

    def test_ack_wins_over_any_receipt(self):
        self.running()
        self.receipt_state("submitting")
        cli.write_request(cli.ack_path(self.queue, "native-c1"), {"id": "native-c1"})
        cli.recover_running(self.queue)
        self.assertTrue((self.queue / "done/native-c1.json").is_file())

    def test_daemon_releases_a_lease_the_acceptance_could_not(self):
        path = self.queue / "pending/native-c1.json"
        cli.write_request(path, dict(self.request, delivery_state=codex_queue.AWAITING_ACK, live_delivered_at=1.0))
        cli.retain_target_lease(self.queue, self.request)
        codex_queue.reconcile(self.queue)
        self.assertFalse(cli.desktop_submission_pending(self.queue, self.request))
        self.assertTrue(path.is_file())


class SavedDesktopRecordTests(QueueTestCase):
    def saved(self):
        return dict(self.request, id="saved", version=2, callback_mode="desktop", sandbox_mode="workspace-write",
                    callback_bridge_file=str(self.root / "profile/long-task-wakeup/desktop/bridge.json"),
                    log_path="/saved/attempt-1.log", exit_code=0)

    def test_unreachable_bridge_is_blocked_with_the_exact_command(self):
        for platform in ("darwin", "win32"):
            with self.subTest(platform=platform), mock.patch.object(sys, "platform", platform):
                report = transport.inspect_route(self.saved())
                self.assertEqual(report["status"], "blocked")
                self.assertIn("ltc retry --id saved --callback-mode codex-queue", report["action"])

    def test_daemon_retains_the_blocked_record_and_names_the_repair(self):
        cli.write_request(self.queue / "pending/saved.json", self.saved())
        with mock.patch.object(cli, "run_resume_until_exit_or_ack",
                               return_value=(subprocess.CompletedProcess([], 126), False, False)):
            self.assertTrue(cli.process_one(self.queue, argparse.Namespace(retries=10)))
        saved = cli.load_request(self.queue / "failed/saved.json")
        self.assertEqual(saved["delivery_state"], "blocked")
        self.assertEqual(saved["exit_code"], 0)
        self.assertEqual(saved["log_path"], "/saved/attempt-1.log")
        self.assertIn("ltc retry --id saved --callback-mode codex-queue", saved["last_error"])

    def test_retry_without_a_new_mode_repeats_the_command(self):
        cli.write_request(self.queue / "failed/saved.json", self.saved())
        with mock.patch.object(sys, "platform", "darwin"):
            self.assertEqual(self.retry(callback_id="saved"), 2)
        self.assertIn("ltc retry --id saved --callback-mode codex-queue", self.errors.getvalue())
        self.assertTrue((self.queue / "failed/saved.json").is_file())

    def test_retry_moves_the_record_to_the_queue_without_touching_the_workload(self):
        cli.write_request(self.queue / "failed/saved.json", self.saved())
        with mock.patch.dict(os.environ, {"SESSION_ONLY": "from-the-retry-shell"}), \
                mock.patch.object(transport, "inspect_route", return_value={"status": "available"}), \
                mock.patch.object(cli, "submit_managed_run") as launch:
            self.assertEqual(self.retry("codex-queue", callback_id="saved"), 0)
        launch.assert_not_called()
        moved = cli.load_request(self.queue / "pending/saved.json")
        self.assertEqual(moved["version"], codex_queue.CALLBACK_VERSION)
        self.assertEqual(moved["callback_mode"], "codex-queue")
        self.assertNotIn("callback_bridge_file", moved)
        self.assertEqual(moved["log_path"], "/saved/attempt-1.log")
        self.assertEqual(moved["exit_code"], 0)
        self.assertEqual(codex_queue.load_environment(self.queue, "saved")["SESSION_ONLY"], "from-the-retry-shell")

    def test_launcher_commands_explain_the_replacement(self):
        for arguments in (["desktop", "launch"], ["desktop", "prepare", "--force"], ["desktop", "status"]):
            with self.subTest(arguments=arguments), mock.patch.object(sys, "argv", ["ltc", *arguments]):
                self.assertEqual(cli.main(), 2)
                self.assertIn("--callback-mode codex-queue", self.errors.getvalue())

    def test_setup_names_a_leftover_launcher_without_deleting_it(self):
        launcher = self.root / "profile/long-task-wakeup/Start LTC Desktop.command"
        launcher.parent.mkdir(parents=True)
        launcher.write_text("#!/bin/sh\n", encoding="utf-8")
        cli.report_retired_desktop_launcher()
        self.assertIn(str(launcher), self.errors.getvalue())
        self.assertTrue(launcher.is_file())


class PromptTests(QueueTestCase):
    def test_receipt_instructions_describe_native_permissions_and_no_replay(self):
        text = cli.build_acknowledgement_text("ltc ack --id native-c1", callback_mode="codex-queue")
        self.assertIn("keeps its existing permissions", text)
        self.assertIn("published once", text)
        self.assertNotIn("retry this callback", text)
        self.assertIn("retry this callback", cli.build_acknowledgement_text("ltc ack --id x", callback_mode="cli"))


if __name__ == "__main__":
    unittest.main()
