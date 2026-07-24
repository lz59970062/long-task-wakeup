from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import os
import shlex
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import textwrap
import threading
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
            target = {"kind": "session", "value": "late-ack-thread"}
            self._write_request(root, "late-failed-ack", tmp, target=target)
            cli.ensure_daemon_dirs(root)
            cli.move_request(
                cli.request_path(root, "pending", "late-failed-ack"),
                root / "failed",
            )
            failed = cli.load_request(cli.request_path(root, "failed", "late-failed-ack"))
            failed["retain_target_lease"] = True
            cli.write_request(cli.request_path(root, "failed", "late-failed-ack"), failed)
            args = argparse.Namespace(queue_dir=str(root), id="late-failed-ack", message="late delivery completed")

            with mock.patch.dict(os.environ, {cli.TARGET_LOCK_DIR_ENV: str(Path(tmp) / "target-locks")}, clear=False):
                cli.retain_target_lease(root, failed)
                self.assertEqual(cli.ack(args), 0)
                self.assertFalse(cli.request_path(root, "failed", "late-failed-ack").exists())
                self.assertTrue(cli.request_path(root, "done", "late-failed-ack").exists())
                self.assertTrue(cli.ack_path(root, "late-failed-ack").exists())
                self.assertFalse(cli.retained_target_lease_is_held(failed))

    def test_late_ack_releases_manual_recovery_lease_across_queue_roots(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root_a = Path(tmp) / "queue-a"
            root_b = Path(tmp) / "queue-b"
            target = {"kind": "session", "value": "late-ack-thread"}
            self._write_request(root_a, "late-failed-ack", tmp, target=target)
            cli.ensure_daemon_dirs(root_a)
            cli.move_request(cli.request_path(root_a, "pending", "late-failed-ack"), root_a / "failed")
            failed = cli.load_request(cli.request_path(root_a, "failed", "late-failed-ack"))
            failed["retain_target_lease"] = True
            cli.write_request(cli.request_path(root_a, "failed", "late-failed-ack"), failed)
            self._write_request(root_b, "same-session", tmp, target=target)
            args = argparse.Namespace(queue_dir=str(root_a), id="late-failed-ack", message=None)

            with mock.patch.dict(os.environ, {cli.TARGET_LOCK_DIR_ENV: str(Path(tmp) / "target-locks")}, clear=False):
                cli.retain_target_lease(root_a, failed)
                self.assertIsNone(cli.select_pending(root_b, time.time()))
                self.assertEqual(cli.ack(args), 0)
                self.assertEqual(cli.select_pending(root_b, time.time()).stem, "same-session")

    def test_restart_reconciles_retained_marker_after_acknowledged_done_transition(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root_a = Path(tmp) / "queue-a"
            root_b = Path(tmp) / "queue-b"
            target = {"kind": "session", "value": "restart-thread"}
            self._write_request(root_a, "reconciled", tmp, target=target)
            cli.ensure_daemon_dirs(root_a)
            cli.move_request(cli.request_path(root_a, "pending", "reconciled"), root_a / "done")
            completed = cli.load_request(cli.request_path(root_a, "done", "reconciled"))
            self._write_request(root_b, "same-session", tmp, target=target)

            with mock.patch.dict(os.environ, {cli.TARGET_LOCK_DIR_ENV: str(Path(tmp) / "target-locks")}, clear=False):
                cli.retain_target_lease(root_a, completed)
                cli.write_request(cli.ack_path(root_a, "reconciled"), {"id": "reconciled", "marked_at": time.time()})
                self.assertEqual(cli.select_pending(root_b, time.time()).stem, "same-session")
                self.assertFalse(cli.retained_target_lease_is_held(completed))

    def test_marker_reconciliation_blocks_while_transaction_lock_is_held(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "queue"
            target = {"kind": "session", "value": "reconcile-thread"}
            self._write_request(root, "reconciled", tmp, target=target)
            request = cli.load_request(cli.request_path(root, "pending", "reconciled"))

            with mock.patch.dict(os.environ, {cli.TARGET_LOCK_DIR_ENV: str(Path(tmp) / "target-locks")}, clear=False):
                cli.retain_target_lease(root, request)
                cli.write_request(cli.ack_path(root, "reconciled"), {"id": "reconciled", "marked_at": time.time()})
                lock = cli.acquire_retained_target_lease_lock(request, blocking=False)
                self.assertIsNotNone(lock)
                try:
                    self.assertTrue(cli.retained_target_lease_is_held(request))
                finally:
                    cli.release_owner_lock(lock, remove=False)
                self.assertFalse(cli.retained_target_lease_is_held(request))

    def test_ack_releases_timeout_marker_while_parent_still_marks_running(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "queue"
            target = {"kind": "session", "value": "timeout-thread"}
            self._write_request(root, "timeout-ack", tmp, target=target)
            cli.ensure_daemon_dirs(root)
            cli.move_request(cli.request_path(root, "pending", "timeout-ack"), root / "running")
            request = cli.load_request(cli.request_path(root, "running", "timeout-ack"))
            args = argparse.Namespace(queue_dir=str(root), id="timeout-ack", message=None)

            with mock.patch.dict(os.environ, {cli.TARGET_LOCK_DIR_ENV: str(Path(tmp) / "target-locks")}, clear=False):
                cli.retain_target_lease(root, request)
                self.assertEqual(cli.ack(args), 0)
                self.assertTrue(cli.request_path(root, "running", "timeout-ack").exists())
                self.assertFalse(cli.retained_target_lease_is_held(request))

    def test_ack_racing_final_failed_transition_converges_to_done(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "queue"
            request_id = "final-transition-ack"
            self._write_request(root, request_id, tmp, target={"kind": "session", "value": "race-thread"})
            args = argparse.Namespace(retries=0, retry_delay=0.0, retry_backoff=2.0)
            original_move = cli.move_request

            def move_with_late_ack(source: Path, destination: Path) -> Path:
                if source.parent.name == "running" and destination == root / "failed":
                    cli.retain_target_lease(root, cli.load_request(source))
                    cli.write_request(
                        cli.ack_path(root, request_id),
                        {"id": request_id, "marked_at": time.time()},
                    )
                return original_move(source, destination)

            completed = subprocess.CompletedProcess(["codex"], 125)
            with (
                mock.patch.dict(os.environ, {cli.TARGET_LOCK_DIR_ENV: str(Path(tmp) / "target-locks")}, clear=False),
                mock.patch.object(cli, "run_resume_until_exit_or_ack", return_value=(completed, False, False)),
                mock.patch.object(cli, "move_request", side_effect=move_with_late_ack),
            ):
                self.assertTrue(cli.process_one(root, args))
                request = cli.load_request(cli.request_path(root, "done", request_id))
                self.assertFalse(cli.retained_target_lease_is_held(request))

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

    def test_stale_selection_rechecks_retained_target_lease_before_worker_launch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root_a = Path(tmp) / "queue-a"
            root_b = Path(tmp) / "queue-b"
            target = {"kind": "session", "value": "stale-selection-session"}
            failed = {
                "version": 1,
                "id": "desktop-unknown",
                "created_at": 1.0,
                "cwd": tmp,
                "target": target,
                "prompt": "wake up",
                "retain_target_lease": True,
            }
            cli.ensure_daemon_dirs(root_a)
            cli.write_request(cli.request_path(root_a, "failed", "desktop-unknown"), failed)
            self._write_request(root_b, "contender", tmp, target=target)
            args = argparse.Namespace(retries=3, retry_delay=0.0, retry_backoff=2.0, resume_timeout=10.0)

            with mock.patch.dict(
                os.environ,
                {cli.TARGET_LOCK_DIR_ENV: str(Path(tmp) / "global-target-locks")},
            ):
                cli.retain_target_lease(root_a, failed)
                with (
                    mock.patch.object(cli, "target_has_live_resume", return_value=False),
                    mock.patch.object(cli.subprocess, "Popen") as spawn,
                ):
                    self.assertTrue(cli.process_one(root_b, args))
                spawn.assert_not_called()

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

    def test_completed_goal_is_terminal_and_suppresses_reminders(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "queue"
            start_args = argparse.Namespace(
                queue_dir=str(root), id="terminal-goal", session="thread-1", last=False,
                cwd=tmp, task="finish report", idle_seconds=1.0,
            )
            self.assertEqual(cli.goal_start(start_args), 0)
            ack_args = argparse.Namespace(
                queue_dir=str(root), id="terminal-goal", state="completed", message="done",
                condition=None, email_to=None, email_after=cli.DEFAULT_BLOCKED_EMAIL_SECONDS,
            )
            self.assertEqual(cli.goal_ack(ack_args), 0)

            cli.update_goal_from_callback(root, {"goal_id": "terminal-goal", "created_at": time.time()})
            self.assertEqual(
                cli.enqueue_existing_request(
                    root,
                    {
                        "version": 1,
                        "id": "terminal-callback",
                        "created_at": time.time(),
                        "cwd": tmp,
                        "target": {"kind": "session", "value": "thread-1"},
                        "target_source": "--session",
                        "goal_id": "terminal-goal",
                        "prompt": "Task: stale callback",
                    },
                    "Task: stale callback",
                ),
                1,
            )

            goal = cli.load_goal(root, "terminal-goal")
            self.assertEqual(goal["state"], "completed")
            self.assertFalse((root / "pending" / "terminal-callback.json").exists())
            self.assertFalse(cli.process_goal_reminders(root))
            self.assertEqual(
                cli.goal_resume(argparse.Namespace(queue_dir=str(root), id="terminal-goal")),
                1,
            )

    def test_goal_reminder_is_deduplicated_and_external_callback_resets_idle_timer(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "queue"
            start_args = argparse.Namespace(
                queue_dir=str(root), id="reminder-goal", session="thread-1", last=False,
                cwd=tmp, task="train model", idle_seconds=1.0,
            )
            self.assertEqual(cli.goal_start(start_args), 0)
            goal = cli.load_goal(root, "reminder-goal")
            goal["last_external_callback_at"] = time.time() - 2.0
            cli.write_goal(root, goal)

            self.assertTrue(cli.process_goal_reminders(root))
            self.assertFalse(cli.process_goal_reminders(root))
            pending = list((root / "pending").glob("*.json"))
            self.assertEqual(len(pending), 1)
            reminder = cli.load_request(pending[0])
            self.assertTrue(reminder["goal_reminder"])
            self.assertIn("goal ack --queue-dir", reminder["prompt"])
            self.assertIn("--id reminder-goal", reminder["prompt"])
            self.assertIn("--condition", reminder["prompt"])

            cli.move_request(pending[0], root / "done")
            goal = cli.load_goal(root, "reminder-goal")
            goal["last_reminder_at"] = time.time() - 2.0
            cli.write_goal(root, goal)
            self.assertTrue(cli.process_goal_reminders(root))
            self.assertEqual(len(list((root / "pending").glob("*.json"))), 1)

            cli.update_goal_from_callback(root, {"goal_id": "reminder-goal", "created_at": time.time()})
            goal = cli.load_goal(root, "reminder-goal")
            self.assertNotIn("last_reminder_at", goal)
            self.assertGreaterEqual(float(goal["last_external_callback_at"]), time.time() - 1.0)

    def test_goal_reminder_reconciles_callback_persisted_before_goal_update(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "queue"
            start_args = argparse.Namespace(
                queue_dir=str(root), id="recovery-goal", session="thread-1", last=False,
                cwd=tmp, task="run recovery", idle_seconds=1.0,
            )
            self.assertEqual(cli.goal_start(start_args), 0)
            goal = cli.load_goal(root, "recovery-goal")
            goal["last_external_callback_at"] = time.time() - 2.0
            cli.write_goal(root, goal)
            callback_time = time.time()
            cli.write_request(
                root / "pending" / "crash-window.json",
                {
                    "version": 1,
                    "id": "crash-window",
                    "created_at": callback_time,
                    "cwd": tmp,
                    "target": {"kind": "session", "value": "thread-1"},
                    "goal_id": "recovery-goal",
                    "prompt": "Task: callback persisted before goal timestamp",
                },
            )

            self.assertFalse(cli.process_goal_reminders(root))
            goal = cli.load_goal(root, "recovery-goal")
            self.assertEqual(float(goal["last_external_callback_at"]), callback_time)
            self.assertNotIn("last_reminder_at", goal)

    def test_blocked_goal_email_is_configured_and_sent_once_after_delay(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(
            os.environ, {"CODEX_LONG_TASK_WAKEUP_BLOCKED_EMAIL_TO": "owner@example.test"}, clear=False
        ):
            root = Path(tmp) / "queue"
            start_args = argparse.Namespace(
                queue_dir=str(root), id="blocked-goal", session="thread-1", last=False,
                cwd=tmp, task="wait for access", idle_seconds=1.0,
            )
            self.assertEqual(cli.goal_start(start_args), 0)
            bad_ack_args = argparse.Namespace(
                queue_dir=str(root), id="blocked-goal", state="blocked_conditions", message=None,
                condition="need credentials", email_to="other@example.test", email_after=1.0,
            )
            self.assertEqual(cli.goal_ack(bad_ack_args), 2)
            ack_args = argparse.Namespace(
                queue_dir=str(root), id="blocked-goal", state="blocked_conditions", message=None,
                condition="need credentials", email_to="owner@example.test", email_after=1.0,
            )
            self.assertEqual(cli.goal_ack(ack_args), 0)
            goal = cli.load_goal(root, "blocked-goal")
            goal["state_changed_at"] = time.time() - 2.0
            cli.write_goal(root, goal)

            accepted = subprocess.CompletedProcess(["sendmail"], 0)
            with mock.patch.object(cli.subprocess, "run", return_value=accepted) as sendmail:
                self.assertTrue(cli.process_blocked_goal_email(root))
                self.assertFalse(cli.process_blocked_goal_email(root))

            self.assertEqual(sendmail.call_count, 1)
            self.assertEqual(cli.load_goal(root, "blocked-goal")["blocked_email_result"], "accepted")

    def test_blocked_goal_email_requires_one_recipient_and_stops_after_three_failures(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(
            os.environ, {"CODEX_LONG_TASK_WAKEUP_BLOCKED_EMAIL_TO": "owner@example.test"}, clear=False
        ):
            root = Path(tmp) / "queue"
            start_args = argparse.Namespace(
                queue_dir=str(root), id="retry-goal", session="thread-1", last=False,
                cwd=tmp, task="wait for service", idle_seconds=1.0,
            )
            self.assertEqual(cli.goal_start(start_args), 0)
            multi_recipient_args = argparse.Namespace(
                queue_dir=str(root), id="retry-goal", state="blocked_conditions", message=None,
                condition="need service", email_to="owner@example.test,other@example.test", email_after=1.0,
            )
            self.assertEqual(cli.goal_ack(multi_recipient_args), 2)
            ack_args = argparse.Namespace(
                queue_dir=str(root), id="retry-goal", state="blocked_conditions", message=None,
                condition="need service", email_to="owner@example.test", email_after=1.0,
            )
            self.assertEqual(cli.goal_ack(ack_args), 0)
            goal = cli.load_goal(root, "retry-goal")
            goal["state_changed_at"] = time.time() - 2.0
            cli.write_goal(root, goal)

            failed = subprocess.CompletedProcess(["sendmail"], 1)
            with mock.patch.object(cli.subprocess, "run", return_value=failed) as sendmail:
                for attempt in range(3):
                    self.assertTrue(cli.process_blocked_goal_email(root))
                    if attempt < 2:
                        goal = cli.load_goal(root, "retry-goal")
                        goal["blocked_email_next_attempt_at"] = time.time() - 1.0
                        cli.write_goal(root, goal)
                self.assertFalse(cli.process_blocked_goal_email(root))

            goal = cli.load_goal(root, "retry-goal")
            self.assertEqual(sendmail.call_count, 3)
            self.assertIn("blocked_email_exhausted_at", goal)

    def test_desktop_app_server_delivery_preserves_turn_completion_and_unknown_submit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            def start_server(*, reply_to_turn_start: bool) -> tuple[Path, threading.Thread, list[dict[str, object]], list[BaseException]]:
                socket_path = Path(tmp) / ("confirmed.sock" if reply_to_turn_start else "unknown.sock")
                listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                listener.bind(str(socket_path))
                listener.listen(1)
                messages: list[dict[str, object]] = []
                errors: list[BaseException] = []

                def read_exact(connection: socket.socket, size: int) -> bytes:
                    chunks = bytearray()
                    while len(chunks) < size:
                        chunk = connection.recv(size - len(chunks))
                        if not chunk:
                            raise AssertionError("fake App Server closed unexpectedly")
                        chunks.extend(chunk)
                    return bytes(chunks)

                def read_frame(connection: socket.socket) -> dict[str, object]:
                    first, second = read_exact(connection, 2)
                    self.assertEqual(first & 0x0F, 0x1)
                    length = second & 0x7F
                    if length == 126:
                        length = int.from_bytes(read_exact(connection, 2), "big")
                    self.assertTrue(second & 0x80)
                    mask = read_exact(connection, 4)
                    payload = read_exact(connection, length)
                    decoded = bytes(value ^ mask[index % 4] for index, value in enumerate(payload))
                    return json.loads(decoded.decode("utf-8"))

                def send_json(connection: socket.socket, message: dict[str, object]) -> None:
                    payload = json.dumps(message).encode("utf-8")
                    self.assertLess(len(payload), 126)
                    connection.sendall(bytes([0x81, len(payload)]) + payload)

                def serve() -> None:
                    try:
                        connection, _ = listener.accept()
                        with connection:
                            request = bytearray()
                            while not request.endswith(b"\r\n\r\n"):
                                request.extend(read_exact(connection, 1))
                            key = next(
                                line.split(b":", 1)[1].strip()
                                for line in bytes(request).split(b"\r\n")
                                if line.lower().startswith(b"sec-websocket-key:")
                            )
                            accepted = base64.b64encode(
                                hashlib.sha1(key + b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11").digest()
                            )
                            connection.sendall(
                                b"HTTP/1.1 101 Switching Protocols\r\n"
                                b"Connection: Upgrade\r\n"
                                b"Upgrade: websocket\r\n"
                                b"Sec-WebSocket-Accept: " + accepted + b"\r\n\r\n"
                            )
                            for expected_method in ("initialize", "initialized", "thread/resume", "turn/start"):
                                message = read_frame(connection)
                                self.assertEqual(message["method"], expected_method)
                                messages.append(message)
                                if "id" not in message:
                                    continue
                                if expected_method == "turn/start" and not reply_to_turn_start:
                                    return
                                if expected_method == "initialize":
                                    send_json(connection, {"method": "thread/status/changed", "params": {}})
                                result: dict[str, object] = {"turn": {"id": "desktop-turn"}} if expected_method == "turn/start" else {}
                                send_json(connection, {"id": message["id"], "result": result})
                            send_json(
                                connection,
                                {
                                    "method": "turn/completed",
                                    "params": {"threadId": "desktop-thread", "turn": {"id": "desktop-turn"}},
                                },
                            )
                    except BaseException as exc:
                        errors.append(exc)
                    finally:
                        listener.close()

                thread = threading.Thread(target=serve)
                thread.start()
                return socket_path, thread, messages, errors

            request = {
                "target": {"kind": "session", "value": "desktop-thread"},
                "queue_dir": str(Path(tmp) / "queue"),
                "sandbox_mode": "workspace-write",
                "approval_policy": "on-request",
                "approvals_reviewer": "auto_review",
            }
            payload = {"request": request, "prompt": "Desktop callback test", "cwd": tmp, "timeout": 2.0}
            socket_path, server_thread, messages, errors = start_server(reply_to_turn_start=True)
            with mock.patch.dict(
                os.environ,
                {
                    cli.DESKTOP_APP_SERVER_ENV: "1",
                    cli.APP_SERVER_SOCKET_ENV: str(socket_path),
                    cli.ALLOW_APP_SERVER_SOCKET_OVERRIDE_ENV: "1",
                },
                clear=False,
            ):
                delivery = cli.start_desktop_app_server_turn(payload)
            self.assertIsNotNone(delivery)
            assert delivery is not None
            self.assertTrue(delivery.wait_for_completion(1.0))
            delivery.close()
            server_thread.join(timeout=5)

            self.assertFalse(server_thread.is_alive())
            self.assertEqual(errors, [])
            self.assertEqual([message["method"] for message in messages], ["initialize", "initialized", "thread/resume", "turn/start"])
            self.assertTrue(all("jsonrpc" not in message for message in messages))
            self.assertEqual(messages[0]["params"]["capabilities"], {"experimentalApi": True})
            self.assertEqual(
                messages[-1]["params"]["sandboxPolicy"],
                {"type": "workspaceWrite", "writableRoots": [str(Path(tmp) / "queue")]},
            )

            socket_path, server_thread, messages, errors = start_server(reply_to_turn_start=False)
            with mock.patch.dict(
                os.environ,
                {
                    cli.DESKTOP_APP_SERVER_ENV: "1",
                    cli.APP_SERVER_SOCKET_ENV: str(socket_path),
                    cli.ALLOW_APP_SERVER_SOCKET_OVERRIDE_ENV: "1",
                },
                clear=False,
            ):
                unknown_delivery = cli.start_desktop_app_server_turn(payload)
            self.assertIsNotNone(unknown_delivery)
            assert unknown_delivery is not None
            self.assertFalse(unknown_delivery.wait_for_completion(0.01))
            unknown_delivery.close()
            server_thread.join(timeout=5)
            self.assertFalse(server_thread.is_alive())
            self.assertEqual(errors, [])
            self.assertEqual(messages[-1]["method"], "turn/start")

    def test_desktop_delivery_worker_retains_ack_lease_until_turn_completion(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ack_marker = Path(tmp) / "acks" / "callback.json"

            class CompletingDelivery:
                def __init__(self) -> None:
                    self.waits = 0
                    self.closed = False

                def wait_for_completion(self, _timeout: float) -> bool:
                    self.waits += 1
                    if self.waits == 1:
                        ack_marker.parent.mkdir(parents=True)
                        ack_marker.write_text("{}\n", encoding="utf-8")
                        return False
                    return True

                def close(self) -> None:
                    self.closed = True

            delivery = CompletingDelivery()
            payload = {
                "command": ["unused"],
                "prompt": "wake up",
                "cwd": tmp,
                "request": {},
                "timeout": 1.0,
                "ack_path": str(ack_marker),
                "canceled_path": str(Path(tmp) / "canceled.json"),
            }
            read_fd, lock_fd = os.pipe()
            output = io.StringIO()
            try:
                with (
                    mock.patch.dict(os.environ, {"CODEX_LONG_TASK_DELIVERY_LOCK_FDS": json.dumps([lock_fd])}, clear=False),
                    mock.patch.object(sys, "stdin", io.StringIO(json.dumps(payload))),
                    mock.patch.object(sys, "stdout", output),
                    mock.patch.object(cli, "start_desktop_app_server_turn", return_value=delivery),
                    mock.patch.object(cli.subprocess, "Popen") as popen,
                ):
                    self.assertEqual(cli.delivery_worker_main(), 0)
            finally:
                os.close(read_fd)

            result = json.loads(output.getvalue())
            self.assertEqual(result["returncode"], 0)
            self.assertTrue(result["turn_completed"])
            self.assertGreaterEqual(delivery.waits, 2)
            self.assertTrue(delivery.closed)
            popen.assert_not_called()

    def test_desktop_app_server_explicit_turn_rejection_allows_cli_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            class RejectedConnection:
                def __init__(self) -> None:
                    self.methods: list[str] = []
                    self.closed = False

                def connect(self) -> None:
                    pass

                def request(self, method: str, _params: dict[str, object]) -> dict[str, object]:
                    self.methods.append(method)
                    if method == "turn/start":
                        raise cli.AppServerRpcError("App Server turn/start error: rejected")
                    return {}

                def notify(self, method: str, _params: dict[str, object]) -> None:
                    self.methods.append(method)

                def close(self) -> None:
                    self.closed = True

            connection = RejectedConnection()
            payload = {
                "request": {
                    "target": {"kind": "session", "value": "desktop-thread"},
                    "queue_dir": str(Path(tmp) / "queue"),
                    "sandbox_mode": "workspace-write",
                },
                "prompt": "Desktop callback test",
                "cwd": tmp,
                "timeout": 1.0,
            }
            with (
                mock.patch.dict(os.environ, {cli.DESKTOP_APP_SERVER_ENV: "1"}, clear=False),
                mock.patch.object(cli, "AppServerConnection", return_value=connection),
            ):
                self.assertIsNone(cli.start_desktop_app_server_turn(payload))

            self.assertEqual(connection.methods, ["initialize", "initialized", "thread/resume", "turn/start"])
            self.assertTrue(connection.closed)

    def test_desktop_app_server_rejects_invalid_protocol_and_ungated_socket_override(self) -> None:
        deadline = time.monotonic() + 1.0
        for frame in (
            bytes([0xC1, 0x00]),
            bytes([0x81, 0x80]) + b"mask",
            bytes([0x09, 0x00]),
        ):
            connection = cli.AppServerConnection(Path("/tmp/not-used.sock"), 1.0)
            connection.buffer = frame
            with self.assertRaises(cli.AppServerProtocolError):
                connection._receive_frame(deadline)

        connection = cli.AppServerConnection(Path("/tmp/not-used.sock"), 1.0)
        with (
            mock.patch.object(connection, "_send_frame"),
            mock.patch.object(connection, "_receive_json", return_value={"id": 1, "result": {}}),
        ):
            self.assertEqual(connection.request("initialize", {}), {})

        connection = cli.AppServerConnection(Path("/tmp/not-used.sock"), 1.0)
        invalid_message = json.dumps(
            {
                "params": {"threadId": "desktop-thread", "turn": {"id": "turn-1"}},
            }
        ).encode("utf-8")
        with mock.patch.object(connection, "_receive_message", return_value=(0x1, invalid_message)):
            with self.assertRaises(cli.AppServerProtocolError):
                connection._receive_json(deadline)

        request = {"target": {"kind": "session", "value": "desktop-thread"}}
        with (
            mock.patch.dict(
                os.environ,
                {
                    cli.DESKTOP_APP_SERVER_ENV: "1",
                    cli.APP_SERVER_SOCKET_ENV: "/tmp/override.sock",
                    cli.ALLOW_APP_SERVER_SOCKET_OVERRIDE_ENV: "0",
                },
                clear=False,
            ),
            mock.patch.object(cli, "codex_home", return_value=Path("/tmp/codex-home")),
        ):
            self.assertEqual(cli.desktop_app_server_socket(request), Path("/tmp/codex-home/app-server-control/app-server-control.sock"))

    def test_desktop_delivery_worker_uses_cli_after_explicit_rejection(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            payload = {
                "command": ["codex", "exec", "resume"],
                "prompt": "wake up",
                "cwd": tmp,
                "request": {},
                "timeout": 1.0,
                "ack_path": str(Path(tmp) / "acks" / "callback.json"),
                "canceled_path": str(Path(tmp) / "canceled.json"),
            }
            process = mock.Mock(returncode=0)
            process.communicate.return_value = ("", "")
            read_fd, lock_fd = os.pipe()
            output = io.StringIO()
            try:
                with (
                    mock.patch.dict(os.environ, {"CODEX_LONG_TASK_DELIVERY_LOCK_FDS": json.dumps([lock_fd])}, clear=False),
                    mock.patch.object(sys, "stdin", io.StringIO(json.dumps(payload))),
                    mock.patch.object(sys, "stdout", output),
                    mock.patch.object(cli, "start_desktop_app_server_turn", return_value=None),
                    mock.patch.object(cli.subprocess, "Popen", return_value=process) as popen,
                ):
                    self.assertEqual(cli.delivery_worker_main(), 0)
            finally:
                os.close(read_fd)

            self.assertEqual(json.loads(output.getvalue())["returncode"], 0)
            popen.assert_called_once()

    def test_desktop_delivery_worker_suppresses_cli_on_unknown_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            class IncompleteDelivery:
                def __init__(self) -> None:
                    self.closed = False

                def wait_for_completion(self, timeout: float) -> bool:
                    time.sleep(timeout)
                    return False

                def close(self) -> None:
                    self.closed = True

            delivery = IncompleteDelivery()
            queue_root = Path(tmp) / "queue"
            request = {
                "version": 1,
                "id": "desktop-timeout",
                "created_at": 1.0,
                "cwd": tmp,
                "target": {"kind": "session", "value": "desktop-thread"},
                "prompt": "wake up",
            }
            payload = {
                "command": ["unused"],
                "prompt": "wake up",
                "cwd": tmp,
                "request": request,
                "queue_dir": str(queue_root),
                "timeout": 1.0,
                "ack_path": str(Path(tmp) / "acks" / "callback.json"),
                "canceled_path": str(Path(tmp) / "canceled.json"),
            }
            read_fd, lock_fd = os.pipe()
            output = io.StringIO()
            try:
                with (
                    mock.patch.dict(
                        os.environ,
                        {
                            "CODEX_LONG_TASK_DELIVERY_LOCK_FDS": json.dumps([lock_fd]),
                            cli.TARGET_LOCK_DIR_ENV: str(Path(tmp) / "target-locks"),
                        },
                        clear=False,
                    ),
                    mock.patch.object(sys, "stdin", io.StringIO(json.dumps(payload))),
                    mock.patch.object(sys, "stdout", output),
                    mock.patch.object(cli, "start_desktop_app_server_turn", return_value=delivery),
                    mock.patch.object(cli.subprocess, "Popen") as popen,
                ):
                    self.assertEqual(cli.delivery_worker_main(), 0)
            finally:
                os.close(read_fd)

            result = json.loads(output.getvalue())
            self.assertEqual(result["returncode"], 125)
            self.assertTrue(result["manual_recovery_required"])
            self.assertTrue(delivery.closed)
            popen.assert_not_called()
            with mock.patch.dict(os.environ, {cli.TARGET_LOCK_DIR_ENV: str(Path(tmp) / "target-locks")}, clear=False):
                self.assertTrue(cli.retained_target_lease_is_held(request))

    def test_desktop_delivery_worker_clears_timeout_marker_after_late_ack(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            class IncompleteDelivery:
                def wait_for_completion(self, timeout: float) -> bool:
                    time.sleep(timeout)
                    return False

                def close(self) -> None:
                    pass

            queue_root = Path(tmp) / "queue"
            request = {
                "version": 1,
                "id": "desktop-late-ack",
                "created_at": 1.0,
                "cwd": tmp,
                "target": {"kind": "session", "value": "desktop-thread"},
                "prompt": "wake up",
            }
            ack = Path(tmp) / "acks" / "callback.json"
            payload = {
                "command": ["unused"],
                "prompt": "wake up",
                "cwd": tmp,
                "request": request,
                "queue_dir": str(queue_root),
                "timeout": 1.0,
                "ack_path": str(ack),
                "canceled_path": str(Path(tmp) / "canceled.json"),
            }
            original_retain = cli.retain_target_lease

            def retain_then_ack(root: Path, callback: dict[str, object]) -> None:
                original_retain(root, callback)
                cli.write_request(ack, {"id": callback["id"], "marked_at": time.time()})

            read_fd, lock_fd = os.pipe()
            output = io.StringIO()
            try:
                with (
                    mock.patch.dict(
                        os.environ,
                        {
                            "CODEX_LONG_TASK_DELIVERY_LOCK_FDS": json.dumps([lock_fd]),
                            cli.TARGET_LOCK_DIR_ENV: str(Path(tmp) / "target-locks"),
                        },
                        clear=False,
                    ),
                    mock.patch.object(sys, "stdin", io.StringIO(json.dumps(payload))),
                    mock.patch.object(sys, "stdout", output),
                    mock.patch.object(cli, "start_desktop_app_server_turn", return_value=IncompleteDelivery()),
                    mock.patch.object(cli, "retain_target_lease", side_effect=retain_then_ack),
                ):
                    self.assertEqual(cli.delivery_worker_main(), 0)
            finally:
                os.close(read_fd)

            self.assertEqual(json.loads(output.getvalue())["returncode"], 0)
            with mock.patch.dict(os.environ, {cli.TARGET_LOCK_DIR_ENV: str(Path(tmp) / "target-locks")}, clear=False):
                self.assertFalse(cli.retained_target_lease_is_held(request))

    def test_manual_desktop_recovery_retains_target_lease_until_canceled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "queue"
            cli.ensure_daemon_dirs(root)
            request = {
                "version": 1,
                "id": "desktop-unknown",
                "created_at": 1.0,
                "cwd": tmp,
                "target": {"kind": "session", "value": "desktop-thread"},
                "prompt": "wake up",
                "retain_target_lease": True,
            }
            cli.write_request(cli.request_path(root, "failed", "desktop-unknown"), request)
            with mock.patch.dict(os.environ, {cli.TARGET_LOCK_DIR_ENV: str(Path(tmp) / "target-locks")}, clear=False):
                cli.retain_target_lease(root, request)
                self.assertTrue(cli.target_has_live_resume(root, request))
                self.assertTrue(cli.cancel_one(root, "desktop-unknown", "manual recovery resolved"))
                self.assertFalse(cli.target_has_live_resume(root, request))

    def test_manual_desktop_recovery_lease_blocks_same_session_across_queue_roots(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root_a = Path(tmp) / "queue-a"
            root_b = Path(tmp) / "queue-b"
            target = {"kind": "session", "value": "desktop-thread"}
            failed = {
                "version": 1,
                "id": "desktop-unknown",
                "created_at": 1.0,
                "cwd": tmp,
                "target": target,
                "prompt": "wake up",
                "retain_target_lease": True,
            }
            cli.ensure_daemon_dirs(root_a)
            cli.write_request(cli.request_path(root_a, "failed", "desktop-unknown"), failed)
            self._write_request(root_b, "same-session", tmp, target=target)
            with mock.patch.dict(os.environ, {cli.TARGET_LOCK_DIR_ENV: str(Path(tmp) / "target-locks")}, clear=False):
                cli.retain_target_lease(root_a, failed)
                self.assertIsNone(cli.select_pending(root_b, time.time()))
                self.assertTrue(cli.cancel_one(root_a, "desktop-unknown", "manual recovery resolved"))
                self.assertEqual(cli.select_pending(root_b, time.time()).stem, "same-session")

    def test_cancel_releases_timeout_marker_while_parent_still_marks_running(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "queue"
            target = {"kind": "session", "value": "timeout-thread"}
            self._write_request(root, "timeout-cancel", tmp, target=target)
            cli.ensure_daemon_dirs(root)
            cli.move_request(cli.request_path(root, "pending", "timeout-cancel"), root / "running")
            request = cli.load_request(cli.request_path(root, "running", "timeout-cancel"))

            with mock.patch.dict(os.environ, {cli.TARGET_LOCK_DIR_ENV: str(Path(tmp) / "target-locks")}, clear=False):
                cli.retain_target_lease(root, request)
                self.assertTrue(cli.cancel_one(root, "timeout-cancel", "stop after timeout"))
                self.assertFalse(cli.retained_target_lease_is_held(request))

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
