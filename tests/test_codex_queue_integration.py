"""Opt-in end-to-end runs against real Codex with a local stub model and isolated homes.

Set LTC_TEST_NATIVE_CODEX_BIN to a real Codex executable and LTC_TEST_E2E_ROOT to a
directory outside /tmp (Codex's workspace-write sandbox can write /tmp, which would
hide a blocked ACK). Set LTC_TEST_OLD_SOURCE to the src directory of an older LTC to
also run its coordinator against new records. Nothing here opens Desktop, reads
account credentials, calls a remote model or touches a service manager.

Each chat is the real Codex TUI in a pseudo-terminal. The stub model answers a
callback by asking Codex to run the callback's own ACK command, so the ACK goes
through the thread's real sandbox.
"""
from __future__ import annotations

import http.server
import json
import os
from pathlib import Path
import re
import select
import shutil
import signal
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid

try:
    import fcntl
    import pty
    import termios
except ImportError:  # Windows, where these runs are skipped.
    fcntl = pty = termios = None

SOURCE = Path(__file__).resolve().parents[1] / "src"
CODEX = os.environ.get("LTC_TEST_NATIVE_CODEX_BIN", "")
E2E_ROOT = os.environ.get("LTC_TEST_E2E_ROOT", "")
OLD_SOURCE = os.environ.get("LTC_TEST_OLD_SOURCE", "")
ACK_LINE = re.compile(r"ACK after inspection:\n(.+)")
CALLBACK_LINE = re.compile(r"\[long-task-callback\] [A-Za-z0-9_-]+")


class StubModel:
    """A Responses API endpoint that logs what each turn was asked and scripts the reply."""

    def __init__(self) -> None:
        self.requests: list[dict[str, object]] = []
        self.answer_with_ack = True
        self.holding = threading.Event()
        self.release = threading.Event()
        self.release.set()
        model = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_GET(self):
                self.reply("application/json", b'{"models":[],"data":[]}')

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                data = model.respond(body)
                self.reply("text/event-stream", data)

            def reply(self, kind, data):
                self.send_response(200)
                self.send_header("Content-Type", kind)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def respond(self, body: dict[str, object]) -> bytes:
        items = body.get("input") or [{}]
        last = items[-1]
        text = ""
        if last.get("type") == "message":
            text = "".join(part.get("text", "") for part in last.get("content", []) if isinstance(part, dict))
        output = last.get("output") if last.get("type") == "function_call_output" else None
        tools = [tool.get("name") for tool in body.get("tools", [])]
        self.requests.append({"text": text, "output": output, "at": time.time(), "tools": tools})
        if "HOLD_TURN" in text:
            self.holding.set()
            self.release.wait(timeout=60)
        # Codex's title request quotes the same text but offers no tools.
        ack = ACK_LINE.search(text) if self.answer_with_ack and "exec_command" in tools and CALLBACK_LINE.match(text) else None
        if ack:
            item = {"type": "function_call", "id": "fc_" + uuid.uuid4().hex, "call_id": "call_" + uuid.uuid4().hex,
                    "name": "exec_command", "arguments": json.dumps({"cmd": ack[1]}), "status": "completed"}
        else:
            item = {"type": "message", "id": "msg_" + uuid.uuid4().hex, "role": "assistant", "status": "completed",
                    "content": [{"type": "output_text", "text": "STUB_REPLY", "annotations": []}]}
        response = {"id": "resp_" + uuid.uuid4().hex, "object": "response", "status": "completed", "output": [item],
                    "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2}}
        events = [{"type": "response.created", "response": dict(response, status="in_progress", output=[])},
                  {"type": "response.output_item.done", "output_index": 0, "item": item},
                  {"type": "response.completed", "response": response}]
        return "".join("event: " + e["type"] + "\ndata: " + json.dumps(e) + "\n\n" for e in events).encode()

    def deliveries(self, callback_id: str) -> int:
        """Turns the callback itself started; later requests of the same turn end in tool output."""
        return sum(1 for r in self.requests if str(r["text"]).startswith(f"[long-task-callback] {callback_id}"))

    def close(self) -> None:
        self.release.set()
        self.server.shutdown()
        self.server.server_close()


class Chat:
    """The real Codex TUI on a thread, in a pseudo-terminal."""

    def __init__(self, argv: list[str], env: dict[str, str], cwd: Path, log: Path) -> None:
        self.master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 50, 160, 0, 0))
        self.process = subprocess.Popen(argv, stdin=slave, stdout=slave, stderr=slave, cwd=cwd, env=env,
                                        start_new_session=True)
        os.close(slave)
        self.log = log.open("ab")
        self.reader = threading.Thread(target=self.read, daemon=True)
        self.reader.start()

    def read(self) -> None:
        while self.process.poll() is None:
            ready, _, _ = select.select([self.master], [], [], 0.2)
            if not ready:
                continue
            try:
                data = os.read(self.master, 65536)
            except OSError:
                return
            self.log.write(data)
            self.log.flush()
            if b"\x1b[6n" in data:
                os.write(self.master, b"\x1b[1;1R")
            if b"\x1b[c" in data:
                os.write(self.master, b"\x1b[?62c")

    def close(self) -> None:
        if self.process.poll() is None:
            os.killpg(self.process.pid, signal.SIGTERM)
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(self.process.pid, signal.SIGKILL)
                self.process.wait(timeout=10)
        self.reader.join(timeout=2)
        os.close(self.master)
        self.log.close()


@unittest.skipUnless(sys.platform == "linux" and CODEX and E2E_ROOT,
                     "opt in with LTC_TEST_NATIVE_CODEX_BIN and LTC_TEST_E2E_ROOT on Linux")
class CodexQueueEndToEnd(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="run-", dir=E2E_ROOT)).resolve()
        self.assertFalse(str(self.root).startswith("/tmp/"), "the sandbox can write /tmp")
        self.home = self.root / "home"
        self.profile = self.root / "profile"
        self.state = self.root / "ltc"
        self.work = self.root / "work"
        self.queue = self.state / "queue"
        for directory in (self.home, self.profile, self.work):
            directory.mkdir()
        self.model = StubModel()
        self.addCleanup(self.model.close)
        (self.profile / "config.toml").write_text(
            'model = "stub"\nmodel_provider = "stub"\ncheck_for_update_on_startup = false\n'
            '[model_providers.stub]\nname = "stub"\n'
            f'base_url = "http://127.0.0.1:{self.model.server.server_port}/v1"\n'
            'wire_api = "responses"\nrequires_openai_auth = false\n'
            f'[projects."{self.work}"]\ntrust_level = "trusted"\n', encoding="utf-8")
        self.daemon = None
        self.chat = None
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.addCleanup(self.teardown_processes)

    def teardown_processes(self):
        if self.chat is not None:
            self.chat.close()
        self.stop_daemon()
        self.kill_shared_app_server()

    def kill_shared_app_server(self):
        # The TUI starts a shared app-server inside this isolated profile.
        marker = str(self.root).encode()
        for entry in Path("/proc").iterdir():
            if not entry.name.isdigit() or int(entry.name) == os.getpid():
                continue
            try:
                if entry.stat().st_uid == os.getuid() and marker in (entry / "cmdline").read_bytes():
                    os.kill(int(entry.name), signal.SIGKILL)
            except OSError:
                continue

    def base_environment(self, path: str | None = None) -> dict[str, str]:
        return {"PATH": path if path is not None else os.environ["PATH"], "HOME": str(self.home),
                "CODEX_HOME": str(self.profile), "LTC_HOME": str(self.state), "LANG": "C.UTF-8",
                "TERM": "xterm-256color", "PYTHONPATH": str(SOURCE), "LTC_EXECUTION_BACKEND": "screen"}

    def session_environment(self, thread: str, **extra: str) -> dict[str, str]:
        """A shell inside the chat: Desktop's origin marker and thread id, the session's PATH."""
        return {**self.base_environment(), "CODEX_THREAD_ID": thread,
                "CODEX_INTERNAL_ORIGINATOR_OVERRIDE": "Codex Desktop", **extra}

    def new_thread(self) -> str:
        result = subprocess.run([CODEX, "exec", "--json", "-s", "read-only", "-C", str(self.work), "--skip-git-repo-check", "-"],
                                input="first turn\n", env=self.base_environment(), capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr[-2000:])
        return json.loads(result.stdout.splitlines()[0])["thread_id"]

    def open_chat(self, thread: str, *, queue_writable: bool = True) -> None:
        argv = [CODEX, "resume", thread, "-C", str(self.work), "-s", "workspace-write", "-a", "never"]
        if queue_writable:
            argv += ["-c", f"sandbox_workspace_write.writable_roots=[{json.dumps(str(self.state))}]"]
        self.chat = Chat(argv, self.session_environment(thread), self.work, self.root / "chat.log")
        # The TUI is ready once it has drawn its composer; nothing here needs the model.
        self.wait_until(lambda: b"shortcuts" in (self.root / "chat.log").read_bytes(), 30, "the TUI to draw")

    def close_chat(self) -> None:
        self.chat.close()
        self.chat = None

    def start_daemon(self, path: str | None = None) -> None:
        env = self.base_environment(path)
        self.daemon_log = (self.root / "daemon.log").open("ab")
        self.daemon = subprocess.Popen([sys.executable, "-m", "long_task_callback", "daemon", "--queue-dir", str(self.queue),
                                        "--interval", "0.2", "--retries", "0", "--resume-timeout", "60"],
                                       env=env, cwd=self.work, stdin=subprocess.DEVNULL, stdout=self.daemon_log,
                                       stderr=self.daemon_log)

    def stop_daemon(self) -> None:
        if self.daemon is not None:
            self.daemon.terminate()
            self.daemon.wait(timeout=20)
            self.daemon = None
            self.daemon_log.close()

    def ltc(self, env: dict[str, str], *arguments: str, source: Path = SOURCE) -> subprocess.CompletedProcess[str]:
        return subprocess.run([sys.executable, "-m", "long_task_callback", *arguments], env=dict(env, PYTHONPATH=str(source)),
                              cwd=self.work, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=120)

    def done(self, thread: str, task: str = "e2e task", env: dict[str, str] | None = None) -> str:
        result = self.ltc(env or self.session_environment(thread), "done", "--queue-dir", str(self.queue), "--task", task,
                          "--cwd", str(self.work), "--exit-code", "0", "--strict")
        self.assertEqual(result.returncode, 0, result.stderr)
        return re.search(r"queued callback ([0-9a-f]+)", result.stderr)[1]

    def record(self, state: str, callback_id: str) -> dict[str, object]:
        return json.loads((self.queue / state / f"{callback_id}.json").read_text(encoding="utf-8"))

    def receipt(self, callback_id: str) -> dict[str, object]:
        return json.loads((self.queue / "codex-queue" / f"{callback_id}.json").read_text(encoding="utf-8"))

    def has(self, state: str, callback_id: str) -> bool:
        return (self.queue / state / f"{callback_id}.json").is_file()

    def wait_until(self, predicate, timeout: float, what: str) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.1)
        path = self.root / "daemon.log"
        logs = path.read_text(errors="replace")[-3000:] if path.exists() else ""
        self.fail(f"timed out waiting for {what}\n{logs}\nrequests: {[str(r['text'])[:80] for r in self.model.requests]}")

    def awaiting_ack(self, callback_id: str) -> bool:
        return self.has("pending", callback_id) and self.record("pending", callback_id).get("delivery_state") == "queued_awaiting_ack"

    def status(self, thread: str) -> str:
        return self.ltc(self.session_environment(thread), "status", "--queue-dir", str(self.queue)).stdout

    def assert_finished_once(self, callback_id: str) -> None:
        self.wait_until(lambda: self.has("done", callback_id), 60, f"{callback_id} to be acknowledged")
        self.assertEqual(self.model.deliveries(callback_id), 1)
        self.assertEqual(self.receipt(callback_id)["state"], "queued")
        self.assertFalse((self.queue / "environments" / f"{callback_id}.json").exists())

    def leases(self) -> list[Path]:
        return [path for path in self.state.rglob("*") if path.is_file() and path.name.endswith(".retained.json")]

    def assert_no_lease(self) -> None:
        self.assertEqual(self.leases(), [])

    def test_idle_chat_receives_it_once_and_acks_from_its_sandbox(self):
        thread = self.new_thread()
        self.open_chat(thread)
        self.start_daemon()
        callback = self.done(thread)
        self.assert_finished_once(callback)
        record = self.record("done", callback)
        self.assertEqual(record["version"], 5)
        self.assertEqual(record["callback_mode"], "codex-queue")
        self.assertEqual(record["callback_codex_home"], str(self.profile))
        self.assert_no_lease()

    def test_busy_chat_gets_it_after_the_turn(self):
        thread = self.new_thread()
        self.open_chat(thread)
        self.start_daemon()
        self.model.release.clear()
        started = subprocess.run([CODEX, "queue", "--thread", thread, "--message", "HOLD_TURN"], env=self.base_environment(),
                                 cwd=self.work, capture_output=True, text=True, timeout=60)
        self.assertEqual(started.returncode, 0, started.stderr)
        self.assertTrue(self.model.holding.wait(30), "the chat did not start a turn")
        callback = self.done(thread)
        self.wait_until(lambda: self.awaiting_ack(callback), 30, "the queue to accept it")
        self.assertEqual(self.model.deliveries(callback), 0)
        self.assertIn("waiting for the thread to ACK", self.status(thread))
        self.model.release.set()
        self.assert_finished_once(callback)

    def closed_then_reopened(self, *, stop_shared_server: bool) -> None:
        thread = self.new_thread()
        self.open_chat(thread)
        self.close_chat()
        if stop_shared_server:
            self.kill_shared_app_server()
        self.start_daemon()
        callback = self.done(thread)
        self.wait_until(lambda: self.awaiting_ack(callback), 30, "the queue to accept it")
        time.sleep(3)
        if stop_shared_server:
            self.assertEqual(self.model.deliveries(callback), 0)
        else:
            print(f"delivered before reopening, with the shared server alive: {self.model.deliveries(callback)}", file=sys.stderr)
        (self.root / "chat.log").unlink()
        self.open_chat(thread)
        self.assert_finished_once(callback)

    def test_closed_chat_gets_it_when_reopened(self):
        self.closed_then_reopened(stop_shared_server=True)

    def test_closed_chat_with_the_shared_server_still_running(self):
        self.closed_then_reopened(stop_shared_server=False)

    def test_rejected_then_retried(self):
        # Real Codex rejection: the frozen profile does not know this thread yet.
        thread = self.new_thread()
        away = self.profile.with_name("profile.away")
        self.profile.rename(away)
        self.profile.mkdir()
        shutil.copy(away / "config.toml", self.profile / "config.toml")
        self.start_daemon()
        callback = self.done(thread)
        self.wait_until(lambda: self.has("failed", callback), 30, "the rejection")
        record = self.record("failed", callback)
        self.assertEqual(record["delivery_state"], "blocked")
        self.assertIn("no rollout found", record["last_error"])
        self.assertIn("ltc retry --id " + callback, record["last_error"])
        self.assertEqual(self.receipt(callback)["state"], "rejected")
        self.assertNotIn("retain_target_lease", record)
        self.assertEqual(self.model.deliveries(callback), 0)
        self.assert_no_lease()
        self.assertTrue((self.queue / "environments" / f"{callback}.json").is_file())
        shutil.rmtree(self.profile)
        away.rename(self.profile)
        self.open_chat(thread)
        retried = self.ltc(self.session_environment(thread), "retry", "--queue-dir", str(self.queue), "--id", callback)
        self.assertEqual(retried.returncode, 0, retried.stderr)
        self.assert_finished_once(callback)

    def test_stripped_path_uses_the_submitting_session(self):
        # The coordinator's PATH reaches neither `node` nor a Codex; the record and snapshot do.
        thread = self.new_thread()
        self.open_chat(thread)
        self.start_daemon(path="/nonexistent")
        callback = self.done(thread)
        self.assert_finished_once(callback)

    def test_stripped_path_without_a_snapshot_is_repairable(self):
        thread = self.new_thread()
        self.open_chat(thread)
        callback = self.done(thread)
        (self.queue / "environments" / f"{callback}.json").unlink()
        self.start_daemon(path="/nonexistent")
        self.wait_until(lambda: self.has("failed", callback), 30, "the rejection")
        self.assertEqual(self.model.deliveries(callback), 0)
        self.assertIn("node", self.record("failed", callback)["last_error"])
        retried = self.ltc(self.session_environment(thread), "retry", "--queue-dir", str(self.queue), "--id", callback)
        self.assertEqual(retried.returncode, 0, retried.stderr)
        self.assert_finished_once(callback)

    def test_no_ack_and_a_later_callback_still_arrives(self):
        self.model.answer_with_ack = False
        thread = self.new_thread()
        self.open_chat(thread)
        self.start_daemon()
        first = self.done(thread, "first")
        self.wait_until(lambda: self.model.deliveries(first) == 1, 30, "the first callback")
        self.wait_until(lambda: self.awaiting_ack(first), 10, "the first to stay pending")
        second = self.done(thread, "second")
        self.wait_until(lambda: self.model.deliveries(second) == 1, 30, "the second callback")
        self.wait_until(lambda: self.awaiting_ack(second), 10, "the second to stay pending")
        time.sleep(2)
        self.assertEqual(self.model.deliveries(first), 1)
        self.assertEqual(self.status(thread).count("waiting for the thread to ACK"), 2)
        self.assert_no_lease()
        for callback in (first, second):
            acked = self.ltc(self.session_environment(thread), "ack", "--queue-dir", str(self.queue), "--id", callback)
            self.assertEqual(acked.returncode, 0, acked.stderr)
            self.assert_finished_once(callback)

    def test_sandbox_that_cannot_write_the_queue_keeps_it_pending(self):
        thread = self.new_thread()
        self.open_chat(thread, queue_writable=False)
        self.start_daemon()
        callback = self.done(thread)
        self.wait_until(lambda: self.model.deliveries(callback) == 1, 30, "the callback")
        self.wait_until(lambda: any(r["output"] for r in self.model.requests), 30, "the ACK attempt")
        self.assertEqual(self.leases(), [])
        output = next(str(r["output"]) for r in self.model.requests if r["output"])
        self.assertNotIn("Traceback", output)
        self.assertIn("could not record the ACK", output)
        self.assertIn("approve this command", output)
        self.assertFalse((self.queue / "acks" / f"{callback}.json").exists())
        self.wait_until(lambda: self.awaiting_ack(callback), 10, "it to stay pending")
        time.sleep(2)
        self.assertEqual(self.model.deliveries(callback), 1)
        acked = self.ltc(self.session_environment(thread), "ack", "--queue-dir", str(self.queue), "--id", callback)
        self.assertEqual(acked.returncode, 0, acked.stderr)
        self.assert_finished_once(callback)

    def shim(self, body: str) -> dict[str, str]:
        shim = self.root / "codex-shim"
        shim.write_text("#!/bin/sh\n" + body.replace("REAL", json.dumps(CODEX)) + "\n", encoding="utf-8")
        shim.chmod(0o755)
        return {"CODEX_LONG_TASK_WAKEUP_CODEX_BIN": str(shim)}

    def unknown_outcome(self, body: str, state: str = "unknown") -> None:
        thread = self.new_thread()
        self.open_chat(thread)
        self.start_daemon()
        env = self.session_environment(thread, **self.shim(body))
        callback = self.done(thread, env=env)
        self.wait_until(lambda: self.has("failed", callback), 30, "the unknown outcome")
        record = self.record("failed", callback)
        self.assertIs(record["retain_target_lease"], True)
        self.assertEqual(self.receipt(callback)["state"], state)
        refused = self.ltc(env, "retry", "--queue-dir", str(self.queue), "--id", callback)
        self.assertEqual(refused.returncode, 2)
        self.assertIn("outcome is unknown", refused.stderr)
        # The real queue did accept it, so the chat has it exactly once and its ACK settles the record.
        self.wait_until(lambda: self.model.deliveries(callback) == 1, 30, "the callback")
        self.wait_until(lambda: self.has("done", callback) and not self.leases(), 30, "the chat's ACK")
        self.assertEqual(self.model.deliveries(callback), 1)

    def test_killed_sender_holds_the_thread_until_ack_or_cancel(self):
        self.model.answer_with_ack = False
        thread = self.new_thread()
        self.start_daemon()
        env = self.session_environment(thread, **self.shim('REAL "$@"\nkill -9 $$'))
        first = self.done(thread, env=env)
        self.wait_until(lambda: self.has("failed", first), 30, "the unknown outcome")
        self.assertEqual(len(self.leases()), 1)
        later = self.done(thread, "later")
        time.sleep(2)
        self.assertTrue(self.has("pending", later))
        self.assertNotIn("delivery_state", self.record("pending", later))
        self.assertFalse((self.queue / "codex-queue" / f"{later}.json").exists())
        canceled = self.ltc(self.session_environment(thread), "cancel", "--queue-dir", str(self.queue), "--id", first)
        self.assertEqual(canceled.returncode, 0, canceled.stderr)
        self.wait_until(lambda: self.awaiting_ack(later), 30, "the later callback after the cancel")
        self.assert_no_lease()

    def test_killed_after_the_queue_accepted_is_unknown_and_never_resent(self):
        self.unknown_outcome('REAL "$@"\nkill -9 $$')

    def test_exit_zero_with_unreadable_output_is_unknown_and_never_resent(self):
        self.unknown_outcome('REAL "$@" >/dev/null')

    def test_managed_run_calls_back_through_the_queue(self):
        thread = self.new_thread()
        self.open_chat(thread)
        # The coordinator launches the task, so it needs screen; it still cannot reach `node`.
        tools = self.root / "coordinator-bin"
        tools.mkdir()
        (tools / "screen").symlink_to(shutil.which("screen"))
        self.start_daemon(path=str(tools))
        result = self.ltc(self.session_environment(thread), "run", "--queue-dir", str(self.queue), "--backend", "screen",
                          "--task", "managed", "--cwd", str(self.work), "--", sys.executable, "-c", "print('workload ran')")
        self.assertEqual(result.returncode, 0, result.stderr)
        task = re.search(r"submitted managed task ([0-9a-f]+)", result.stderr)[1]
        self.assert_finished_once(task)
        self.assertEqual(self.record("done", task)["version"], 5)
        saved = json.loads((self.state / "tasks" / task / "task.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["version"], 6)
        self.assertEqual(saved["callback_mode"], "codex-queue")
        self.assertIn("workload ran", (self.state / "tasks" / task / "attempt-1.log").read_text(encoding="utf-8"))

    def test_saved_desktop_record_keeps_its_result_and_moves_to_the_queue(self):
        thread = self.new_thread()
        self.start_daemon()
        log = self.root / "saved-workload.log"
        log.write_text("saved result\n", encoding="utf-8")
        record = {"version": 2, "id": "saved", "agent": "codex", "cwd": str(self.work), "created_at": time.time(),
                  "target": {"kind": "session", "value": thread}, "target_source": "fixture", "callback_mode": "desktop",
                  "callback_origin": "desktop", "callback_bridge_file": str(self.profile / "long-task-wakeup/desktop/bridge.json"),
                  "sandbox_mode": "workspace-write", "queue_dir": str(self.queue), "log_path": str(log), "exit_code": 0,
                  "prompt": ("[long-task-callback] saved\nACK after inspection:\n"
                             + f"{sys.executable} {SOURCE / 'long_task_callback/_entry.py'} ack --queue-dir {self.queue} --id saved")}
        (self.queue / "pending").mkdir(parents=True, exist_ok=True)
        (self.queue / "pending/saved.json").write_text(json.dumps(record), encoding="utf-8")
        self.wait_until(lambda: self.has("failed", "saved"), 30, "the saved record to be held")
        held = self.record("failed", "saved")
        self.assertEqual(held["callback_mode"], "desktop")
        self.assertEqual(held["exit_code"], 0)
        self.assertEqual(held["log_path"], str(log))
        self.assertIn("ltc retry --id saved --callback-mode codex-queue", held["last_error"])
        self.open_chat(thread)
        moved = self.ltc(self.session_environment(thread), "retry", "--queue-dir", str(self.queue), "--id", "saved",
                         "--callback-mode", "codex-queue")
        self.assertEqual(moved.returncode, 0, moved.stderr)
        self.assert_finished_once("saved")
        self.assertEqual(self.record("done", "saved")["log_path"], str(log))
        self.assertEqual(self.record("done", "saved")["version"], 5)

    @unittest.skipUnless(OLD_SOURCE, "set LTC_TEST_OLD_SOURCE to an older LTC src directory")
    def test_older_coordinator_keeps_new_records_without_sending_them(self):
        thread = self.new_thread()
        callback = self.done(thread)
        old = Path(OLD_SOURCE)
        route = self.record("pending", callback)
        task = {"version": 6, "id": "newtask", "agent": "codex", "execution_backend": "screen", "screen_session": "ltc-newtask",
                "queue_dir": str(self.queue), "state": "running", "target": route["target"], "callback_mode": "codex-queue",
                "callback_codex_bin": route["callback_codex_bin"], "callback_codex_home": route["callback_codex_home"]}
        task_path = self.state / "tasks" / "newtask" / "task.json"
        task_path.parent.mkdir(parents=True)
        task_path.write_text(json.dumps(task), encoding="utf-8")
        result = self.ltc(self.base_environment(), "daemon", "--queue-dir", str(self.queue), "--once", "--retries", "0", source=old)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(task_path.read_text(encoding="utf-8")), task)
        self.assertIn("invalid managed task record", result.stderr)
        self.assertTrue(self.has("failed", callback))
        self.assertEqual(self.record("failed", callback)["version"], 5)
        self.assertEqual(self.model.deliveries(callback), 0)
        self.assertFalse(any(str(r["text"]).startswith("[long-task-callback]") for r in self.model.requests))
        self.assertFalse((self.queue / "codex-queue").exists())
        unreadable = self.ltc(self.base_environment(), "status", "--queue-dir", str(self.queue), "--state", "failed", source=old)
        self.assertIn("unsupported request version", unreadable.stdout)
        task_path.unlink()  # A fixture with no real owner; the new coordinator would recover it as interrupted.
        self.open_chat(thread)
        self.start_daemon()
        retried = self.ltc(self.session_environment(thread), "retry", "--queue-dir", str(self.queue), "--id", callback)
        self.assertEqual(retried.returncode, 0, retried.stderr)
        self.assert_finished_once(callback)


if __name__ == "__main__":
    unittest.main()
