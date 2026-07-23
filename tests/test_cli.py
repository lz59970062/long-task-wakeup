from __future__ import annotations

import argparse
import json
import os
import shlex
import signal
import stat
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path
from unittest import mock

from long_task_callback import cli


class CliTests(unittest.TestCase):
    def tearDown(self) -> None:
        for delivery in list(cli._BACKGROUND_RESUMES.values()):
            cli.stop_resume_process(delivery.process)
        cli._BACKGROUND_RESUMES.clear()

    def test_via_daemon_run_preserves_exit_code_and_one_callback_id(self) -> None:
        for exit_code in (0, 7):
            with self.subTest(exit_code=exit_code), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp) / "queue"
                args = self._run_args(tmp, root, [sys.executable, "-c", f"raise SystemExit({exit_code})"])

                self.assertEqual(cli.run(args), exit_code)

                pending = list((root / "pending").glob("*.json"))
                self.assertEqual(len(pending), 1)
                request = json.loads(pending[0].read_text(encoding="utf-8"))
                self.assertEqual(request["id"], pending[0].stem)
                self.assertEqual(request["exit_code"], exit_code)
                self.assertEqual(request["outcome"], "completed")
                self.assertEqual(request["lifecycle_state"], "pending")
                self.assertIn(f"Exit code: {exit_code}", request["prompt"])
                self.assertFalse(list((root / cli.ACTIVE_STATE).glob("*.json")))

    def test_active_owner_lock_blocks_premature_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "queue"
            args = self._run_args(tmp, root, [sys.executable, "-c", "pass"])
            request = cli.make_active_request(args, time.time())
            request["launch_phase"] = "launch_committed"
            request_id = str(request["id"])
            lock = cli.acquire_owner_lock(root, request_id, blocking=True)
            self.assertIsNotNone(lock)
            cli.write_request(cli.request_path(root, cli.ACTIVE_STATE, request_id), request)

            self.assertEqual(cli.recover_active(root), 0)
            self.assertTrue(cli.request_path(root, cli.ACTIVE_STATE, request_id).exists())

            cli.release_owner_lock(lock, remove=False)
            self.assertEqual(cli.recover_active(root), 1)
            recovered = json.loads(cli.request_path(root, "pending", request_id).read_text(encoding="utf-8"))
            self.assertEqual(recovered["outcome"], "unknown")
            self.assertIn("may still be running", recovered["prompt"])
            self.assertIn("exit status are unknown", recovered["prompt"])

    def test_daemon_singleton_uses_stable_non_removed_lock_inode(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "queue"
            first = cli.acquire_owner_lock(root, "daemon-singleton", blocking=False)
            self.assertIsNotNone(first)
            self.assertIsNone(cli.acquire_owner_lock(root, "daemon-singleton", blocking=False))
            lock_path = cli.owner_lock_path(root, "daemon-singleton")

            cli.release_owner_lock(first, remove=False)
            self.assertTrue(lock_path.exists())
            handoff = cli.acquire_owner_lock(root, "daemon-singleton", blocking=False)
            self.assertIsNotNone(handoff)
            cli.release_owner_lock(handoff, remove=False)
            self.assertTrue(lock_path.exists())

    def test_wrapper_sigkill_recovers_prearmed_callback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "queue"
            child_pid_path = Path(tmp) / "child.pid"
            child_code = (
                "import os,time,pathlib; "
                f"pathlib.Path({str(child_pid_path)!r}).write_text(str(os.getpid())); "
                "time.sleep(60)"
            )
            command = [
                sys.executable,
                "-m",
                "long_task_callback",
                "run",
                "--via-daemon",
                "--session",
                "sigkill-thread",
                "--queue-dir",
                str(root),
                "--cwd",
                tmp,
                "--task",
                "wrapper sigkill",
                "--",
                sys.executable,
                "-c",
                child_code,
            ]
            env = os.environ.copy()
            env["PYTHONPATH"] = str(Path(cli.__file__).parents[1])
            wrapper = subprocess.Popen(
                command,
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            child_pid = None
            try:
                deadline = time.time() + 10.0
                while time.time() < deadline:
                    active = list((root / cli.ACTIVE_STATE).glob("*.json"))
                    if active and child_pid_path.exists():
                        child_pid = int(child_pid_path.read_text(encoding="utf-8"))
                        break
                    time.sleep(0.05)
                self.assertIsNotNone(child_pid, "wrapper never armed the callback and launched the child")

                os.kill(wrapper.pid, signal.SIGKILL)
                wrapper.wait(timeout=5)
                self.assertEqual(cli.recover_active(root), 1)

                pending = list((root / "pending").glob("*.json"))
                self.assertEqual(len(pending), 1)
                request = json.loads(pending[0].read_text(encoding="utf-8"))
                self.assertEqual(request["outcome"], "unknown")
                self.assertEqual(request["target"], {"kind": "session", "value": "sigkill-thread"})
                self.assertIn("wrapper disappeared", request["prompt"])
            finally:
                if wrapper.poll() is None:
                    wrapper.kill()
                    wrapper.wait(timeout=5)
                if child_pid is not None:
                    try:
                        os.kill(child_pid, signal.SIGTERM)
                    except ProcessLookupError:
                        pass

    def test_wrapper_sigint_records_unknown_instead_of_fabricated_completion(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "queue"
            child_pid_path = Path(tmp) / "sigint-child.pid"
            child_code = (
                "import os,time,pathlib; "
                f"pathlib.Path({str(child_pid_path)!r}).write_text(str(os.getpid())); "
                "time.sleep(30)"
            )
            command = [
                sys.executable,
                "-m",
                "long_task_callback",
                "run",
                "--via-daemon",
                "--session",
                "sigint-thread",
                "--queue-dir",
                str(root),
                "--cwd",
                tmp,
                "--task",
                "wrapper sigint",
                "--",
                sys.executable,
                "-c",
                child_code,
            ]
            env = os.environ.copy()
            env["PYTHONPATH"] = str(Path(cli.__file__).parents[1])
            wrapper = subprocess.Popen(command, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            child_pid = None
            try:
                deadline = time.time() + 10.0
                while time.time() < deadline:
                    if list((root / cli.ACTIVE_STATE).glob("*.json")) and child_pid_path.exists():
                        child_pid = int(child_pid_path.read_text(encoding="utf-8"))
                        break
                    time.sleep(0.05)
                self.assertIsNotNone(child_pid)

                os.kill(wrapper.pid, signal.SIGINT)
                wrapper.wait(timeout=5)
                pending = list((root / "pending").glob("*.json"))
                self.assertEqual(len(pending), 1)
                request = json.loads(pending[0].read_text(encoding="utf-8"))
                self.assertEqual(request["outcome"], "unknown")
                self.assertNotIn("exit_code", request)
                self.assertIn("before it observed a child return code", request["prompt"])
            finally:
                if wrapper.poll() is None:
                    wrapper.kill()
                    wrapper.wait(timeout=5)
                if child_pid is not None:
                    try:
                        os.kill(child_pid, signal.SIGTERM)
                    except ProcessLookupError:
                        pass

    def test_prearm_failure_refuses_to_launch_unprotected_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "queue"
            args = self._run_args(tmp, root, [sys.executable, "-c", "pass"])
            args.strict = True

            with (
                mock.patch.object(cli, "write_request", side_effect=OSError("disk unavailable")),
                mock.patch.object(cli.subprocess, "run") as wrapped,
            ):
                self.assertEqual(cli.run(args), 125)

            wrapped.assert_not_called()

    def test_non_strict_prearm_failure_runs_with_best_effort_exit_code(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "queue"
            marker = Path(tmp) / "wrapped-ran"
            args = self._run_args(
                tmp,
                root,
                [sys.executable, "-c", f"from pathlib import Path; Path({str(marker)!r}).write_text('yes'); raise SystemExit(6)"],
            )

            def fail_release(lock: tuple[object, Path] | None, *, remove: bool) -> None:
                if lock is not None:
                    handle, _ = lock
                    handle.close()
                raise OSError("cleanup unavailable")

            with (
                mock.patch.object(cli, "write_request", side_effect=OSError("disk unavailable")),
                mock.patch.object(cli, "release_owner_lock", side_effect=fail_release),
            ):
                self.assertEqual(cli.run(args), 6)

            self.assertEqual(marker.read_text(encoding="utf-8"), "yes")

    def test_non_strict_finalization_and_lock_cleanup_failures_preserve_task_exit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "queue"
            args = self._run_args(tmp, root, [sys.executable, "-c", "pass"])

            with mock.patch.object(cli, "transition_active_to_pending", side_effect=OSError("finalize failed")):
                self.assertEqual(cli.run(args), 0)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "queue"
            args = self._run_args(tmp, root, [sys.executable, "-c", "pass"])

            def fail_release(lock: tuple[object, Path] | None, *, remove: bool) -> None:
                if lock is not None:
                    handle, _ = lock
                    handle.close()
                raise OSError("lock cleanup failed")

            with mock.patch.object(cli, "release_owner_lock", side_effect=fail_release):
                self.assertEqual(cli.run(args), 0)

    def test_wrapped_launch_exception_records_unknown_and_propagates_exception(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "queue"
            args = self._run_args(tmp, root, ["missing-command"])

            with mock.patch.object(cli.subprocess, "run", side_effect=OSError("exec failed")):
                with self.assertRaisesRegex(OSError, "exec failed"):
                    cli.run(args)

            pending = list((root / "pending").glob("*.json"))
            self.assertEqual(len(pending), 1)
            request = json.loads(pending[0].read_text(encoding="utf-8"))
            self.assertEqual(request["outcome"], "unknown")
            self.assertNotIn("exit_code", request)

    def test_cancel_active_is_tombstone_first_and_does_not_kill_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "queue"
            child_pid_path = Path(tmp) / "cancel-child.pid"
            child_code = (
                "import os,time,pathlib; "
                f"pathlib.Path({str(child_pid_path)!r}).write_text(str(os.getpid())); "
                "time.sleep(1.0)"
            )
            command = [
                sys.executable,
                "-m",
                "long_task_callback",
                "run",
                "--via-daemon",
                "--session",
                "cancel-thread",
                "--queue-dir",
                str(root),
                "--cwd",
                tmp,
                "--task",
                "cancel active",
                "--",
                sys.executable,
                "-c",
                child_code,
            ]
            env = os.environ.copy()
            env["PYTHONPATH"] = str(Path(cli.__file__).parents[1])
            wrapper = subprocess.Popen(command, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                deadline = time.time() + 10.0
                active: list[Path] = []
                while time.time() < deadline:
                    active = list((root / cli.ACTIVE_STATE).glob("*.json"))
                    if active and child_pid_path.exists():
                        break
                    time.sleep(0.05)
                self.assertEqual(len(active), 1)
                child_pid = int(child_pid_path.read_text(encoding="utf-8"))

                self.assertTrue(cli.cancel_one(root, active[0].stem, "user canceled callback only"))
                os.kill(child_pid, 0)
                self.assertIsNone(wrapper.poll())
                self.assertEqual(wrapper.wait(timeout=5), 0)

                self.assertTrue(cli.request_path(root, "canceled", active[0].stem).exists())
                self.assertFalse(cli.request_path(root, cli.ACTIVE_STATE, active[0].stem).exists())
                self.assertFalse(cli.request_path(root, "pending", active[0].stem).exists())
            finally:
                if wrapper.poll() is None:
                    wrapper.kill()
                    wrapper.wait(timeout=5)

    def test_recover_active_preserves_stranded_completed_result(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "queue"
            args = self._run_args(tmp, root, [sys.executable, "-c", "raise SystemExit(9)"])
            args.command = shlex.join(args.wrapped_command)
            args.exit_code = 9
            request = cli.make_active_request(args, time.time() - 3.0)
            request.update(
                {
                    "launch_phase": "launch_committed",
                    "outcome": "completed",
                    "exit_code": 9,
                    "completed_at": time.time(),
                }
            )
            cli.prepare_request_for_queue(root, request, cli.build_prompt(args, 3.0))
            request_id = str(request["id"])
            cli.write_request(cli.request_path(root, cli.ACTIVE_STATE, request_id), request)

            self.assertEqual(cli.recover_active(root), 1)
            recovered = json.loads(cli.request_path(root, "pending", request_id).read_text(encoding="utf-8"))
            self.assertEqual(recovered["outcome"], "completed")
            self.assertEqual(recovered["exit_code"], 9)
            self.assertIn("Exit code: 9", recovered["prompt"])

    def test_recover_active_discards_definitely_unlaunched_record(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "queue"
            args = self._run_args(tmp, root, [sys.executable, "-c", "pass"])
            request = cli.make_active_request(args, time.time())
            request_id = str(request["id"])
            cli.write_request(cli.request_path(root, cli.ACTIVE_STATE, request_id), request)

            self.assertEqual(cli.recover_active(root), 0)
            self.assertFalse(cli.request_path(root, cli.ACTIVE_STATE, request_id).exists())
            self.assertFalse(cli.request_path(root, "pending", request_id).exists())

    def test_make_request_binds_codex_thread_id_by_default(self) -> None:
        args = argparse.Namespace(
            cwd="/tmp",
            session=None,
            last=False,
            approvals_reviewer="auto_review",
            approval_policy="on-request",
            sandbox_mode="workspace-write",
        )

        with mock.patch.dict(os.environ, {"CODEX_THREAD_ID": "thread-from-env"}, clear=False):
            request = cli.make_request(args, "wake up")

        self.assertEqual(request["target"], {"kind": "session", "value": "thread-from-env"})
        self.assertEqual(request["target_source"], "CODEX_THREAD_ID")

    def test_make_request_refuses_ambiguous_target(self) -> None:
        args = argparse.Namespace(
            cwd="/tmp",
            session=None,
            last=False,
            approvals_reviewer="auto_review",
            approval_policy="on-request",
            sandbox_mode="workspace-write",
        )

        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(SystemExit, "CODEX_THREAD_ID is unset"):
                cli.make_request(args, "wake up")

    def test_explicit_session_overrides_environment(self) -> None:
        args = argparse.Namespace(
            cwd="/tmp",
            session="explicit-thread",
            last=False,
            approvals_reviewer="auto_review",
            approval_policy="on-request",
            sandbox_mode="workspace-write",
        )

        with mock.patch.dict(os.environ, {"CODEX_THREAD_ID": "thread-from-env"}, clear=False):
            request = cli.make_request(args, "wake up")

        self.assertEqual(request["target"], {"kind": "session", "value": "explicit-thread"})
        self.assertEqual(request["target_source"], "--session")

    def test_bound_target_is_snapshotted_before_environment_changes(self) -> None:
        args = argparse.Namespace(
            cwd="/tmp",
            session=None,
            last=False,
            approvals_reviewer="auto_review",
            approval_policy="on-request",
            sandbox_mode="workspace-write",
        )

        with mock.patch.dict(os.environ, {"CODEX_THREAD_ID": "launch-thread"}, clear=False):
            cli.bind_target(args)
        with mock.patch.dict(os.environ, {"CODEX_THREAD_ID": "later-thread"}, clear=False):
            request = cli.make_request(args, "wake up")

        self.assertEqual(request["target"], {"kind": "session", "value": "launch-thread"})
        self.assertEqual(request["target_source"], "CODEX_THREAD_ID")

    def test_resume_command_uses_auto_review_by_default(self) -> None:
        command = cli.resume_command(
            {
                "target": {"kind": "session", "value": "session-1"},
                "cwd": "/tmp",
                "prompt": "hello",
            }
        )

        self.assertIn("-c", command)
        self.assertIn('approvals_reviewer="auto_review"', command)
        self.assertIn('approval_policy="on-request"', command)
        self.assertIn('sandbox_mode="workspace-write"', command)
        self.assertEqual(command[-2:], ["session-1", "-"])

    def test_enqueue_adds_acknowledgement_instruction(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            args = argparse.Namespace(
                cwd=tmp,
                task="train",
                command=None,
                exit_code=0,
                message=None,
                session="session-1",
                last=False,
                queue_dir=tmp,
                approvals_reviewer="auto_review",
                approval_policy="on-request",
                sandbox_mode="workspace-write",
            )

            self.assertEqual(cli.enqueue_request(args, cli.build_prompt(args)), 0)
            queued = list((Path(tmp) / "pending").glob("*.json"))
            self.assertEqual(len(queued), 1)
            request = json.loads(queued[0].read_text(encoding="utf-8"))

            self.assertIn("codex-long-task-wakeup", request["prompt"])
            self.assertIn(" ack ", request["prompt"])
            self.assertRegex(request["prompt"], r"Callback time: .+[+-]\d{2}:\d{2}")
            self.assertIn(str(request["id"]), request["prompt"])
            self.assertIn("Bound session: session-1", request["prompt"])
            self.assertIn("Binding source: --session", request["prompt"])
            self.assertIn("never redirect this callback to --last", request["prompt"])
            self.assertEqual(request["queue_dir"], tmp)

    def test_resume_command_makes_queue_dir_writable(self) -> None:
        command = cli.resume_command(
            {
                "target": {"kind": "last"},
                "cwd": "/tmp",
                "prompt": "hello",
                "queue_dir": "/tmp/callback-queue",
            }
        )

        self.assertIn('sandbox_workspace_write.writable_roots=["/tmp/callback-queue"]', command)

    def test_daemon_requeues_when_agent_does_not_ack(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "queue"
            self._write_request(root, "missing-ack", tmp)
            fake_codex = self._fake_codex(Path(tmp), ack=False, queue=root, request_id="missing-ack")
            args = argparse.Namespace(retries=3, retry_delay=0.0, retry_backoff=2.0)

            with mock.patch.dict(os.environ, {"CODEX_LONG_TASK_WAKEUP_CODEX_BIN": str(fake_codex)}):
                self.assertTrue(cli.process_one(root, args))

            pending = root / "pending" / "missing-ack.json"
            self.assertTrue(pending.exists())
            request = json.loads(pending.read_text(encoding="utf-8"))
            self.assertEqual(request["attempts"], 1)
            self.assertIn("last_error", request)
            self.assertFalse((root / "done" / "missing-ack.json").exists())

    def test_daemon_marks_done_only_after_ack(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "queue"
            self._write_request(root, "acked", tmp)
            fake_codex = self._fake_codex(Path(tmp), ack=True, queue=root, request_id="acked")
            args = argparse.Namespace(retries=3, retry_delay=0.0, retry_backoff=2.0)

            with mock.patch.dict(os.environ, {"CODEX_LONG_TASK_WAKEUP_CODEX_BIN": str(fake_codex)}):
                self.assertTrue(cli.process_one(root, args))

            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline and not (root / "done" / "acked.json").exists():
                cli.reap_background_resumes()
                cli.recover_running(root)
                time.sleep(0.02)
            self.assertTrue((root / "done" / "acked.json").exists())
            self.assertTrue((root / "acks" / "acked.json").exists())
            self.assertFalse((root / "pending" / "acked.json").exists())

    def test_daemon_reconciles_pending_request_with_existing_ack(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "queue"
            self._write_request(root, "late-ack", tmp)
            cli.ensure_daemon_dirs(root)
            (root / "acks" / "late-ack.json").write_text("{}\n", encoding="utf-8")
            args = argparse.Namespace(retries=3, retry_delay=0.0, retry_backoff=2.0)

            with mock.patch.object(cli.subprocess, "run") as run:
                self.assertTrue(cli.process_one(root, args))

            run.assert_not_called()
            self.assertTrue((root / "done" / "late-ack.json").exists())

    def test_daemon_times_out_stuck_resume_and_requeues(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "queue"
            self._write_request(root, "stuck", tmp)
            args = argparse.Namespace(retries=3, retry_delay=0.0, retry_backoff=2.0, resume_timeout=1.0)

            timed_out = subprocess.CompletedProcess(["codex"], 124)
            with mock.patch.object(cli, "run_resume_until_exit_or_ack", return_value=(timed_out, False, False)):
                self.assertTrue(cli.process_one(root, args))

            request = json.loads((root / "pending" / "stuck.json").read_text(encoding="utf-8"))
            self.assertEqual(request["last_error"], "Codex resume timed out")

    def test_daemon_recovers_stale_running_requests(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "queue"
            self._write_request(root, "unacked", tmp)
            cli.ensure_daemon_dirs(root)
            os.replace(root / "pending" / "unacked.json", root / "running" / "unacked.json")
            self._write_request(root, "acked", tmp)
            os.replace(root / "pending" / "acked.json", root / "running" / "acked.json")
            (root / "acks" / "acked.json").write_text("{}\n", encoding="utf-8")

            cli.recover_running(root)

            self.assertTrue((root / "pending" / "unacked.json").exists())
            self.assertTrue((root / "done" / "acked.json").exists())

    def test_move_request_collision_is_idempotent_only_for_same_valid_id(self) -> None:
        def payload(request_id: str, cwd: str, prompt: str) -> dict[str, object]:
            return {
                "version": 1,
                "id": request_id,
                "created_at": 1.0,
                "cwd": cwd,
                "target": {"kind": "last"},
                "prompt": prompt,
            }

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "queue"
            cli.ensure_daemon_dirs(root)
            source = cli.request_path(root, cli.ACTIVE_STATE, "same-id")
            destination = cli.request_path(root, "pending", "same-id")
            cli.write_request(source, payload("same-id", tmp, "identical"))
            cli.write_request(destination, payload("same-id", tmp, "identical"))

            self.assertEqual(cli.move_request(source, destination.parent), destination)
            self.assertFalse(source.exists())
            self.assertEqual(json.loads(destination.read_text(encoding="utf-8"))["prompt"], "identical")

            source = cli.request_path(root, cli.ACTIVE_STATE, "divergent")
            destination = cli.request_path(root, "pending", "divergent")
            cli.write_request(source, payload("divergent", tmp, "new completed source"))
            cli.write_request(destination, payload("divergent", tmp, "stale destination"))

            with self.assertRaises(FileExistsError):
                cli.move_request(source, destination.parent)
            self.assertTrue(source.exists())
            self.assertTrue(destination.exists())

            source = cli.request_path(root, cli.ACTIVE_STATE, "collision")
            destination = cli.request_path(root, "pending", "collision")
            cli.write_request(source, payload("collision", tmp, "source"))
            cli.write_request(destination, payload("different-id", tmp, "destination"))

            with self.assertRaises(FileExistsError):
                cli.move_request(source, destination.parent)
            self.assertTrue(source.exists())
            self.assertTrue(destination.exists())

            destination.write_text("not json\n", encoding="utf-8")
            with self.assertRaises(FileExistsError):
                cli.move_request(source, destination.parent)
            self.assertTrue(source.exists())

    def test_ack_is_monotonic_and_prevents_redelivery_after_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "queue"
            self._write_request(root, "monotonic-ack", tmp)
            ack_args = argparse.Namespace(queue_dir=str(root), id="monotonic-ack", message="received")

            self.assertEqual(cli.ack(ack_args), 0)
            first = json.loads(cli.ack_path(root, "monotonic-ack").read_text(encoding="utf-8"))
            self.assertEqual(cli.ack(ack_args), 0)
            second = json.loads(cli.ack_path(root, "monotonic-ack").read_text(encoding="utf-8"))
            self.assertGreaterEqual(second["marked_at"], first["marked_at"])

            args = argparse.Namespace(retries=3, retry_delay=0.0, retry_backoff=2.0)
            with mock.patch.object(cli, "run_resume_until_exit_or_ack") as resume:
                self.assertTrue(cli.process_one(root, args))
            resume.assert_not_called()
            self.assertTrue(cli.request_path(root, "done", "monotonic-ack").exists())
            self.assertTrue(cli.ack_path(root, "monotonic-ack").exists())

    def test_late_ack_reconciles_failed_request_to_done(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "queue"
            self._write_request(root, "late-failed-ack", tmp)
            cli.ensure_daemon_dirs(root)
            cli.move_request(
                cli.request_path(root, "pending", "late-failed-ack"),
                root / "failed",
            )
            args = argparse.Namespace(queue_dir=str(root), id="late-failed-ack", message="late delivery completed")

            self.assertEqual(cli.ack(args), 0)
            self.assertFalse(cli.request_path(root, "failed", "late-failed-ack").exists())
            self.assertTrue(cli.request_path(root, "done", "late-failed-ack").exists())
            self.assertTrue(cli.ack_path(root, "late-failed-ack").exists())

    def test_ack_racing_final_failed_transition_converges_to_done(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "queue"
            request_id = "final-transition-ack"
            self._write_request(root, request_id, tmp)
            args = argparse.Namespace(retries=0, retry_delay=0.0, retry_backoff=2.0)
            original_move = cli.move_request

            def move_with_late_ack(source: Path, destination: Path) -> Path:
                if source.parent.name == "running" and destination == root / "failed":
                    cli.write_request(
                        cli.ack_path(root, request_id),
                        {"id": request_id, "marked_at": time.time()},
                    )
                return original_move(source, destination)

            completed = subprocess.CompletedProcess(["codex"], 0)
            with (
                mock.patch.object(cli, "run_resume_until_exit_or_ack", return_value=(completed, False, False)),
                mock.patch.object(cli, "move_request", side_effect=move_with_late_ack),
            ):
                self.assertTrue(cli.process_one(root, args))

            self.assertTrue(cli.ack_path(root, request_id).exists())
            self.assertTrue(cli.request_path(root, "done", request_id).exists())
            self.assertFalse(cli.request_path(root, "failed", request_id).exists())
            self.assertFalse(cli.request_path(root, "running", request_id).exists())

    def test_ack_releases_queue_for_other_session_but_keeps_same_session_lease(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "queue"
            self._write_request(root, "a-running", tmp, target={"kind": "session", "value": "session-a"})
            fake_codex = self._fake_codex(
                Path(tmp),
                ack=True,
                queue=root,
                request_id="a-running",
                sleep_after_ack=3.0,
            )
            args = argparse.Namespace(retries=3, retry_delay=0.0, retry_backoff=2.0, resume_timeout=10.0)

            started = time.monotonic()
            with mock.patch.dict(os.environ, {"CODEX_LONG_TASK_WAKEUP_CODEX_BIN": str(fake_codex)}):
                self.assertTrue(cli.process_one(root, args))
            self.assertLess(time.monotonic() - started, 2.0)
            self.assertTrue(cli.request_path(root, "running", "a-running").exists())
            self.assertTrue(cli.delivery_lock_is_held(root, "a-running"))

            self._write_request(root, "b-same", tmp, target={"kind": "session", "value": "session-a"})
            self._write_request(root, "c-other", tmp, target={"kind": "session", "value": "session-b"})
            self.assertEqual(cli.select_pending(root, time.time()).stem, "c-other")

            cli.recover_running(root)
            self.assertTrue(cli.request_path(root, "running", "a-running").exists())
            for delivery in cli._BACKGROUND_RESUMES.values():
                delivery.deadline = time.monotonic() - 1.0
            cli.reap_background_resumes()
            self.assertFalse(cli._BACKGROUND_RESUMES)
            cli.recover_running(root)
            self.assertTrue(cli.request_path(root, "done", "a-running").exists())

    def test_delivery_rechecks_ack_after_lock_before_spawning_worker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "queue"
            self._write_request(root, "ack-race", tmp, target={"kind": "session", "value": "race-session"})
            request = cli.load_request(cli.request_path(root, "pending", "ack-race"))
            args = argparse.Namespace(resume_timeout=10.0)
            original_acquire = cli.acquire_owner_lock

            def acquire_then_ack(
                queue_root: Path,
                lock_id: str,
                *,
                blocking: bool,
            ) -> tuple[object, Path] | None:
                lock = original_acquire(queue_root, lock_id, blocking=blocking)
                if lock_id == cli.delivery_lock_id("ack-race"):
                    cli.write_request(cli.ack_path(root, "ack-race"), {"id": "ack-race", "marked_at": time.time()})
                return lock

            with (
                mock.patch.object(cli, "acquire_owner_lock", side_effect=acquire_then_ack),
                mock.patch.object(cli.subprocess, "Popen") as spawn,
            ):
                result, acked, live = cli.run_resume_until_exit_or_ack(root, "ack-race", request, args)

            spawn.assert_not_called()
            self.assertTrue(acked)
            self.assertFalse(live)
            self.assertEqual(result.returncode, 0)

    def test_canceled_live_delivery_retains_target_lease_after_memory_loss(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "queue"
            self._write_request(root, "held-canceled", tmp, target={"kind": "session", "value": "held-session"})
            cli.ensure_daemon_dirs(root)
            cli.move_request(cli.request_path(root, "pending", "held-canceled"), root / "canceled")
            delivery_lock = cli.acquire_owner_lock(root, cli.delivery_lock_id("held-canceled"), blocking=False)
            self.assertIsNotNone(delivery_lock)
            try:
                self._write_request(root, "same-target", tmp, target={"kind": "session", "value": "held-session"})
                cli._BACKGROUND_RESUMES.clear()
                self.assertIsNone(cli.select_pending(root, time.time()))
            finally:
                cli.release_owner_lock(delivery_lock, remove=False)
            self.assertEqual(cli.select_pending(root, time.time()).stem, "same-target")

    def test_global_target_lease_blocks_same_session_across_queue_roots(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root_a = Path(tmp) / "queue-a"
            root_b = Path(tmp) / "queue-b"
            target = {"kind": "session", "value": "shared-session"}
            self._write_request(root_a, "owner", tmp, target=target)
            owner = cli.load_request(cli.request_path(root_a, "pending", "owner"))
            self._write_request(root_b, "a-same", tmp, target=target)
            self._write_request(
                root_b,
                "b-other",
                tmp,
                target={"kind": "session", "value": "other-session"},
            )

            with mock.patch.dict(
                os.environ,
                {cli.TARGET_LOCK_DIR_ENV: str(Path(tmp) / "global-target-locks")},
            ):
                lease = cli.acquire_target_lock(owner, blocking=False)
                self.assertIsNotNone(lease)
                try:
                    selected = cli.select_pending(root_b, time.time())
                    self.assertIsNotNone(selected)
                    self.assertEqual(selected.stem, "b-other")
                finally:
                    cli.release_owner_lock(lease, remove=False)

                self.assertEqual(cli.select_pending(root_b, time.time()).stem, "a-same")

    def test_cross_queue_lease_race_requeues_without_consuming_retry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root_a = Path(tmp) / "queue-a"
            root_b = Path(tmp) / "queue-b"
            target = {"kind": "session", "value": "race-session"}
            self._write_request(root_a, "owner", tmp, target=target)
            owner = cli.load_request(cli.request_path(root_a, "pending", "owner"))
            self._write_request(root_b, "contender", tmp, target=target)
            args = argparse.Namespace(retries=3, retry_delay=0.0, retry_backoff=2.0, resume_timeout=10.0)

            with mock.patch.dict(
                os.environ,
                {cli.TARGET_LOCK_DIR_ENV: str(Path(tmp) / "global-target-locks")},
            ):
                lease = cli.acquire_target_lock(owner, blocking=False)
                self.assertIsNotNone(lease)
                try:
                    with (
                        mock.patch.object(cli, "target_has_live_resume", return_value=False),
                        mock.patch.object(cli.subprocess, "Popen") as spawn,
                    ):
                        self.assertTrue(cli.process_one(root_b, args))
                    spawn.assert_not_called()
                finally:
                    cli.release_owner_lock(lease, remove=False)

            pending = cli.request_path(root_b, "pending", "contender")
            self.assertTrue(pending.exists())
            request = json.loads(pending.read_text(encoding="utf-8"))
            self.assertEqual(request["attempts"], 0)
            self.assertIn("target lease held", request["last_deferred_reason"])

    def test_delivery_worker_holds_global_target_lease_across_queues(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root_a = Path(tmp) / "queue-a"
            root_b = Path(tmp) / "queue-b"
            target = {"kind": "session", "value": "worker-session"}
            self._write_request(root_a, "owner", tmp, target=target)
            fake_codex = self._fake_codex(
                Path(tmp),
                ack=True,
                queue=root_a,
                request_id="owner",
                sleep_after_ack=30.0,
            )
            args = argparse.Namespace(retries=3, retry_delay=0.0, retry_backoff=2.0, resume_timeout=10.0)

            with mock.patch.dict(
                os.environ,
                {
                    "CODEX_LONG_TASK_WAKEUP_CODEX_BIN": str(fake_codex),
                    cli.TARGET_LOCK_DIR_ENV: str(Path(tmp) / "global-target-locks"),
                },
            ):
                self.assertTrue(cli.process_one(root_a, args))
                self._write_request(root_b, "same-session", tmp, target=target)
                contender = cli.load_request(cli.request_path(root_b, "pending", "same-session"))
                self.assertTrue(cli.target_lock_is_held(contender))
                self.assertIsNone(cli.select_pending(root_b, time.time()))

                for delivery in cli._BACKGROUND_RESUMES.values():
                    delivery.deadline = time.monotonic() - 1.0
                cli.reap_background_resumes()
                self.assertFalse(cli.target_lock_is_held(contender))
                self.assertEqual(cli.select_pending(root_b, time.time()).stem, "same-session")

    def test_recover_running_tolerates_concurrent_cancellation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "queue"
            self._write_request(root, "cancel-race", tmp)
            cli.ensure_daemon_dirs(root)
            cli.move_request(cli.request_path(root, "pending", "cancel-race"), root / "running")
            running = cli.request_path(root, "running", "cancel-race")
            data = json.loads(running.read_text(encoding="utf-8"))

            def cancel_during_move(source: Path, destination: Path) -> Path:
                canceled = dict(data)
                canceled.update({"canceled_at": time.time(), "canceled_from": "running"})
                cli.write_request(cli.request_path(root, "canceled", "cancel-race"), canceled)
                source.unlink()
                raise FileNotFoundError(source)

            with mock.patch.object(cli, "move_request", side_effect=cancel_during_move):
                cli.recover_running(root)

            self.assertTrue(cli.request_path(root, "canceled", "cancel-race").exists())

    def test_real_daemon_sigkill_does_not_redeliver_live_resume(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "queue"
            launch_log = Path(tmp) / "resume-launches.log"
            self._write_request(root, "restart-lease", tmp, target={"kind": "session", "value": "restart-session"})
            fake_codex = self._fake_codex(
                Path(tmp),
                ack=False,
                queue=root,
                request_id="restart-lease",
                sleep_after_ack=30.0,
                launch_log=launch_log,
            )
            env = os.environ.copy()
            env["PYTHONPATH"] = str(Path(cli.__file__).parents[1])
            env["CODEX_LONG_TASK_WAKEUP_CODEX_BIN"] = str(fake_codex)
            daemon_command = [
                sys.executable,
                "-m",
                "long_task_callback",
                "daemon",
                "--queue-dir",
                str(root),
                "--interval",
                "0.05",
                "--resume-timeout",
                "60",
            ]
            daemon = subprocess.Popen(
                daemon_command,
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            child_pid = None
            try:
                deadline = time.time() + 10.0
                while time.time() < deadline:
                    if cli.request_path(root, "running", "restart-lease").exists() and launch_log.exists():
                        child_pid = int(launch_log.read_text(encoding="utf-8").splitlines()[0])
                        break
                    time.sleep(0.05)
                self.assertIsNotNone(child_pid)

                os.kill(daemon.pid, signal.SIGKILL)
                daemon.wait(timeout=5)
                replacement = subprocess.run(
                    [*daemon_command, "--once"],
                    env=env,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                    timeout=5,
                )
                self.assertEqual(replacement.returncode, 0)
                self.assertEqual(len(launch_log.read_text(encoding="utf-8").splitlines()), 1)
                self.assertTrue(cli.request_path(root, "running", "restart-lease").exists())
                self.assertTrue(cli.delivery_lock_is_held(root, "restart-lease"))

                os.kill(child_pid, signal.SIGTERM)
                deadline = time.time() + 5.0
                while time.time() < deadline and cli.delivery_lock_is_held(root, "restart-lease"):
                    time.sleep(0.05)
                cli.recover_running(root)
                self.assertTrue(cli.request_path(root, "pending", "restart-lease").exists())
            finally:
                if daemon.poll() is None:
                    daemon.kill()
                    daemon.wait(timeout=5)
                if child_pid is not None:
                    try:
                        os.kill(child_pid, signal.SIGTERM)
                    except ProcessLookupError:
                        pass

    def test_delivery_lock_is_not_inherited_by_codex_descendants(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "queue"
            descendant_pid_path = Path(tmp) / "descendant.pid"
            self._write_request(root, "no-fd-leak", tmp, target={"kind": "session", "value": "fd-session"})
            fake_codex = self._fake_codex(
                Path(tmp),
                ack=False,
                queue=root,
                request_id="no-fd-leak",
                descendant_pid_path=descendant_pid_path,
            )
            args = argparse.Namespace(retries=3, retry_delay=0.0, retry_backoff=2.0, resume_timeout=10.0)
            descendant_pid = None
            try:
                with mock.patch.dict(os.environ, {"CODEX_LONG_TASK_WAKEUP_CODEX_BIN": str(fake_codex)}):
                    self.assertTrue(cli.process_one(root, args))
                descendant_pid = int(descendant_pid_path.read_text(encoding="utf-8"))
                os.kill(descendant_pid, 0)
                self.assertFalse(cli.delivery_lock_is_held(root, "no-fd-leak"))
                self.assertTrue(cli.request_path(root, "pending", "no-fd-leak").exists())
            finally:
                if descendant_pid is not None:
                    try:
                        os.kill(descendant_pid, signal.SIGTERM)
                    except ProcessLookupError:
                        pass

    def test_setup_installs_skill_then_systemd(self) -> None:
        args = argparse.Namespace(
            skill_path="/tmp/skills",
            name="codex-long-task-wakeup",
            queue_dir="/tmp/queue",
            interval=2.0,
            retries=3,
            retry_delay=30.0,
            retry_backoff=2.0,
            restart_sec=5.0,
            exec_start=None,
            codex_bin=None,
            path="/bin",
            force=True,
            enable=True,
            now=True,
        )

        with (
            mock.patch.object(cli, "install_skill", return_value=0) as install_skill,
            mock.patch.object(cli, "install_systemd", return_value=0) as install_systemd,
        ):
            self.assertEqual(cli.setup(args), 0)

        skill_args = install_skill.call_args.args[0]
        self.assertEqual(skill_args.path, "/tmp/skills")
        self.assertTrue(skill_args.force)

        systemd_args = install_systemd.call_args.args[0]
        self.assertEqual(systemd_args.queue_dir, "/tmp/queue")
        self.assertEqual(systemd_args.path, "/bin")
        self.assertTrue(systemd_args.enable)
        self.assertTrue(systemd_args.now)

    def test_install_systemd_persists_only_proxy_values_in_private_environment_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / ".env"
            source.write_text(
                "HTTP_PROXY=http://proxy.example:8080\nSECRET_TOKEN=must-not-be-copied\nno_proxy=localhost,127.0.0.1\n",
                encoding="utf-8",
            )
            args = argparse.Namespace(
                name="codex-long-task-wakeup",
                queue_dir="/tmp/queue",
                interval=2.0,
                retries=3,
                retry_delay=30.0,
                retry_backoff=2.0,
                resume_timeout=3600.0,
                restart_sec=5.0,
                exec_start=None,
                codex_bin=None,
                path="/bin",
                proxy_env_file=str(source),
                inherit_proxy=False,
                force=True,
                enable=False,
                now=False,
                print=False,
            )
            home = Path(tmp) / "codex-home"
            with (
                mock.patch.dict(os.environ, {"CODEX_HOME": str(home)}, clear=False),
                mock.patch.object(cli, "systemd_user_dir", return_value=Path(tmp) / "systemd"),
                mock.patch.object(cli.shutil, "which", return_value=None),
            ):
                self.assertEqual(cli.install_systemd(args), 0)

            proxy_file = home / "long-task-wakeup" / "service-proxy.env"
            self.assertEqual(cli.parse_proxy_environment_file(proxy_file), {
                "HTTP_PROXY": "http://proxy.example:8080",
                "no_proxy": "localhost,127.0.0.1",
            })
            self.assertEqual(proxy_file.stat().st_mode & 0o777, 0o600)
            service = (Path(tmp) / "systemd" / "codex-long-task-wakeup.service").read_text(encoding="utf-8")
            self.assertIn(f"EnvironmentFile=-{proxy_file}", service)
            self.assertIn("ExecReload=/bin/kill -HUP $MAINPID", service)
            self.assertNotIn("proxy.example", service)
            self.assertNotIn("SECRET_TOKEN", service)

    def test_install_systemd_defers_old_daemon_restart_while_delivery_is_running(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "queue"
            self._write_request(root, "inflight", tmp)
            cli.move_request(root / "pending" / "inflight.json", root / "running")
            args = argparse.Namespace(
                name="codex-long-task-wakeup",
                queue_dir=str(root),
                interval=2.0,
                retries=3,
                retry_delay=30.0,
                retry_backoff=2.0,
                resume_timeout=3600.0,
                restart_sec=5.0,
                exec_start=None,
                codex_bin=None,
                path="/bin",
                proxy_env_file=None,
                inherit_proxy=False,
                force=True,
                enable=True,
                now=True,
                print=False,
            )
            with (
                mock.patch.dict(os.environ, {"CODEX_HOME": str(Path(tmp) / "codex-home")}, clear=False),
                mock.patch.object(cli, "systemd_user_dir", return_value=Path(tmp) / "systemd"),
                mock.patch.object(cli.shutil, "which", side_effect=lambda command: "/usr/bin/systemctl" if command == "systemctl" else None),
                mock.patch.object(cli, "run_systemctl", return_value=0) as run_systemctl,
            ):
                self.assertEqual(cli.install_systemd(args), 0)

            self.assertEqual(run_systemctl.call_args_list, [mock.call(["daemon-reload"]), mock.call(["enable", "codex-long-task-wakeup.service"])])

    def test_install_systemd_reloads_supported_daemon_while_delivery_is_running(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "queue"
            self._write_request(root, "inflight", tmp)
            cli.move_request(root / "pending" / "inflight.json", root / "running")
            home = Path(tmp) / "codex-home"
            runtime = home / "long-task-wakeup" / "daemon-runtime.json"
            runtime.parent.mkdir(parents=True)
            runtime.write_text(
                json.dumps({"pid": os.getpid(), "reload_protocol": cli.RELOAD_PROTOCOL_VERSION}) + "\n",
                encoding="utf-8",
            )
            args = argparse.Namespace(
                name="codex-long-task-wakeup",
                queue_dir=str(root),
                interval=2.0,
                retries=3,
                retry_delay=30.0,
                retry_backoff=2.0,
                resume_timeout=3600.0,
                restart_sec=5.0,
                exec_start=None,
                codex_bin=None,
                path="/bin",
                proxy_env_file=None,
                inherit_proxy=False,
                force=True,
                enable=True,
                now=True,
                print=False,
            )
            with (
                mock.patch.dict(os.environ, {"CODEX_HOME": str(home)}, clear=False),
                mock.patch.object(cli, "systemd_user_dir", return_value=Path(tmp) / "systemd"),
                mock.patch.object(cli.shutil, "which", side_effect=lambda command: "/usr/bin/systemctl" if command == "systemctl" else None),
                mock.patch.object(cli, "run_systemctl", return_value=0) as run_systemctl,
            ):
                self.assertEqual(cli.install_systemd(args), 0)

            self.assertEqual(
                run_systemctl.call_args_list,
                [
                    mock.call(["daemon-reload"]),
                    mock.call(["enable", "codex-long-task-wakeup.service"]),
                    mock.call(["reload", "codex-long-task-wakeup.service"]),
                ],
            )

    def test_supervisor_config_does_not_embed_proxy_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "codex-home"
            proxy = home / "long-task-wakeup" / "service-proxy.env"
            proxy.parent.mkdir(parents=True)
            proxy.write_text("HTTPS_PROXY=https://user:secret@proxy.example:8443\n", encoding="utf-8")
            args = argparse.Namespace(
                name="codex-long-task-wakeup",
                queue_dir="/tmp/queue",
                interval=2.0,
                retries=3,
                retry_delay=30.0,
                retry_backoff=2.0,
                resume_timeout=3600.0,
                exec_start="/usr/local/bin/codex-long-task-wakeup",
                codex_bin="/usr/local/bin/codex",
                path="/bin",
            )
            with mock.patch.dict(os.environ, {"CODEX_HOME": str(home)}, clear=False):
                text = cli.supervisor_config_text(args)

            self.assertIn(f"{cli.PROXY_ENV_FILE_ENV}=", text)
            self.assertNotIn("secret@proxy.example", text)

    def test_proxy_parser_rejects_values_unsafe_for_service_environment(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / ".env"
            source.write_text("HTTPS_PROXY=https://user:bad password@proxy.example\n", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "unsafe for a systemd environment file"):
                cli.parse_proxy_environment_file(source)

    def test_clear_proxy_removes_persisted_proxy_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "codex-home"
            proxy = home / "long-task-wakeup" / "service-proxy.env"
            proxy.parent.mkdir(parents=True)
            proxy.write_text("HTTP_PROXY=http://proxy.example:8080\n", encoding="utf-8")
            args = argparse.Namespace(proxy_env_file=None, inherit_proxy=False, clear_proxy=True)

            with mock.patch.dict(os.environ, {"CODEX_HOME": str(home)}, clear=False):
                self.assertIsNone(cli.configure_proxy_environment(args))

            self.assertFalse(proxy.exists())

    def test_daemon_hup_waits_for_live_delivery_workers_before_reexec(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            args = argparse.Namespace(
                queue_dir=str(Path(tmp) / "queue"),
                interval=2.0,
                retries=3,
                retry_delay=30.0,
                retry_backoff=2.0,
                resume_timeout=3600.0,
                once=True,
                max_items=None,
            )
            process = mock.Mock()
            process.poll.return_value = None
            delivery = cli.BackgroundResume(process=process, target_key="session:test", deadline=time.monotonic() + 60, request_id="inflight")

            def signal_immediately(signum: int, handler: object) -> object:
                if signum == signal.SIGHUP and callable(handler):
                    handler(signal.SIGHUP, None)
                return signal.SIG_DFL

            with (
                mock.patch.dict(os.environ, {"CODEX_HOME": str(Path(tmp) / "codex-home")}, clear=False),
                mock.patch.dict(cli._BACKGROUND_RESUMES, {123: delivery}, clear=True),
                mock.patch.object(cli.signal, "signal", side_effect=signal_immediately),
                mock.patch.object(cli.os, "execv") as execv,
            ):
                self.assertEqual(cli.daemon(args), 0)

            execv.assert_not_called()

    def test_daemon_hup_reexecs_in_place(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            args = argparse.Namespace(
                queue_dir=str(Path(tmp) / "queue"),
                interval=2.0,
                retries=3,
                retry_delay=30.0,
                retry_backoff=2.0,
                resume_timeout=3600.0,
                once=False,
                max_items=None,
            )
            expected = cli.daemon_reexec_command(args)

            def signal_immediately(signum: int, handler: object) -> object:
                if signum == signal.SIGHUP and callable(handler):
                    handler(signal.SIGHUP, None)
                return signal.SIG_DFL

            with (
                mock.patch.dict(os.environ, {"CODEX_HOME": str(Path(tmp) / "codex-home")}, clear=False),
                mock.patch.object(cli.signal, "signal", side_effect=signal_immediately),
                mock.patch.object(cli.os, "execv", side_effect=RuntimeError("reexec")) as execv,
            ):
                with self.assertRaisesRegex(RuntimeError, "reexec"):
                    cli.daemon(args)

            execv.assert_called_once_with(sys.executable, expected)

    def test_install_systemd_starts_supervisor_in_container(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            args = argparse.Namespace(
                name="codex-long-task-wakeup",
                queue_dir="/tmp/queue",
                interval=2.0,
                retries=3,
                retry_delay=30.0,
                retry_backoff=2.0,
                restart_sec=5.0,
                exec_start=None,
                codex_bin=None,
                path="/bin",
                force=True,
                enable=True,
                now=True,
                print=False,
            )

            with (
                mock.patch.object(cli, "systemd_user_dir", return_value=Path(tmp)),
                mock.patch.object(cli.shutil, "which", side_effect=lambda command: "/usr/bin/supervisorctl" if command == "supervisorctl" else None),
                mock.patch.object(cli, "running_in_container", return_value=True),
                mock.patch.object(cli, "install_supervisor", return_value=0) as install_supervisor,
            ):
                self.assertEqual(cli.install_systemd(args), 0)

            self.assertTrue((Path(tmp) / "codex-long-task-wakeup.service").exists())
            install_supervisor.assert_called_once()

    def test_install_systemd_starts_standalone_without_supervisor(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            args = argparse.Namespace(
                name="codex-long-task-wakeup",
                queue_dir="/tmp/queue",
                interval=2.0,
                retries=3,
                retry_delay=30.0,
                retry_backoff=2.0,
                restart_sec=5.0,
                exec_start=None,
                codex_bin=None,
                path="/bin",
                force=True,
                enable=True,
                now=True,
                print=False,
            )

            with (
                mock.patch.object(cli, "systemd_user_dir", return_value=Path(tmp)),
                mock.patch.object(cli.shutil, "which", return_value=None),
                mock.patch.object(cli, "running_in_container", return_value=True),
                mock.patch.object(cli, "start_standalone_daemon", return_value=0) as start_standalone,
            ):
                self.assertEqual(cli.install_systemd(args), 0)

            self.assertTrue((Path(tmp) / "codex-long-task-wakeup.service").exists())
            start_standalone.assert_called_once()

    def test_install_systemd_falls_back_when_user_bus_is_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            args = argparse.Namespace(
                name="codex-long-task-wakeup",
                queue_dir="/tmp/queue",
                interval=2.0,
                retries=3,
                retry_delay=30.0,
                retry_backoff=2.0,
                restart_sec=5.0,
                exec_start=None,
                codex_bin=None,
                path="/bin",
                force=True,
                enable=True,
                now=True,
                print=False,
            )

            with (
                mock.patch.object(cli, "systemd_user_dir", return_value=Path(tmp)),
                mock.patch.object(cli.shutil, "which", side_effect=lambda command: "/usr/bin/systemctl" if command == "systemctl" else None),
                mock.patch.object(cli, "run_systemctl", return_value=1),
                mock.patch.object(cli, "start_standalone_daemon", return_value=0) as start_standalone,
            ):
                self.assertEqual(cli.install_systemd(args), 0)

            start_standalone.assert_called_once()

    def test_supervisor_config_uses_autorestart(self) -> None:
        args = argparse.Namespace(
            name="codex-long-task-wakeup",
            queue_dir="/tmp/queue",
            interval=2.0,
            retries=3,
            retry_delay=30.0,
            retry_backoff=2.0,
            exec_start="/usr/local/bin/codex-long-task-wakeup",
            codex_bin="/usr/local/bin/codex",
            path="/bin",
        )

        text = cli.supervisor_config_text(args)

        self.assertIn("[program:codex-long-task-wakeup]", text)
        self.assertIn("autostart=true", text)
        self.assertIn("autorestart=true", text)
        self.assertIn("--queue-dir /tmp/queue", text)

    def test_cancel_moves_pending_to_canceled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "queue"
            self._write_request(root, "cancel-me", tmp)

            self.assertTrue(cli.cancel_one(root, "cancel-me", "not needed"))

            self.assertFalse((root / "pending" / "cancel-me.json").exists())
            canceled = root / "canceled" / "cancel-me.json"
            self.assertTrue(canceled.exists())
            request = json.loads(canceled.read_text(encoding="utf-8"))
            self.assertEqual(request["id"], "cancel-me")
            self.assertEqual(request["canceled_from"], "pending")
            self.assertEqual(request["cancel_message"], "not needed")

    def test_cancel_all_moves_pending_and_running(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "queue"
            self._write_request(root, "pending-one", tmp)
            (root / "running").mkdir(parents=True)
            (root / "running" / "running-one.json").write_text(
                json.dumps(
                    {
                        "version": 1,
                        "id": "running-one",
                        "created_at": 1.0,
                        "cwd": tmp,
                        "target": {"kind": "last"},
                        "prompt": "wake up",
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            self.assertEqual(cli.cancel_all(root), 2)

            self.assertFalse((root / "pending" / "pending-one.json").exists())
            self.assertFalse((root / "running" / "running-one.json").exists())
            self.assertTrue((root / "canceled" / "pending-one.json").exists())
            self.assertTrue((root / "canceled" / "running-one.json").exists())

    def test_status_lists_pending_task_and_bound_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "queue"
            (root / "pending").mkdir(parents=True)
            request = {
                "version": 1,
                "id": "pending-id",
                "created_at": 1.0,
                "cwd": tmp,
                "target": {"kind": "session", "value": "thread-1"},
                "prompt": "[long-task-callback]\nTask: train model\n",
            }
            (root / "pending" / "pending-id.json").write_text(json.dumps(request), encoding="utf-8")
            args = argparse.Namespace(queue_dir=str(root), state=None, limit=5, quiet_empty=False, shell_hook=True)

            with mock.patch("builtins.print") as output:
                self.assertEqual(cli.status(args), 0)

            rendered = "\n".join(str(call.args[0]) for call in output.call_args_list)
            self.assertIn("train model", rendered)
            self.assertIn("session thread-1", rendered)
            self.assertIn("pending-id", rendered)

    def test_status_is_quiet_when_queue_is_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            args = argparse.Namespace(queue_dir=tmp, state=None, limit=5, quiet_empty=True, shell_hook=True)
            with mock.patch("builtins.print") as output:
                self.assertEqual(cli.status(args), 0)
            output.assert_not_called()

    def test_install_shell_hook_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            rc_file = Path(tmp) / ".bashrc"
            rc_file.write_text("export EXISTING=1\n", encoding="utf-8")
            args = argparse.Namespace(rc_file=str(rc_file), command="/opt/bin/codex-long-task-wakeup")

            self.assertEqual(cli.install_shell_hook(args), 0)
            self.assertEqual(cli.install_shell_hook(args), 0)

            rendered = rc_file.read_text(encoding="utf-8")
            self.assertIn("export EXISTING=1", rendered)
            self.assertEqual(rendered.count(cli.SHELL_HOOK_BEGIN), 1)
            self.assertEqual(rendered.count(cli.SHELL_HOOK_END), 1)
            self.assertIn("timeout 2s /opt/bin/codex-long-task-wakeup status", rendered)

    def _write_request(
        self,
        root: Path,
        request_id: str,
        cwd: str,
        *,
        target: dict[str, str] | None = None,
    ) -> None:
        (root / "pending").mkdir(parents=True, exist_ok=True)
        request = {
            "version": 1,
            "id": request_id,
            "created_at": 1.0,
            "cwd": cwd,
            "target": target or {"kind": "last"},
            "prompt": "wake up",
        }
        (root / "pending" / f"{request_id}.json").write_text(
            json.dumps(request) + "\n",
            encoding="utf-8",
        )

    def _run_args(self, cwd: str, root: Path, command: list[str]) -> argparse.Namespace:
        return argparse.Namespace(
            cwd=cwd,
            task="test long task",
            command=None,
            exit_code=None,
            message=None,
            session="test-thread",
            last=False,
            via_daemon=True,
            queue_dir=str(root),
            approvals_reviewer="auto_review",
            approval_policy="on-request",
            sandbox_mode="workspace-write",
            dry_run=False,
            strict=False,
            wrapped_command=command,
        )

    def _fake_codex(
        self,
        directory: Path,
        *,
        ack: bool,
        queue: Path,
        request_id: str,
        sleep_after_ack: float = 0.0,
        launch_log: Path | None = None,
        descendant_pid_path: Path | None = None,
    ) -> Path:
        script = directory / ("fake-codex-ack" if ack else "fake-codex")
        src_path = Path(cli.__file__).parents[1]
        ack_code = (
            f"os.environ['PYTHONPATH'] = {str(src_path)!r} + os.pathsep + os.environ.get('PYTHONPATH', ''); "
            "subprocess.run([sys.executable, '-m', 'long_task_callback', 'ack', "
            f"'--queue-dir', {str(queue)!r}, '--id', {request_id!r}], check=True)"
            if ack
            else "pass"
        )
        script.write_text(
            textwrap.dedent(
                f"""\
                #!/usr/bin/env python3
                import os
                import pathlib
                import subprocess
                import sys
                import time

                launch_log = {str(launch_log) if launch_log is not None else None!r}
                if launch_log is not None:
                    with open(launch_log, "a", encoding="utf-8") as handle:
                        handle.write(str(os.getpid()) + "\\n")
                        handle.flush()
                sys.stdin.read()
                {ack_code}
                descendant_pid_path = {str(descendant_pid_path) if descendant_pid_path is not None else None!r}
                if descendant_pid_path is not None:
                    descendant = subprocess.Popen(
                        [sys.executable, "-c", "import time; time.sleep(30)"],
                        start_new_session=True,
                    )
                    pathlib.Path(descendant_pid_path).write_text(str(descendant.pid), encoding="utf-8")
                time.sleep({sleep_after_ack!r})
                raise SystemExit(0)
                """
            ),
            encoding="utf-8",
        )
        script.chmod(script.stat().st_mode | stat.S_IXUSR)
        return script


if __name__ == "__main__":
    unittest.main()
