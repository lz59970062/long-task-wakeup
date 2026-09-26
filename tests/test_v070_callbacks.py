"""Requirement tests: short callbacks retain actionable, private evidence.

No token counter is assumed. Character budgets catch unbounded command/message
regressions while the original evidence must remain available on disk.
"""
from __future__ import annotations

import argparse
import json
import shlex
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from long_task_callback import cli


class CompactCallbackTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="ltc callback ")
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.root = self.directory / "queue's data"
        self.root.mkdir()

    def make_callback(self, ident: str, **overrides: object) -> tuple[dict[str, object], str]:
        arguments = dict(
            agent="codex", session="bound-original-session", last=False,
            task="Compare candidate with frozen reference", cwd=str(self.directory),
            command="python compare.py --frozen reference.json", exit_code=0,
            message=None, queue_dir=str(self.root), _callback_id=ident,
        )
        arguments.update(overrides)
        args = argparse.Namespace(**arguments)
        prompt = cli.build_prompt(args, duration=125.5)
        request = cli.make_request(args, prompt)
        return request, prompt

    def prepare(self, ident: str, **overrides: object) -> dict[str, object]:
        request, prompt = self.make_callback(ident, **overrides)
        return cli.prepare_request_for_queue(self.root, request, prompt)

    def test_new_default_sends_short_status_and_keeps_exact_full_evidence(self) -> None:
        command = "python train.py --configuration " + "very-long-argument=" * 700
        message = "HANDOFF: inspect held-out failures.\n" + "evidence detail " * 900
        request, full = self.make_callback("compact-result", command=command, message=message)
        prepared = cli.prepare_request_for_queue(self.root, request, full)
        self.assertEqual(prepared["prompt_format"], "compact-v1")
        compact = prepared["prompt_compact"]
        self.assertLess(len(compact), 1200)
        self.assertLess(len(prepared["prompt"]), 1500)
        self.assertNotIn(command, compact)
        self.assertNotIn(message, compact)
        for essential in ("compact-result", "bound-original-session", "ack", "--id", str(self.root)):
            self.assertIn(essential, compact)
        details = Path(prepared["prompt_details_path"])
        self.assertTrue(details.is_file())
        self.assertIn(full, details.read_text(encoding="utf-8"))
        self.assertEqual(stat.S_IMODE(details.stat().st_mode), 0o600)
        self.assertIn(str(details), compact)
        self.assertIn("read", compact.lower())
        self.assertIn("details", compact.lower())

    def test_ack_line_can_be_executed_with_spaces_and_quotes_without_wrong_queue(self) -> None:
        launcher = "/opt/LTC user's tools/ltc"
        with mock.patch.object(cli, "console_script_path", return_value=launcher):
            prepared = self.prepare("ack-correct-queue")
        lines = prepared["prompt_compact"].splitlines()
        candidates = [line for line in lines if "--queue-dir" in line and "--id" in line]
        self.assertEqual(len(candidates), 1)
        self.assertEqual(
            shlex.split(candidates[0]),
            [launcher, "ack", "--queue-dir", str(self.root), "--id", "ack-correct-queue"],
        )

    def test_unknown_outcome_is_visible_and_full_recovery_guidance_is_retained(self) -> None:
        request, full = self.make_callback(
            "unknown-result", exit_code=None,
            message="The task was not automatically restarted. Inspect checkpoints before recreating work.",
        )
        request.update(outcome="unknown", recovery_reason="worker_disappeared_before_completion")
        prepared = cli.prepare_request_for_queue(self.root, request, full)
        self.assertIn("unknown", prepared["prompt_compact"].lower())
        self.assertNotIn("exit=0", prepared["prompt_compact"].lower())
        self.assertIn("read", prepared["prompt_compact"].lower())
        details = Path(prepared["prompt_details_path"]).read_text(encoding="utf-8")
        self.assertIn("not automatically restarted", details)
        self.assertIn("checkpoints", details)

    def test_explicit_full_format_remains_available(self) -> None:
        message = "USER REQUESTED FULL EVIDENCE " * 100
        request, full = self.make_callback("full-request", message=message, callback_format="full")
        prepared = cli.prepare_request_for_queue(self.root, request, full)
        self.assertNotEqual(prepared.get("prompt_format"), "compact-v1")
        self.assertIn(message, prepared["prompt"])

    def test_legacy_request_is_not_silently_reinterpreted_or_loses_handoff(self) -> None:
        request, full = self.make_callback("legacy", message="LEGACY HANDOFF: examine output carefully")
        request.pop("prompt_format", None)
        prepared = cli.prepare_request_for_queue(self.root, request, full)
        self.assertIn("LEGACY HANDOFF: examine output carefully", prepared["prompt"])
        self.assertIn("LEGACY HANDOFF: examine output carefully", prepared["prompt_compact"])
        self.assertIn("bound-original-session", prepared["prompt_compact"])

    def test_new_callback_cadence_has_no_bookkeeping_prose_and_retry_stays_stable(self) -> None:
        hook = self.directory / "callback-prompt.md"
        hook.write_text("USER REMINDER: preserve negative evidence", encoding="utf-8")
        outputs = []
        requests = []
        for sequence in range(1, 6):
            request = self.prepare(f"cadence-{sequence}")
            requests.append(request)
            payload = dict(prompt=request["prompt"], request=request,
                           queue_dir=str(self.root), callback_hook_path=str(hook))
            selected = cli.select_delivery_prompt(payload)
            self.assertEqual(payload["prompt"], selected)
            self.assertNotIn("Reminder cadence:", selected)
            self.assertEqual("USER REMINDER" in selected, sequence in (1, 4))
            self.assertIn("bound-original-session", selected)
            self.assertIn("ack", selected)
            outputs.append(selected)
        request = json.loads(json.dumps(requests[1]))
        retry = dict(prompt=request["prompt"], request=request,
                     queue_dir=str(self.root), callback_hook_path=str(hook))
        self.assertEqual(cli.select_delivery_prompt(retry), outputs[1])
        self.assertEqual(list((self.root / "pending").glob("*.json")), [])


if __name__ == "__main__":
    unittest.main()
