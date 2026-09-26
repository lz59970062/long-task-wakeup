from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock
from urllib.parse import urlsplit

from long_task_callback import desktop_bridge as bridge
from long_task_callback.platforms import windows


KEY = base64.b64encode(b"0123456789abcdef").decode("ascii")


def request(*extra: str, first: str = "GET / HTTP/1.1") -> bytes:
    return ("\r\n".join([first, "Host: ignored.example", "Connection: keep-alive, Upgrade", "Upgrade: WebSocket",
                         "Sec-WebSocket-Version: 13", f"Sec-WebSocket-Key: {KEY}", *extra]) + "\r\n\r\n").encode("ascii")


def identity(created=10, sid="S-user"):
    return {"sid": sid, "boot_id": "boot", "machine_id": "machine", "creation_time": created}


class HandshakeTests(unittest.TestCase):
    def test_request_only_forwards_websocket_fields_and_injects_private_token(self):
        value, key = bridge.upstream_request(request("X-Forwarded-For: attacker", "Sec-WebSocket-Protocol: json"), 456, "private")
        self.assertEqual(KEY, key)
        self.assertIn(b"Host: 127.0.0.1:456\r\n", value)
        self.assertIn(b"Authorization: Bearer private\r\n", value)
        self.assertIn(b"sec-websocket-protocol: json\r\n", value)
        self.assertNotIn(b"attacker", value)
        self.assertNotIn(b"ignored.example", value)

    def test_rejects_origin_credentials_bodies_duplicates_and_folded_headers(self):
        bad = ["Origin: null", "oRiGiN: https://evil.test", "Authorization: Bearer untrusted", "Proxy-Authorization: Basic x",
               "Content-Length: 0", "Transfer-Encoding: chunked", "Host: duplicate", " folded", "X-Test: hi\tthere", "Bad Name: x"]
        for field in bad:
            with self.subTest(field=field), self.assertRaises(bridge.BridgeError):
                bridge.upstream_request(request(field), 1, "secret")

    def test_only_root_relative_upgrade_with_strict_key(self):
        for first in ("GET /other HTTP/1.1", "GET ws://127.0.0.1/ HTTP/1.1", "POST / HTTP/1.1", "GET /?x HTTP/1.1"):
            with self.subTest(first=first), self.assertRaises(bridge.BridgeError):
                bridge.upstream_request(request(first=first), 1, "secret")
        with self.assertRaises(bridge.BridgeError):
            bridge.upstream_request(request().replace(KEY.encode(), b"bad"), 1, "secret")

    def test_header_limit_and_slow_handshake_deadline(self):
        client, server = socket.socketpair()
        try:
            worker = threading.Thread(target=lambda: client.sendall(b"A" * bridge.HEADER_LIMIT), daemon=True)
            worker.start()
            with self.assertRaisesRegex(bridge.BridgeError, "too large"):
                bridge.read_http_header(server, 0.3)
            worker.join(1)
        finally:
            client.close()
            server.close()
        client, server = socket.socketpair()
        try:
            with self.assertRaisesRegex(bridge.BridgeError, "timed out"):
                bridge.read_http_header(server, 0.03)
        finally:
            client.close()
            server.close()

    def test_response_requires_valid_accept_and_upgrade(self):
        accept = base64.b64encode(hashlib.sha1((KEY + bridge._GUID).encode()).digest()).decode()
        response = f"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: {accept}\r\n\r\n".encode()
        bridge.validate_upgrade(response, KEY)
        for bad in (response.replace(b"101", b"401"), response.replace(accept.encode(), b"forged")):
            with self.assertRaises(bridge.BridgeError):
                bridge.validate_upgrade(bad, KEY)

    def test_upstream_tree_requires_live_matching_identity_and_ordered_ancestors(self):
        records = {1: identity(10), 2: identity(20), 3: identity(30)}
        with mock.patch.object(bridge, "_parent_snapshot", return_value={3: 2, 2: 1}), mock.patch.object(windows, "process_identity", side_effect=records.get):
            bridge.verify_process_tree({"pid": 3, **records[3]}, 1, records[1])
            with self.assertRaises(bridge.BridgeError):
                bridge.verify_process_tree({"pid": 3, **records[3]}, 1, identity(11))
            records[2] = identity(40)
            with self.assertRaises(bridge.BridgeError):
                bridge.verify_process_tree({"pid": 3, **records[3]}, 1, records[1])
            records[2] = identity(20, sid="foreign")
            with self.assertRaises(bridge.BridgeError):
                bridge.verify_process_tree({"pid": 3, **records[3]}, 1, records[1])

    def test_credentials_are_not_sent_to_unverified_upstream(self):
        instance = bridge.DesktopBridge(Path("profile"), "unused", Path("metadata"))
        instance.upstream = mock.Mock(pid=22)
        instance.upstream_identity = identity()
        fake = mock.Mock()
        with mock.patch.object(socket, "create_connection", return_value=fake), mock.patch.object(bridge, "_verify_peer", side_effect=OSError("foreign peer")):
            with self.assertRaises(OSError):
                instance._upgrade(b"Authorization: secret", KEY)
        fake.sendall.assert_not_called()
        fake.close.assert_called_once()

    def test_unrelated_same_user_upstream_gets_no_credentials(self):
        instance = bridge.DesktopBridge(Path("profile"), "unused", Path("metadata"))
        instance.upstream = mock.Mock(pid=22)
        instance.upstream_identity = identity()
        fake = mock.Mock()
        with mock.patch.object(socket, "create_connection", return_value=fake), mock.patch.object(bridge, "_verify_peer", return_value={"pid": 33, **identity()}), mock.patch.object(bridge, "verify_process_tree", side_effect=bridge.BridgeError("unrelated")):
            with self.assertRaises(bridge.BridgeError):
                instance._upgrade(b"Authorization: secret", KEY)
        fake.sendall.assert_not_called()
        fake.close.assert_called_once()

    def test_non_finite_timeouts_fail_before_state_creation(self):
        for value in (0, -1, float("nan"), float("inf")):
            instance = bridge.DesktopBridge(Path("profile"), "unused", Path("metadata"), startup_timeout=value)
            with mock.patch.object(bridge.windows_io, "require_windows"), mock.patch.object(bridge.windows_io, "acquire_path_lock") as lock:
                with self.assertRaisesRegex(bridge.BridgeError, "finite and positive"):
                    instance.start()
            lock.assert_not_called()

    def test_metadata_sharing_conflicts_retry_without_weakening_other_errors(self):
        conflict = PermissionError("temporarily shared")
        conflict.winerror = 32
        instance = bridge.DesktopBridge(Path("profile"), "unused", Path("metadata"))
        with mock.patch.object(bridge, "write_request", side_effect=[conflict, None]) as write:
            instance._publish()
        self.assertEqual(2, write.call_count)
        with mock.patch.object(Path, "read_text", side_effect=[conflict, '{"version": 1}']) as read:
            self.assertEqual({"version": 1}, bridge._read_record(Path("metadata")))
        self.assertEqual(2, read.call_count)
        with mock.patch.object(Path, "read_text", side_effect=[PermissionError(13, "CRT sharing conflict"), '{"version": 1}']) as read:
            self.assertEqual({"version": 1}, bridge._read_record(Path("metadata")))
        self.assertEqual(2, read.call_count)
        with mock.patch.object(bridge, "write_request", side_effect=OSError("disk failure")):
            with self.assertRaisesRegex(OSError, "disk failure"):
                instance._publish()

    def test_cleanup_still_removes_token_and_releases_lock_if_metadata_is_invalid(self):
        instance = bridge.DesktopBridge(Path("profile"), "unused", Path("metadata"))
        instance._published = True
        instance._lock = (mock.Mock(), Path("lock"))
        handle = instance._lock[0]
        with mock.patch.object(bridge, "_read_record", side_effect=bridge.BridgeError("invalid metadata")), mock.patch.object(bridge, "_remove_file") as remove:
            with self.assertRaisesRegex(bridge.BridgeError, "invalid metadata"):
                instance.close()
        remove.assert_called_once_with(instance.token_file)
        handle.close.assert_called_once()

    def test_failed_job_drain_keeps_ownership_and_never_announces_stopped(self):
        instance = bridge.DesktopBridge(Path("profile"), "unused", Path("metadata"))
        instance._job_handle = 42
        instance._published = True
        instance._lock = (mock.Mock(), Path("lock"))
        instance._log = mock.Mock()
        instance._stop_response = (Path("stop-response"), "a" * 32)
        retained = []
        with mock.patch.object(bridge, "_FAILED_BRIDGES", retained), mock.patch.object(bridge, "terminate_worker_children", side_effect=TimeoutError("still exiting")), mock.patch.object(bridge, "_remove_file") as remove, mock.patch.object(bridge, "write_request") as write:
            with self.assertRaises(TimeoutError):
                instance.close()
            with self.assertRaisesRegex(bridge.BridgeError, "previously failed"):
                instance.close()
        self.assertEqual([instance], retained)
        instance._lock[0].close.assert_not_called()
        instance._log.close.assert_not_called()
        remove.assert_not_called()
        write.assert_not_called()


FAKE_SERVER = r'''
import base64, hashlib, json, os, pathlib, socket, sys, threading
args = sys.argv[1:]
home = pathlib.Path(os.environ["CODEX_HOME"])
port = int(args[args.index("--listen") + 1].rsplit(":", 1)[1])
token = pathlib.Path(args[args.index("--ws-token-file") + 1]).read_text().strip()
if (home / "fail-startup").exists():
    sys.exit(7)
server = socket.socket()
server.bind(("127.0.0.1", port))
server.listen(10)
(home / "server-pid").write_text(str(os.getpid()))
def handle(conn):
    try:
        data = b""
        while b"\r\n\r\n" not in data:
            part = conn.recv(4096)
            if not part: return
            data += part
        fields = dict(line.split(": ", 1) for line in data.decode().split("\r\n")[1:] if ": " in line)
        if fields.get("Authorization") != "Bearer " + token:
            conn.sendall(b"HTTP/1.1 401 Unauthorized\r\nContent-Length: 0\r\n\r\n")
            return
        (home / "auth-count").open("a").write("1\n")
        key = fields["Sec-WebSocket-Key"]
        accept = base64.b64encode(hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()).decode()
        conn.sendall(("HTTP/1.1 101 Switching Protocols\r\nConnection: Upgrade\r\nUpgrade: websocket\r\nSec-WebSocket-Accept: " + accept + "\r\n\r\n").encode())
        while True:
            data = conn.recv(65536)
            if not data: return
            conn.sendall(data)
    except OSError:
        pass
    finally:
        conn.close()
while True:
    conn, _ = server.accept()
    threading.Thread(target=handle, args=(conn,), daemon=True).start()
'''


@unittest.skipUnless(os.name == "nt", "native Windows gateway identity and Job tests")
class NativeBridgeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="ltc-bridge-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.profile = self.root / "profile 中文"
        self.profile.mkdir()
        self.metadata = self.profile / "long-task-wakeup" / "desktop-bridge.json"
        fixture = self.root / "fake server.py"
        fixture.write_text(FAKE_SERVER, encoding="utf-8")
        self.shim = self.root / "fake codex.cmd"
        self.shim.write_text(f'@echo off\n"{sys.executable}" "{fixture}" %*\n', encoding="utf-8")
        self.env = os.environ.copy()
        # Exercise redirected legacy-code-page output with a Unicode profile.
        self.env["PYTHONIOENCODING"] = "ascii"
        self.env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
        self.command = [sys.executable, "-m", "long_task_callback.desktop_bridge", "--codex-home", str(self.profile),
                        "--codex-bin", str(self.shim), "--startup-timeout", "3", "--handshake-timeout", "0.4"]
        self.processes = []
        self.addCleanup(self.stop_processes)

    def stop_processes(self):
        for process in self.processes:
            if process.poll() is None:
                process.kill()
            process.communicate(timeout=5)

    def start(self):
        process = subprocess.Popen(self.command, env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.processes.append(process)
        deadline = time.monotonic() + 6
        while time.monotonic() < deadline:
            if self.metadata.exists():
                return process, bridge._read_record(self.metadata)
            if process.poll() is not None:
                output, error = process.communicate()
                self.fail(f"Bridge failed: {output!r} {error!r}")
            time.sleep(0.025)
        self.fail("Bridge did not publish metadata")

    def connect(self, record):
        client = socket.create_connection(("127.0.0.1", urlsplit(record["url"]).port), timeout=2)
        self.addCleanup(client.close)
        return client

    def wait_idle(self):
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            if bridge._read_record(self.metadata)["active_clients"] == 0:
                return
            time.sleep(0.025)
        self.fail("Bridge did not drain the closed connection")

    def test_native_authenticated_forwarding_singleton_and_safe_stop(self):
        process, record = self.start()
        self.assertTrue(bridge.bridge_status(self.metadata)["running"])
        self.assertEqual(str(self.profile.resolve()), record["codex_home"])
        self.assertNotIn("token", json.dumps(record))
        second = subprocess.run(self.command, env=self.env, capture_output=True, timeout=5)
        self.assertNotEqual(second.returncode, 0)
        self.assertEqual(record["identity"], bridge._read_record(self.metadata)["identity"])
        client = self.connect(record)
        client.sendall(request())
        response, _ = bridge.read_http_header(client, 2)
        bridge.validate_upgrade(response, KEY)
        client.sendall(b"opaque websocket bytes\x00\xff")
        self.assertEqual(b"opaque websocket bytes\x00\xff", client.recv(4096))
        with self.assertRaisesRegex(bridge.BridgeError, "active clients"):
            bridge.stop_bridge(self.metadata, timeout=1)
        # Also bypass the stop client's early check to test the server's own
        # synchronized refusal; stale metadata must never authorize a stop.
        nonce = "a" * 32
        bridge.write_request(self.metadata.with_name(self.metadata.name + ".stop.json"),
                             {"nonce": nonce, "pid": record["pid"], "identity": record["identity"]})
        reply_file = self.metadata.with_name(self.metadata.name + f".stop-{nonce}.json")
        deadline = time.monotonic() + 2
        reply = None
        while time.monotonic() < deadline and reply is None:
            reply = bridge._read_record(reply_file)
            time.sleep(0.025)
        self.assertEqual({"nonce": nonce, "stopped": False}, reply)
        reply_file.unlink()
        self.assertIsNone(process.poll())
        client.close()
        self.wait_idle()
        stopped = bridge.stop_bridge(self.metadata)
        self.assertTrue(stopped["stopped"])
        output, error = process.communicate(timeout=5)
        self.assertEqual(0, process.returncode, error)
        self.assertFalse(self.metadata.exists())
        self.assertFalse(list(self.metadata.parent.glob("*.token")))
        self.assertNotIn(b"Bearer", output + error)
        self.assertFalse(windows.pid_is_running(int((self.profile / "server-pid").read_text())))

    def test_native_rejected_client_headers_never_open_upstream(self):
        process, record = self.start()
        original = (self.profile / "auth-count").read_text().count("1")
        for field in ("Origin: null", "Authorization: Bearer attack", "Content-Length: 1"):
            client = self.connect(record)
            client.sendall(request(field))
            response, _ = bridge.read_http_header(client, 2)
            self.assertTrue(response.startswith(b"HTTP/1.1 403"))
            client.close()
            self.wait_idle()
        self.assertEqual(original, (self.profile / "auth-count").read_text().count("1"))
        bridge.stop_bridge(self.metadata)
        process.communicate(timeout=5)

    def test_startup_failure_rolls_back_and_status_creates_nothing(self):
        absent = self.root / "absent" / "record.json"
        self.assertFalse(bridge.bridge_status(absent)["running"])
        self.assertFalse(absent.parent.exists())
        (self.profile / "fail-startup").write_text("fail")
        completed = subprocess.run(self.command, env=self.env, capture_output=True, timeout=6)
        self.assertNotEqual(0, completed.returncode)
        self.assertFalse(self.metadata.exists())
        self.assertFalse(list(self.metadata.parent.glob("*.token")))
        self.assertFalse(bridge.windows_io.probe_existing_lock(self.metadata.with_name(self.metadata.name + ".lock")))

    def test_bridge_crash_kills_upstream_and_stale_metadata_can_restart(self):
        process, record = self.start()
        child_pid = int((self.profile / "server-pid").read_text())
        child_identity = windows.process_identity(child_pid)
        process.kill()
        process.communicate(timeout=5)
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and windows.process_identity(child_pid) == child_identity:
            time.sleep(0.025)
        self.assertFalse(windows.pid_is_running(child_pid))
        self.assertFalse(bridge.bridge_status(self.metadata)["running"])
        process2 = subprocess.Popen(self.command, env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.processes.append(process2)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            newer = bridge._read_record(self.metadata)
            if newer["identity"] != record["identity"]:
                break
            if process2.poll() is not None:
                self.fail(f"Restart failed: {process2.communicate()!r}")
            time.sleep(0.025)
        else:
            self.fail("Restart did not replace stale metadata")
        self.assertTrue(bridge.bridge_status(self.metadata)["running"])
        bridge.stop_bridge(self.metadata)
        process2.communicate(timeout=5)


if __name__ == "__main__":
    unittest.main()
