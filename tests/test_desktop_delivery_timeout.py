"""Persist Desktop submission intent before a parent deadline can kill its worker.

The real subprocess/lease handoff runs on Windows and POSIX; the RPC endpoint is
a local fake, so these tests never connect to a model or a user's Desktop task.
"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import textwrap
import time
import unittest
from unittest import mock

from long_task_callback import cli


class DesktopDeliveryTimeoutTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="ltc desktop intent ")
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name).resolve()
        self.root = self.directory / "queue"
        self.events = self.directory / "rpc-events.jsonl"
        self.cli_calls = self.directory / "cli-calls"
        self.request = {
            "version": 1, "id": "desktop-fixture", "created_at": time.time(),
            "agent": "codex", "cwd": str(self.directory), "queue_dir": str(self.root),
            "target": {"kind": "session", "value": "fixture-original-session"},
            "target_source": "explicit", "prompt": "Model-free Desktop delivery fixture",
            "sandbox_mode": "workspace-write", "attempts": 0,
        }
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(mock.patch.dict(os.environ, {
            "CODEX_HOME": str(self.directory / "codex-profile"),
            cli.TARGET_LOCK_DIR_ENV: str(self.directory / "target-locks"),
            "PYTHONPATH": str(Path(cli.__file__).resolve().parents[1]),
            "PYTHONIOENCODING": "utf-8",
        }))
        self.errors = self.stack.enter_context(contextlib.redirect_stderr(io.StringIO()))
        cli.ensure_daemon_dirs(self.root)

    def worker_command(self, mode):
        program = textwrap.dedent("""
            import argparse,json,os,sys,time
            from pathlib import Path
            from long_task_callback import cli
            root=Path(ROOT)
            request=json.loads(REQUEST)
            events=Path(EVENTS)
            mode=MODE
            class FakeConnection:
                def connect(self): pass
                def close(self): pass
                def notify(self, method, params): pass
                def request(self, method, params):
                    if method == 'thread/resume' and mode == 'before-submit-failure':
                        raise cli.AppServerProtocolError('connection lost before turn submission')
                    if method != 'turn/start': return {}
                    with events.open('a',encoding='utf-8') as log:
                        log.write(json.dumps({'method':method,'intent_durable':cli.desktop_submission_pending(root,request)})+'\\n')
                    if mode == 'worker-crash': os._exit(23)
                    if mode == 'explicit-rejection':
                        raise cli.AppServerRpcError('explicit rejection')
                    if mode in ('acknowledged','unknown-after-ack'):
                        cli.ack(argparse.Namespace(queue_dir=str(root),id=request['id'],message='fixture ACK'))
                    if mode in ('unknown','unknown-after-ack'):
                        time.sleep(0.35)
                        raise cli.AppServerProtocolError('turn/start response lost')
                    return {'turn':{'id':'fixture-turn'}}
                def wait_for_turn_completion(self, thread_id, turn_id, timeout): return True
            cli.desktop_app_server_socket=lambda request:Path('unused-fixture.sock')
            cli.AppServerConnection=lambda path,timeout:FakeConnection()
            raise SystemExit(cli.delivery_worker_main())
        """)
        program = program.replace("ROOT", repr(str(self.root))).replace("REQUEST", repr(json.dumps(self.request)))
        program = program.replace("EVENTS", repr(str(self.events))).replace("MODE", repr(mode))
        return [sys.executable, "-c", program]

    def fallback_command(self):
        code = (
            "from pathlib import Path; import sys; sys.stdin.read(); "
            f"Path({str(self.cli_calls)!r}).write_text('called',encoding='utf-8')"
        )
        return [sys.executable, "-c", code]

    def run_worker(self, mode, timeout=1):
        with mock.patch.object(cli, "delivery_worker_command", return_value=self.worker_command(mode)), mock.patch.object(
            cli, "resume_command", return_value=self.fallback_command()
        ):
            return cli.run_resume_until_exit_or_ack(self.root, self.request["id"], self.request,
                                                    argparse.Namespace(resume_timeout=timeout))

    def assert_submission_recorded_once(self):
        events = [json.loads(line) for line in self.events.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(events, [{"method": "turn/start", "intent_durable": True}])

    def tearDown(self):
        for delivery in list(cli._BACKGROUND_RESUMES.values()):
            cli.stop_resume_process(delivery.process)
        cli._BACKGROUND_RESUMES.clear()

    def test_parent_deadline_before_worker_timeout_preserves_unknown_outcome(self):
        result, acked, running = self.run_worker("unknown")
        self.assertEqual(result.returncode, 125, self.errors.getvalue())
        self.assertFalse(acked)
        self.assertFalse(running)
        self.assert_submission_recorded_once()
        self.assertTrue(cli.desktop_submission_pending(self.root, self.request))
        self.assertTrue(cli.retained_target_lease_is_held(self.request))
        self.assertFalse(self.cli_calls.exists())

    def test_worker_crash_after_submission_preserves_unknown_outcome(self):
        result, acked, running = self.run_worker("worker-crash")
        self.assertEqual(result.returncode, 125, self.errors.getvalue())
        self.assertFalse(acked)
        self.assertFalse(running)
        self.assert_submission_recorded_once()
        self.assertTrue(cli.retained_target_lease_is_held(self.request))
        self.assertFalse(self.cli_calls.exists())

    def test_unknown_submission_enters_failed_without_retry_and_blocks_other_queue(self):
        cli.write_request(cli.request_path(self.root, "pending", self.request["id"]), self.request)
        args = argparse.Namespace(resume_timeout=1, retries=3, retry_delay=0, retry_backoff=1)
        with mock.patch.object(cli, "delivery_worker_command", return_value=self.worker_command("unknown")), mock.patch.object(
            cli, "resume_command", return_value=self.fallback_command()
        ):
            self.assertTrue(cli.process_one(self.root, args))
            self.assertFalse(cli.process_one(self.root, args))
        failed = cli.load_request(cli.request_path(self.root, "failed", self.request["id"]))
        self.assertTrue(failed["retain_target_lease"])
        self.assertEqual(failed["attempts"], 1)
        self.assertFalse(cli.request_path(self.root, "pending", self.request["id"]).exists())
        other = self.directory / "other-queue"
        cli.ensure_daemon_dirs(other)
        cli.write_request(cli.request_path(other, "pending", "contender"), dict(self.request, id="contender", queue_dir=str(other)))
        self.assertIsNone(cli.select_pending(other, time.time()))
        self.assert_submission_recorded_once()

    def test_explicit_rejection_clears_intent_and_allows_cli(self):
        result, acked, running = self.run_worker("explicit-rejection", timeout=3)
        self.assertEqual(result.returncode, 0, self.errors.getvalue())
        self.assertFalse(acked)
        self.assertFalse(running)
        self.assert_submission_recorded_once()
        self.assertFalse(cli.desktop_submission_pending(self.root, self.request))
        self.assertTrue(self.cli_calls.exists())

    def test_matching_completion_clears_intent_and_keeps_normal_ack_policy(self):
        result, acked, running = self.run_worker("completed", timeout=3)
        self.assertEqual(result.returncode, 1, self.errors.getvalue())  # Known completion without ACK.
        self.assertFalse(acked)
        self.assertFalse(running)
        self.assert_submission_recorded_once()
        self.assertFalse(cli.desktop_submission_pending(self.root, self.request))
        self.assertFalse(self.cli_calls.exists())

    def test_acknowledged_submission_releases_intent_even_if_rpc_reply_is_lost(self):
        for mode in ("acknowledged", "unknown-after-ack"):
            with self.subTest(mode=mode):
                if cli.ack_path(self.root, self.request["id"]).exists():
                    cli.ack_path(self.root, self.request["id"]).unlink()
                if self.events.exists():
                    self.events.unlink()
                result, acked, _running = self.run_worker(mode, timeout=3)
                self.assertEqual(result.returncode, 0, self.errors.getvalue())
                self.assertTrue(acked)
                self.assertFalse(cli.retained_target_lease_is_held(self.request))
                self.assertFalse(self.cli_calls.exists())
                self.assert_submission_recorded_once()
                self.tearDown()  # Finish the acknowledged worker before the next fixture.

    def test_failure_before_turn_submission_falls_back_without_intent(self):
        result, acked, running = self.run_worker("before-submit-failure", timeout=3)
        self.assertEqual(result.returncode, 0, self.errors.getvalue())
        self.assertFalse(acked)
        self.assertFalse(running)
        self.assertFalse(self.events.exists())
        self.assertFalse(cli.desktop_submission_pending(self.root, self.request))
        self.assertTrue(self.cli_calls.exists())

    def test_foreign_callback_lease_does_not_classify_this_request_as_unknown(self):
        other = dict(self.request, id="another-callback")
        cli.retain_target_lease(self.root, other)
        self.assertFalse(cli.desktop_submission_pending(self.root, self.request))


if __name__ == "__main__":
    unittest.main()
