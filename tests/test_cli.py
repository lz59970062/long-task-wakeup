from __future__ import annotations

import argparse
import json
import os
import stat
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

from long_task_callback import cli


class CliTests(unittest.TestCase):
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

            self.assertEqual(cli.enqueue_request(args, "wake up"), 0)
            queued = list((Path(tmp) / "pending").glob("*.json"))
            self.assertEqual(len(queued), 1)
            request = json.loads(queued[0].read_text(encoding="utf-8"))

            self.assertIn("codex-long-task-wakeup", request["prompt"])
            self.assertIn(" ack ", request["prompt"])
            self.assertIn(str(request["id"]), request["prompt"])
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

            self.assertTrue((root / "done" / "acked.json").exists())
            self.assertTrue((root / "acks" / "acked.json").exists())
            self.assertFalse((root / "pending" / "acked.json").exists())

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

    def _write_request(self, root: Path, request_id: str, cwd: str) -> None:
        (root / "pending").mkdir(parents=True)
        request = {
            "version": 1,
            "id": request_id,
            "created_at": 1.0,
            "cwd": cwd,
            "target": {"kind": "last"},
            "prompt": "wake up",
        }
        (root / "pending" / f"{request_id}.json").write_text(
            json.dumps(request) + "\n",
            encoding="utf-8",
        )

    def _fake_codex(self, directory: Path, *, ack: bool, queue: Path, request_id: str) -> Path:
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
                import subprocess
                import sys

                sys.stdin.read()
                {ack_code}
                raise SystemExit(0)
                """
            ),
            encoding="utf-8",
        )
        script.chmod(script.stat().st_mode | stat.S_IXUSR)
        return script


if __name__ == "__main__":
    unittest.main()
