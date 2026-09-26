"""Explicit Windows bridge discovery and real local WebSocket client checks."""
from __future__ import annotations

import base64
import hashlib
import io
import json
import os
from pathlib import Path
import socket
import struct
import tempfile
import threading
import unittest
from unittest import mock

from long_task_callback import cli, windows_service
from long_task_callback.desktop_connection import BridgeEndpoint, load_bridge_endpoint
from long_task_callback.platforms import windows


@unittest.skipUnless(os.name == "nt", "native Windows bridge peer verification")
class BridgeClientTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="ltc-bridge-client-")
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.profile = self.directory / "profile"
        self.metadata = self.directory / "bridge.json"
        self.identity = windows.process_identity(os.getpid())
        self.assertIsNotNone(self.identity)
        self.record = {"version": 1, "url": "ws://127.0.0.1:49152", "pid": os.getpid(),
                       "identity": self.identity, "codex_home": str(self.profile)}

    def write_metadata(self, **changes):
        self.metadata.write_text(json.dumps(dict(self.record, **changes)), encoding="utf-8")

    def test_metadata_requires_live_same_profile_identity(self):
        self.write_metadata()
        endpoint = load_bridge_endpoint(self.metadata, self.profile)
        self.assertEqual(endpoint.url, self.record["url"])
        self.assertEqual(endpoint.pid, os.getpid())
        for changes in ({"pid": True}, {"identity": dict(self.identity, creation_time=0)},
                        {"identity": dict(self.identity, sid="other-user")},
                        {"codex_home": str(self.directory / "other-profile")}, {"version": 2}):
            with self.subTest(changes=changes):
                self.write_metadata(**changes)
                with self.assertRaises(ValueError):
                    load_bridge_endpoint(self.metadata, self.profile)

    def test_metadata_rejects_ambiguous_or_remote_endpoints(self):
        for url in ("ws://localhost:1", "ws://127.0.0.2:2", "ws://0.0.0.0:3",
                    "ws://[::1]:4", "ws://user:secret@127.0.0.1:5", "ws://127.0.0.1:0",
                    "ws://127.0.0.1:65536", "ws://127.0.0.1:12/", "ws://127.0.0.1:12?q=x",
                    "ws://127.0.0.1:12#fragment", "ws://127.0.0.1:12\r\nX: y"):
            with self.subTest(url=url):
                self.write_metadata(url=url)
                with self.assertRaises(ValueError):
                    load_bridge_endpoint(self.metadata, self.profile)

    def test_metadata_retries_crt_sharing_denial_without_winerror(self):
        self.write_metadata()
        saved = self.metadata.read_text(encoding="utf-8")
        with mock.patch.object(Path, "read_text", side_effect=[PermissionError(13, "Sharing conflict"), saved]):
            endpoint = load_bridge_endpoint(self.metadata, self.profile)
        self.assertEqual(endpoint.pid, os.getpid())

    def test_windows_requires_explicit_bridge_and_pins_it_for_service(self):
        request = {"target": {"kind": "session", "value": "original-session"}}
        with mock.patch.dict(os.environ, {cli.DESKTOP_APP_SERVER_ENV: "1"}, clear=True):
            self.assertIsNone(cli.desktop_app_server_socket(request))
        self.write_metadata()
        with mock.patch.dict(os.environ, {cli.DESKTOP_APP_SERVER_ENV: "1",
                                         cli.APP_SERVER_BRIDGE_FILE_ENV: str(self.metadata)}, clear=True), \
                mock.patch.object(cli, "codex_home", return_value=self.profile):
            self.assertIsInstance(cli.desktop_app_server_socket(request), BridgeEndpoint)
            with mock.patch.object(cli, "queue_dir", return_value=self.directory / "queue"), \
                    mock.patch.object(cli, "daemon_command", return_value=["python.exe", "daemon"]), \
                    mock.patch.object(cli, "claude_home", return_value=self.directory / "claude"), \
                    mock.patch.object(cli, "daemon_environment", return_value={}):
                from argparse import Namespace
                config = windows_service.configuration(Namespace(name="test", restart_sec=2))
            self.assertEqual(config["environment"][cli.APP_SERVER_BRIDGE_FILE_ENV], str(self.metadata))

    def listener(self):
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        server.settimeout(3)
        self.addCleanup(server.close)
        endpoint = BridgeEndpoint(server.getsockname()[1], os.getpid(), self.identity)
        return server, endpoint

    def test_wrong_server_identity_sends_no_http_or_prompt(self):
        server, endpoint = self.listener()
        received = []
        errors = []
        def serve():
            try:
                peer, _ = server.accept()
                with peer:
                    peer.settimeout(2)
                    received.append(peer.recv(4096))
            except BaseException as error:
                errors.append(error)
        thread = threading.Thread(target=serve)
        thread.start()
        wrong = BridgeEndpoint(endpoint.port, endpoint.pid, dict(endpoint.identity, creation_time=0))
        client = cli.AppServerConnection(wrong, 2)
        with self.assertRaises(OSError):
            client.connect()
        thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(received, [b""])

    def test_real_tcp_websocket_rpc_and_ping_pong(self):
        server, endpoint = self.listener()
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
            self.assertTrue(second & 128)
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
                    peer.sendall(b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: " + accepted + b"\r\n\r\n")
                    for expected in ("initialize", "initialized", "thread/resume", "turn/start"):
                        opcode, value = frame(peer)
                        self.assertEqual(opcode, 1)
                        message = json.loads(value)
                        methods.append(message["method"])
                        self.assertEqual(message["method"], expected)
                        if "id" in message:
                            if expected == "initialize":
                                peer.sendall(b"\x8a\x00\x89\x01p")
                                self.assertEqual(frame(peer), (10, b"p"))
                            send(peer, {"id": message["id"], "result": {"turn": {"id": "turn-1"}}})
                    send(peer, {"method": "turn/completed", "params": {"threadId": "original-session", "turn": {"id": "turn-1"}}})
            except BaseException as error:
                errors.append(error)
        thread = threading.Thread(target=serve)
        thread.start()
        payload = {"request": {"target": {"kind": "session", "value": "original-session"},
                               "sandbox_mode": "workspace-write", "queue_dir": str(self.directory / "queue")},
                   "cwd": str(self.directory), "prompt": "fixture callback", "timeout": 2}
        with mock.patch.object(cli, "desktop_app_server_socket", return_value=endpoint):
            delivery = cli.start_desktop_app_server_turn(payload)
        self.assertIsNotNone(delivery)
        try:
            self.assertTrue(delivery.wait_for_completion(2))
        finally:
            delivery.close()
        thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(methods, ["initialize", "initialized", "thread/resume", "turn/start"])

    def test_explicit_bridge_unavailability_never_falls_back_to_cli(self):
        payload = {"request": {"target": {"kind": "session", "value": "original-session"},
                               "sandbox_mode": "workspace-write", "queue_dir": str(self.directory / "queue")},
                   "cwd": str(self.directory), "prompt": "fixture callback", "timeout": 1}
        endpoint = BridgeEndpoint(49152, os.getpid(), self.identity)
        for error in (OSError("unavailable"), cli.AppServerRpcError("writer conflict")):
            with self.subTest(error=error), mock.patch.object(cli, "desktop_app_server_socket", return_value=endpoint), \
                    mock.patch.object(cli.AppServerConnection, "connect", side_effect=error):
                with self.assertRaisesRegex(cli.AppServerProtocolError, "CLI fallback disabled"):
                    cli.start_desktop_app_server_turn(payload)


if __name__ == "__main__":
    unittest.main()
