"""The Desktop launcher is gone: nothing reaches it, and saved `desktop` records have a way out."""
from __future__ import annotations

import argparse
import base64
import contextlib
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

from long_task_callback import callback_transport as transport, cli, codex_queue

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "src"
THREAD = "01a15842-e8a7-7630-9fee-869784c02260"
MESSAGE = "01a19796-8012-7ea3-bd98-60b2b6acbfbc"
REPAIR = "ltc retry --id saved --callback-mode codex-queue"


class RetiredDesktopRouteTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="ltc-retired-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(mock.patch.dict(os.environ, {
            "CODEX_HOME": str(self.root / "profile"), "LTC_HOME": str(self.root / "ltc"),
            "CODEX_LONG_TASK_WAKEUP_TARGET_LOCK_DIR": str(self.root / "locks"), "PATH": os.environ.get("PATH", ""),
            **{key: os.environ[key] for key in ("SystemRoot", "USERPROFILE") if key in os.environ},
        }, clear=True))
        self.errors = io.StringIO()
        self.stack.enter_context(contextlib.redirect_stderr(self.errors))
        self.queue = self.root / "queue"
        cli.ensure_daemon_dirs(self.queue)

    def saved(self, **changes):
        return {"version": 2, "id": "saved", "agent": "codex", "cwd": str(self.root), "created_at": 1.0,
                "target": {"kind": "session", "value": THREAD}, "target_source": "fixture",
                "prompt": "inspect the result", "callback_mode": "desktop", "callback_origin": "desktop",
                "callback_bridge_file": str(self.root / "profile/long-task-wakeup/desktop/bridge.json"),
                "sandbox_mode": "workspace-write", "queue_dir": str(self.queue), "attempts": 0,
                "log_path": "/saved/attempt-1.log", "exit_code": 0, **changes}

    def test_launcher_modules_and_entry_point_are_gone(self):
        for name in ("desktop", "desktop_bridge", "desktop_connection", "desktop_core", "desktop_macos",
                     "desktop_unix", "desktop_windows", "_desktop_entry", "platforms.windows_tcp"):
            with self.subTest(module=name):
                self.assertIsNone(importlib.util.find_spec("long_task_callback." + name))
        self.assertNotIn("ltc-desktop-core", (ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        self.assertNotIn("desktop_windows_assets", (ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    def test_saved_records_load_with_their_results_and_route(self):
        path = self.queue / "pending/saved.json"
        cli.write_request(path, self.saved())
        loaded = cli.load_request(path)
        self.assertEqual(loaded["callback_mode"], "desktop")
        self.assertEqual(loaded["callback_bridge_file"], self.saved()["callback_bridge_file"])
        self.assertEqual(loaded["log_path"], "/saved/attempt-1.log")
        self.assertEqual(loaded["exit_code"], 0)
        path = cli.managed_task_path(self.queue, "t1")
        cli.write_request(path, {"version": 3, "id": "t1", "agent": "codex", "execution_backend": "screen",
                                 "screen_session": "s", "queue_dir": str(self.queue), "callback_mode": "desktop",
                                 "callback_bridge_file": "/saved/bridge.json",
                                 "target": {"kind": "session", "value": THREAD}})
        self.assertEqual(cli.load_managed_task(path)["callback_bridge_file"], "/saved/bridge.json")

    def test_delivery_is_blocked_with_the_exact_command_on_mac_and_windows(self):
        for platform in ("darwin", "win32"):
            with self.subTest(platform=platform), mock.patch.object(sys, "platform", platform), \
                    mock.patch.object(cli, "AppServerConnection") as connection:
                with self.assertRaisesRegex(cli.CallbackTransportBlocked, REPAIR):
                    cli.start_desktop_app_server_turn({"request": self.saved(), "timeout": 2})
                connection.assert_not_called()

    def test_blocking_wins_over_a_disabled_or_missing_connection(self):
        with mock.patch.object(sys, "platform", "darwin"), \
                mock.patch.dict(os.environ, {cli.DESKTOP_APP_SERVER_ENV: "0"}), \
                self.assertRaisesRegex(cli.CallbackTransportBlocked, REPAIR):
            cli.desktop_app_server_socket(self.saved(callback_bridge_file=None, target=None))

    def test_only_the_desktop_route_is_blocked_on_mac_and_windows(self):
        for platform in ("darwin", "win32"):
            for request in (self.saved(callback_mode="cli"), self.saved(callback_mode="manual")):
                with self.subTest(platform=platform, mode=request["callback_mode"]), \
                        mock.patch.object(sys, "platform", platform):
                    cli.desktop_app_server_socket(request)

    @unittest.skipIf(os.name == "nt", "the worker's Job handling and lock inheritance differ on Windows")
    def test_real_worker_reports_blocked_and_starts_no_agent(self):
        marker = self.root / "agent-started"
        request = self.saved()
        payload = {"request": request, "queue_dir": str(self.queue), "timeout": 5, "cwd": str(self.root),
                   "prompt": request["prompt"], "ack_path": str(self.root / "ack"), "canceled_path": str(self.root / "canceled"),
                   "command": [sys.executable, "-c", f"open({str(marker)!r}, 'w').close()"]}
        program = "import sys\nfrom long_task_callback import cli\nsys.platform = 'darwin'\nraise SystemExit(cli.delivery_worker_main())"
        result = subprocess.run([sys.executable, "-c", program], input=json.dumps(payload), capture_output=True, text=True,
                                timeout=60, env={**os.environ, "PYTHONPATH": str(SOURCE),
                                                 "CODEX_LONG_TASK_DELIVERY_LOCK_FDS": "[]"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout.splitlines()[-1])["returncode"], 126, result.stderr)
        self.assertIn(REPAIR, result.stderr)
        self.assertFalse(marker.exists())

    def test_daemon_keeps_a_blocked_record_with_its_results(self):
        cli.write_request(self.queue / "pending/saved.json", self.saved())
        with mock.patch.object(cli, "run_resume_until_exit_or_ack",
                               return_value=(subprocess.CompletedProcess([], 126), False, False)):
            self.assertTrue(cli.process_one(self.queue, argparse.Namespace(retries=10)))
        held = cli.load_request(self.queue / "failed/saved.json")
        self.assertEqual(held["delivery_state"], "blocked")
        self.assertEqual(held["callback_mode"], "desktop")
        self.assertEqual(held["log_path"], "/saved/attempt-1.log")
        self.assertEqual(held["exit_code"], 0)
        self.assertIn(REPAIR, held["last_error"])

    def test_retry_refuses_the_retired_route_with_the_command(self):
        cli.write_request(self.queue / "failed/saved.json", self.saved())
        for platform in ("darwin", "win32"):
            with self.subTest(platform=platform), mock.patch.object(sys, "platform", platform):
                self.assertEqual(cli.retry_callback(argparse.Namespace(queue_dir=str(self.queue), id="saved", callback_mode=None)), 2)
                self.assertIn(REPAIR, self.errors.getvalue())
                self.assertTrue((self.queue / "failed/saved.json").is_file())

    @unittest.skipUnless(os.name == "posix", "the fixture Codex is a shell script")
    def test_retry_moves_the_record_to_the_queue_and_publishes_once(self):
        calls = self.root / "codex-calls"
        codex = self.root / "codex"
        codex.write_text('#!/bin/sh\nif [ "$2" = "--help" ]; then echo "--thread --message"; exit 0; fi\n'
                         f'echo "$@" >> "{calls}"\necho "Queued message {MESSAGE} for thread $3."\n', encoding="utf-8")
        codex.chmod(0o700)
        cli.write_request(self.queue / "failed/saved.json", self.saved())
        with mock.patch.dict(os.environ, {"CODEX_LONG_TASK_WAKEUP_CODEX_BIN": str(codex)}), \
                mock.patch.object(sys, "platform", "darwin"):
            self.assertEqual(cli.retry_callback(argparse.Namespace(queue_dir=str(self.queue), id="saved",
                                                                   callback_mode="codex-queue")), 0, self.errors.getvalue())
            moved = cli.load_request(self.queue / "pending/saved.json")
            self.assertEqual(moved["callback_mode"], "codex-queue")
            self.assertNotIn("callback_bridge_file", moved)
            self.assertEqual(moved["log_path"], "/saved/attempt-1.log")
            self.assertFalse((self.queue / "failed/saved.json").exists())
            result = codex_queue.publish({"request": moved, "queue_dir": str(self.queue), "timeout": 10, "prompt": moved["prompt"]})
        self.assertEqual(result["returncode"], codex_queue.PUBLISHED_RETURN_CODE)
        self.assertEqual(calls.read_text(encoding="utf-8").splitlines(),
                         [f"queue --thread {THREAD} --message inspect the result"])

    @unittest.skipUnless(os.name == "posix", "Windows has no local control socket")
    def test_linux_keeps_delivering_through_the_control_socket(self):
        with mock.patch.object(sys, "platform", "linux"), \
                mock.patch.object(cli, "codex_home", return_value=Path("/fixture/profile")):
            self.assertEqual(cli.desktop_app_server_socket(self.saved()),
                             Path("/fixture/profile/app-server-control/app-server-control.sock"))

    @unittest.skipUnless(os.name == "posix", "Windows has no local control socket")
    def test_linux_blocks_with_the_command_when_the_socket_is_absent(self):
        with mock.patch.object(sys, "platform", "linux"):
            report = transport.inspect_route(self.saved())
        self.assertEqual(report["status"], "blocked")
        self.assertEqual(report["reason"], "desktop_route_unavailable")
        self.assertIn(REPAIR, report["action"])

    def test_setup_still_accepts_the_hidden_launcher_flags(self):
        arguments = ["ltc", "setup", "--desktop-app", "/old/Codex.app", "--desktop-launch-mode", "direct"]
        for argv in (arguments, ["ltc", "setup", "--no-desktop-launcher"]):
            with self.subTest(argv=argv), mock.patch.object(sys, "argv", argv), \
                    mock.patch.object(cli, "setup", return_value=0) as setup:
                self.assertEqual(cli.main(), 0)
                setup.assert_called_once()


@unittest.skipUnless(hasattr(socket, "AF_UNIX") and os.name == "posix", "App Server callbacks use Unix sockets")
class SocketRpcTests(unittest.TestCase):
    def test_websocket_rpc_answers_ping_and_reports_completion(self):
        directory = tempfile.TemporaryDirectory(prefix="ltc-rpc-", dir="/tmp")
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "core.sock"
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(server.close)
        server.bind(str(path))
        server.listen(1)
        server.settimeout(3)
        errors, methods = [], []

        def exact(peer, count):
            data = b""
            while len(data) < count:
                part = peer.recv(count - len(data))
                if not part:
                    raise EOFError("client closed")
                data += part
            return data

        def frame(peer):
            first, second = exact(peer, 2)
            size = second & 127
            if size == 126:
                size = struct.unpack("!H", exact(peer, 2))[0]
            elif size == 127:
                size = struct.unpack("!Q", exact(peer, 8))[0]
            mask, data = exact(peer, 4), exact(peer, size)
            return first & 15, bytes(value ^ mask[index % 4] for index, value in enumerate(data))

        def send(peer, message):
            value = json.dumps(message).encode()
            peer.sendall(bytes([129, len(value)]) + value)

        def serve():
            try:
                peer, _ = server.accept()
                with peer:
                    peer.settimeout(3)
                    header = b""
                    while not header.endswith(b"\r\n\r\n"):
                        header += exact(peer, 1)
                    key = next(line.split(b":", 1)[1].strip() for line in header.split(b"\r\n")
                               if line.lower().startswith(b"sec-websocket-key:"))
                    accepted = base64.b64encode(hashlib.sha1(key + b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11").digest())
                    peer.sendall(b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                                 b"Sec-WebSocket-Accept: " + accepted + b"\r\n\r\n")
                    for expected in ("initialize", "initialized", "thread/resume", "turn/start"):
                        opcode, value = frame(peer)
                        message = json.loads(value)
                        methods.append((opcode, message["method"]))
                        if "id" in message:
                            if expected == "initialize":
                                peer.sendall(b"\x8a\x00\x89\x01p")  # An unsolicited pong, then a ping.
                                if frame(peer) != (10, b"p"):
                                    raise AssertionError("ping was not answered")
                            send(peer, {"id": message["id"], "result": {"turn": {"id": "turn-1"}}})
                    send(peer, {"method": "turn/completed", "params": {"threadId": "original", "turn": {"id": "turn-1"}}})
            except BaseException as error:
                errors.append(error)

        thread = threading.Thread(target=serve)
        thread.start()
        payload = {"request": {"target": {"kind": "session", "value": "original"}, "callback_mode": "cli",
                               "sandbox_mode": "workspace-write", "queue_dir": str(Path(directory.name) / "queue")},
                   "cwd": directory.name, "prompt": "fixture callback", "timeout": 2}
        with mock.patch.object(cli, "desktop_app_server_socket", return_value=path):
            callback = cli.start_desktop_app_server_turn(payload)
        self.assertIsNotNone(callback)
        try:
            self.assertTrue(callback.wait_for_completion(2))
        finally:
            callback.close()
        thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(methods, [(1, "initialize"), (1, "initialized"), (1, "thread/resume"), (1, "turn/start")])


if __name__ == "__main__":
    unittest.main()
