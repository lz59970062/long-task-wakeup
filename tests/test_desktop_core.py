from __future__ import annotations

import json
import os
from pathlib import Path
import queue
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

from long_task_callback import desktop_core as core
from long_task_callback import desktop_bridge as bridge
from long_task_callback.platforms import windows


def server_frame(opcode, payload, final=True):
    header = bytes([(0x80 if final else 0) | opcode])
    if len(payload) < 126:
        return header + bytes([len(payload)]) + payload
    if len(payload) <= 65535:
        return header + b"\x7e" + struct.pack("!H", len(payload)) + payload
    return header + b"\x7f" + struct.pack("!Q", len(payload)) + payload


def read_client_frame(connection):
    def exact(size):
        result = b""
        while len(result) < size:
            data = connection.recv(size - len(result))
            if not data:
                raise EOFError
            result += data
        return result
    first, second = exact(2)
    length = second & 127
    if length == 126:
        length = struct.unpack("!H", exact(2))[0]
    elif length == 127:
        length = struct.unpack("!Q", exact(8))[0]
    if not second & 128:
        raise AssertionError("Client frame was not masked")
    mask = exact(4)
    payload = exact(length)
    return first & 15, bytes(value ^ mask[index % 4] for index, value in enumerate(payload))


class CoreUnitTests(unittest.TestCase):
    def test_preserves_configuration_and_replaces_only_explicit_stdio(self):
        arguments = ["-c", "global=true", "app-server", "-c", "features.code_mode_host=true", "--analytics-default-enabled", "--listen", "stdio://"]
        self.assertEqual(arguments[:-2], core.app_server_arguments(arguments))
        self.assertEqual(["app-server", "--flag"], core.app_server_arguments(["app-server", "--listen=stdio://", "--flag"]))

    def test_version_help_codegen_and_non_server_calls_bypass_bridge(self):
        for values in (["--version"], ["exec", "app-server"], ["-c", "app-server"], ["app-server", "--help"],
                       ["app-server", "generate-ts"], ["app-server", "generate-json-schema"]):
            with self.subTest(values=values):
                self.assertIsNone(core.app_server_arguments(values))
        for values in (["app-server", "--listen", "ws://127.0.0.1:123"], ["app-server", "--ws-auth", "none"], ["app-server", "--listen"]):
            with self.subTest(values=values), self.assertRaises(core.RelayError):
                core.app_server_arguments(values)

    def test_rejects_wrapper_recursion_and_unverified_executable(self):
        with mock.patch.dict(os.environ, {core.REAL_CODEX_ENV: "relative.exe"}):
            with self.assertRaises(core.RelayError):
                core.real_core()
        with tempfile.TemporaryDirectory() as directory:
            wrapper = Path(directory).resolve() / "ltc-desktop-core.exe"
            wrapper.touch()
            with mock.patch.dict(os.environ, {core.REAL_CODEX_ENV: str(wrapper)}):
                with self.assertRaisesRegex(core.RelayError, "wrapper itself"):
                    core.real_core()

    def test_fragmented_text_handles_ping_and_preserves_partial_reads(self):
        client, server = socket.socketpair()
        self.addCleanup(client.close)
        self.addCleanup(server.close)
        relay = core.WebSocketRelay(client, threading.Event())
        client.settimeout(0.05)
        stream = server_frame(1, b'{"id":"str', False) + server_frame(9, b"hello") + server_frame(0, b'ing","result":7}')
        def delayed():
            server.sendall(stream[:4])
            time.sleep(0.6)  # crosses the internal socket timeout mid-frame
            server.sendall(stream[4:])
        worker = threading.Thread(target=delayed, daemon=True)
        worker.start()
        self.assertEqual(b'{"id":"string","result":7}', relay.receive())
        self.assertEqual((10, b"hello"), read_client_frame(server))
        worker.join(2)

    def test_oversize_masked_and_binary_server_frames_fail_closed(self):
        frames = [b"\x81\x7f" + struct.pack("!Q", core.MAX_MESSAGE_BYTES + 1), b"\x81\x80", server_frame(2, b"binary")]
        for frame in frames:
            with self.subTest(frame=frame):
                client, server = socket.socketpair()
                try:
                    relay = core.WebSocketRelay(client, threading.Event())
                    server.sendall(frame)
                    with self.assertRaises(core.RelayError):
                        relay.receive()
                finally:
                    client.close()
                    server.close()

    def test_stdio_messages_preserve_ids_and_json_bytes(self):
        relay = mock.Mock()
        relay.stop = threading.Event()
        chunks = [b'{"id":"approval", "result":{"ok":true}}\r', b'\n{"id":9007199254740993,"method":"x"}\n', b""]
        with mock.patch.object(os, "read", side_effect=chunks):
            core._stdin_messages(0, relay)
        self.assertEqual([mock.call(1, b'{"id":"approval", "result":{"ok":true}}'),
                          mock.call(1, b'{"id":9007199254740993,"method":"x"}')], relay.send.call_args_list)

    def test_status_exit_code_distinguishes_inactive_metadata(self):
        with mock.patch.object(bridge.windows_io, "require_windows"), mock.patch.object(bridge, "bridge_status", return_value={"running": False}), mock.patch("builtins.print"):
            self.assertEqual(1, bridge.main(["--status"]))


FAKE_CORE = r'''
import base64, hashlib, json, os, pathlib, socket, struct, sys, threading, time
args = sys.argv[1:]
home = pathlib.Path(os.environ["CODEX_HOME"])
port = int(args[args.index("--listen") + 1].rsplit(":", 1)[1])
token = pathlib.Path(args[args.index("--ws-token-file") + 1]).read_text().strip()
(home / "real-core-pid").write_text(str(os.getpid()))
(home / "launch.json").write_text(json.dumps({"args": args, "pipe": os.environ.get("CODEX_APP_TOOLS_PIPE_PATH")}))
server = socket.socket()
server.bind(("127.0.0.1", port))
server.listen(20)
def exact(connection, size):
    value = b""
    while len(value) < size:
        part = connection.recv(size-len(value))
        if not part: raise EOFError
        value += part
    return value
def receive(connection):
    first, second = exact(connection, 2)
    length = second & 127
    if length == 126: length = struct.unpack("!H", exact(connection, 2))[0]
    elif length == 127: length = struct.unpack("!Q", exact(connection, 8))[0]
    mask = exact(connection, 4)
    payload = exact(connection, length)
    return json.loads(bytes(value ^ mask[index % 4] for index, value in enumerate(payload)))
def send(connection, value):
    payload = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()
    if len(payload) < 126: head = b"\x81" + bytes([len(payload)])
    elif len(payload) <= 65535: head = b"\x81\x7e" + struct.pack("!H", len(payload))
    else: head = b"\x81\x7f" + struct.pack("!Q", len(payload))
    connection.sendall(head + payload)
def handle(connection):
    try:
        header = b""
        while b"\r\n\r\n" not in header:
            part = connection.recv(4096)
            if not part: return
            header += part
        fields = dict(line.split(": ", 1) for line in header.decode().split("\r\n")[1:] if ": " in line)
        if fields.get("Authorization") != "Bearer " + token: return
        accept = base64.b64encode(hashlib.sha1((fields["Sec-WebSocket-Key"] + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()).decode()
        connection.sendall(("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: " + accept + "\r\n\r\n").encode())
        while True:
            message = receive(connection)
            if message.get("method") == "crash": os._exit(7)
            if message.get("method") == "stream":
                while True:
                    connection.sendall(b"\x89\x04ping")
                    send(connection, {"method":"fixture/stream", "params":{}})
                    time.sleep(0.01)
            if message.get("method") == "approval":
                send(connection, {"id":"approve/string", "method":"item/commandExecution/requestApproval", "params":{"command":["safe"]}})
                send(connection, {"method":"fixture/notification", "params":{"ok":True}})
            elif "result" in message:
                send(connection, {"method":"fixture/approved", "params":message})
            else:
                send(connection, {"id":message["id"], "result":message.get("params", {})})
    except (OSError, EOFError): pass
    finally: connection.close()
while True:
    connection, _ = server.accept()
    threading.Thread(target=handle, args=(connection,), daemon=True).start()
'''


@unittest.skipUnless(os.name == "nt", "native Desktop Core relay")
class NativeCoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="ltc-core-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.profile = self.root / "profile"
        self.profile.mkdir()
        self.metadata = self.profile / "long-task-wakeup" / "desktop-bridge.json"
        (self.root / "app-server").write_text(FAKE_CORE, encoding="utf-8")
        self.env = os.environ.copy()
        self.env.update({core.REAL_CODEX_ENV: sys.executable, core.BRIDGE_FILE_ENV: str(self.metadata),
                         "CODEX_HOME": str(self.profile), "CODEX_APP_TOOLS_PIPE_PATH": "fresh-test-desktop-pipe",
                         "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")})
        self.command = [sys.executable, "-m", "long_task_callback.desktop_core"]
        self.process = None
        self.addCleanup(self.cleanup_process)

    def cleanup_process(self):
        if self.process is not None:
            if self.process.poll() is None:
                self.process.kill()
            self.process.wait(timeout=5)
            if self.process.stdin and not self.process.stdin.closed:
                self.process.stdin.close()
            if hasattr(self, "reader"):
                self.reader.join(timeout=2)
            self.process.stdout.close()
            self.process.stderr.close()

    def start(self):
        args = ["app-server", "-c", "features.code_mode_host=true", "--analytics-default-enabled"]
        self.process = subprocess.Popen(self.command + args, cwd=self.root, env=self.env,
                                        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.lines = queue.Queue()
        def read():
            for line in iter(self.process.stdout.readline, b""):
                self.lines.put(line)
        self.reader = threading.Thread(target=read, daemon=True)
        self.reader.start()
        deadline = time.monotonic() + 6
        while time.monotonic() < deadline:
            if self.metadata.exists():
                return bridge._read_record(self.metadata)
            if self.process.poll() is not None:
                self.fail(self.process.stderr.read().decode())
            time.sleep(0.025)
        self.fail("Wrapper failed to publish bridge metadata")

    def send(self, value):
        self.process.stdin.write(json.dumps(value, ensure_ascii=False).encode() + b"\n")
        self.process.stdin.flush()

    def response(self):
        try:
            return json.loads(self.lines.get(timeout=5))
        except queue.Empty:
            self.fail("Wrapper did not relay a response")

    def test_transparent_requests_approvals_large_messages_and_fresh_environment(self):
        record = self.start()
        self.send({"id": "desktop/initialize", "method": "initialize", "params": {"client": "original"}})
        self.assertEqual({"id": "desktop/initialize", "result": {"client": "original"}}, self.response())
        launch = json.loads((self.profile / "launch.json").read_text())
        self.assertEqual("fresh-test-desktop-pipe", launch["pipe"])
        self.assertEqual(["-c", "features.code_mode_host=true", "--analytics-default-enabled"], launch["args"][:3])
        self.assertIn("--ws-token-file", launch["args"])
        blob = "x" * (1024 * 1024 + 257)
        self.send({"id": 9007199254740993, "method": "large", "params": {"blob": blob}})
        self.assertEqual({"id": 9007199254740993, "result": {"blob": blob}}, self.response())
        self.send({"id": 42, "method": "approval"})
        self.assertEqual({"id": "approve/string", "method": "item/commandExecution/requestApproval", "params": {"command": ["safe"]}}, self.response())
        self.assertEqual("fixture/notification", self.response()["method"])
        answer = {"id": "approve/string", "result": {"decision": "accept"}}
        self.send(answer)
        self.assertEqual({"method": "fixture/approved", "params": answer}, self.response())
        # A second LTC-side connection cannot outlive the owning Desktop stdin.
        client = socket.create_connection(("127.0.0.1", int(record["url"].rsplit(":", 1)[1])), timeout=2)
        self.addCleanup(client.close)
        self.process.stdin.close()
        self.assertEqual(0, self.process.wait(timeout=5), self.process.stderr.read().decode(errors="replace"))
        self.assertFalse(self.metadata.exists())
        self.assertFalse(windows.pid_is_running(int((self.profile / "real-core-pid").read_text())))
        self.assertEqual(b"", client.recv(1))

    def test_child_failure_exits_while_stdin_remains_open(self):
        self.start()
        self.send({"id": 1, "method": "crash"})
        self.assertNotEqual(0, self.process.wait(timeout=5))
        self.assertFalse(self.process.stdin.closed)
        self.assertFalse(self.metadata.exists())

    def test_stdin_eof_during_ping_and_notification_stream_is_normal_exit(self):
        self.start()
        self.send({"id": 1, "method": "stream"})
        self.assertEqual("fixture/stream", self.response()["method"])
        self.process.stdin.close()
        self.assertEqual(0, self.process.wait(timeout=5))
        self.assertFalse(self.metadata.exists())

    def test_non_server_passthrough_preserves_environment_args_and_exit(self):
        code = "import json,os,sys; print(json.dumps([os.environ['CODEX_APP_TOOLS_PIPE_PATH'],sys.argv[1:]]));sys.exit(7)"
        completed = subprocess.run(self.command + ["-c", code, "literal & %PATH% 中文", ""], cwd=self.root, env=self.env, capture_output=True, timeout=5)
        self.assertEqual(7, completed.returncode)
        self.assertEqual(["fresh-test-desktop-pipe", ["literal & %PATH% 中文", ""]], json.loads(completed.stdout))
        self.assertFalse(self.metadata.parent.exists())


if __name__ == "__main__":
    unittest.main()
