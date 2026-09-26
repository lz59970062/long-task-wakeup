import contextlib
import ctypes
import json
import os
import socket
import struct
import subprocess
import sys
import unittest
from unittest import mock

from long_task_callback.platforms import windows, windows_tcp


class WindowsTcpContracts(unittest.TestCase):
    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(mock.patch.object(sys, "platform", "win32"))
        self.guard = self.stack.enter_context(mock.patch.object(
            windows_tcp, "_process_guard", side_effect=lambda pid: contextlib.nullcontext()))
        self.connection = mock.Mock(family=socket.AF_INET, proto=socket.IPPROTO_TCP)
        self.connection.getsockopt.return_value = socket.SOCK_STREAM
        self.connection.fileno.return_value = 10
        self.connection.getsockname.return_value = ("127.0.0.1", 12345)
        self.connection.getpeername.return_value = ("127.0.0.1", 54321)
        self.own = {"boot_id": "boot", "machine_id": "machine", "sid": "S-1-5-21-1", "creation_time": 10}
        self.peer = dict(self.own, creation_time=20)
        self.rows = self.stack.enter_context(mock.patch.object(windows_tcp, "_read_tcp_rows", return_value=[
            ("127.0.0.1", 12345, "127.0.0.1", 54321, os.getpid(), 5),
            ("127.0.0.1", 54321, "127.0.0.1", 12345, 123456, 5),
            ("127.0.0.1", 54321, "0.0.0.0", 0, 999999, 2),
        ]))
        self.identities = self.stack.enter_context(mock.patch.object(windows, "process_identity",
            side_effect=lambda pid: dict(self.own if pid == os.getpid() else self.peer)))

    def assert_rejected(self, **kwargs):
        with self.assertRaises(windows_tcp.PeerVerificationError):
            windows_tcp.verify_loopback_peer(self.connection, **kwargs)
        self.connection.close.assert_called_once()

    def test_checks_reverse_established_connection_and_complete_identity(self):
        result = windows_tcp.verify_loopback_peer(self.connection, self.peer, expected_pid=123456)
        self.assertEqual(result, {"pid": 123456, **self.peer})
        self.assertEqual(self.rows.call_count, 2)
        self.guard.assert_called_once_with(123456)
        self.connection.close.assert_not_called()
        self.connection.send.assert_not_called()
        self.assertEqual(windows_tcp.verify_loopback_peer(self.connection, result), result)

    def test_refuses_other_user_or_unknown_process_identity(self):
        for identity in (None, {}, dict(self.peer, sid="S-1-5-21-2"), dict(self.peer, sid=""),
                         dict(self.peer, machine_id="other"), dict(self.peer, boot_id="other"),
                         dict(self.peer, creation_time=0), dict(self.peer, creation_time=True)):
            with self.subTest(identity=identity):
                self.connection.close.reset_mock()
                self.identities.side_effect = lambda pid: self.own if pid == os.getpid() else identity
                self.assert_rejected()

    def test_refuses_wrong_or_partial_expected_identity_and_pid(self):
        for arguments in (
            {"expected_identity": dict(self.peer, creation_time=21)},
            {"expected_identity": {"sid": self.peer["sid"]}},
            {"expected_identity": dict(self.peer, extra="unrecognized")},
            {"expected_identity": dict(self.peer, pid=123456), "expected_pid": 1},
            {"expected_identity": dict(self.peer, pid=True)},
            {"expected_pid": 456789}, {"expected_pid": True}, {"expected_pid": -1},
        ):
            with self.subTest(arguments=arguments):
                self.connection.close.reset_mock()
                self.assert_rejected(**arguments)

    def test_refuses_external_ipv6_udp_and_alternate_loopback_addresses(self):
        for endpoint in (("192.0.2.1", 54321), ("127.0.0.2", 54321), ("localhost", 54321),
                         ("::1", 54321, 0, 0), ("127.0.0.1", 0)):
            for side in (self.connection.getsockname, self.connection.getpeername):
                with self.subTest(endpoint=endpoint, side=side):
                    self.connection.close.reset_mock()
                    side.return_value = endpoint
                    self.assert_rejected()
                    self.connection.getsockname.return_value = ("127.0.0.1", 12345)
                    self.connection.getpeername.return_value = ("127.0.0.1", 54321)
        for family, kind, proto in ((socket.AF_INET6, socket.SOCK_STREAM, 0),
                                    (socket.AF_INET, socket.SOCK_DGRAM, 0),
                                    (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_UDP)):
            with self.subTest(family=family, kind=kind, proto=proto):
                self.connection.close.reset_mock()
                self.connection.family, self.connection.proto = family, proto
                self.connection.getsockopt.return_value = kind
                self.assert_rejected()
        self.rows.assert_not_called()

    def test_refuses_unowned_ambiguous_dead_or_changing_connections(self):
        peer_row = ("127.0.0.1", 54321, "127.0.0.1", 12345, 123456, 5)
        for rows in ([], [peer_row, peer_row], [(*peer_row[:4], 0, 5)],
                     [(*peer_row[:5], 8)], [(*peer_row[:3], 11111, 123456, 5)]):
            with self.subTest(rows=rows):
                self.connection.close.reset_mock()
                self.rows.return_value = rows
                self.assert_rejected()
        self.connection.close.reset_mock()
        self.rows.side_effect = [[peer_row], [(*peer_row[:4], 123457, 5)]]
        self.assert_rejected()

    def test_refuses_pid_reuse_and_identity_changes(self):
        for identities in (
            [self.own, self.peer, dict(self.peer, creation_time=21)],
            [self.own, self.peer, self.peer, dict(self.own, sid="changed")],
        ):
            with self.subTest(identities=identities):
                self.connection.close.reset_mock()
                self.identities.side_effect = identities
                self.assert_rejected()

    def test_refuses_native_error_changed_socket_and_unsupported_platform(self):
        with mock.patch.object(windows_tcp, "_read_tcp_rows", side_effect=OSError("denied")):
            self.assert_rejected()
        self.connection.close.reset_mock()
        self.connection.getpeername.side_effect = [("127.0.0.1", 54321), ("127.0.0.1", 54322)]
        self.assert_rejected()
        self.connection.close.reset_mock()
        with mock.patch.object(sys, "platform", "linux"):
            self.assert_rejected()


class WindowsTcpNativeTableContracts(unittest.TestCase):
    def table_api(self, table, *, status=0):
        def read(buffer, size_pointer, ordered, family, table_class, reserved):
            self.assertEqual((ordered, family, table_class, reserved), (False, socket.AF_INET, 5, 0))
            size = ctypes.cast(size_pointer, ctypes.POINTER(ctypes.c_uint32))
            size.contents.value = len(table)
            if buffer is None:
                return 122
            if not status:
                ctypes.memmove(buffer, table, len(table))
            return status
        return read

    def test_native_row_network_byte_order(self):
        address = struct.unpack("=I", socket.inet_aton("127.0.0.1"))[0]
        table = struct.pack("=7I", 1, 5, address, socket.htons(12345),
                            address, socket.htons(54321), 123456)
        with mock.patch.object(windows_tcp, "_get_extended_tcp_table", return_value=self.table_api(table)):
            self.assertEqual(windows_tcp._read_tcp_rows(), [("127.0.0.1", 12345, "127.0.0.1", 54321, 123456, 5)])

    def test_incomplete_native_table_and_api_failures_are_rejected(self):
        table = struct.pack("=I", 1)
        for api in (self.table_api(table), self.table_api(table, status=5),
                    self.table_api(b"x"), self.table_api(table, status=122)):
            with self.subTest(api=api), mock.patch.object(windows_tcp, "_get_extended_tcp_table", return_value=api):
                with self.assertRaises(windows_tcp.PeerVerificationError):
                    windows_tcp._read_tcp_rows()


@unittest.skipUnless(sys.platform == "win32", "requires native Windows TCP and process APIs")
class WindowsTcpNativeTests(unittest.TestCase):
    def connected_pair(self):
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.addCleanup(listener.close)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        listener.settimeout(5)
        client = socket.create_connection(listener.getsockname(), timeout=5)
        self.addCleanup(client.close)
        server, _address = listener.accept()
        self.addCleanup(server.close)
        return client, server

    def test_real_client_and_accepted_socket_same_user(self):
        identity = windows.process_identity(os.getpid())
        self.assertIsNotNone(identity)
        client, server = self.connected_pair()
        for connection in (client, server):
            with self.subTest(connection=connection):
                result = windows_tcp.verify_loopback_peer(connection, identity, expected_pid=os.getpid())
                self.assertEqual(result, {"pid": os.getpid(), **identity})
        client.sendall(b"verified")
        self.assertEqual(server.recv(8), b"verified")

    def test_real_socket_wrong_identity_is_closed(self):
        client, _server = self.connected_pair()
        identity = windows.process_identity(os.getpid())
        self.assertIsNotNone(identity)
        identity["creation_time"] += 1
        with self.assertRaises(windows_tcp.PeerVerificationError):
            windows_tcp.verify_loopback_peer(client, identity)
        self.assertEqual(client.fileno(), -1)

    def test_real_separate_process_owns_reverse_endpoint(self):
        program = (
            "import json,os,socket,sys\n"
            "with socket.socket(socket.AF_INET,socket.SOCK_STREAM) as listener:\n"
            " listener.bind(('127.0.0.1',0));listener.listen(1);listener.settimeout(10)\n"
            " print(json.dumps({'pid':os.getpid(),'port':listener.getsockname()[1]}),flush=True)\n"
            " connection,_=listener.accept()\n"
            " with connection:\n"
            "  connection.settimeout(10)\n"
            "  data=connection.recv(8);connection.sendall(data)\n"
            "  sys.stdin.readline()\n"
        )
        child = subprocess.Popen([sys.executable, "-u", "-c", program], stdin=subprocess.PIPE,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            info = json.loads(child.stdout.readline())
            self.assertNotEqual(info["pid"], os.getpid())
            with socket.create_connection(("127.0.0.1", info["port"]), timeout=5) as connection:
                identity = windows.process_identity(info["pid"])
                result = windows_tcp.verify_loopback_peer(connection, identity, expected_pid=info["pid"])
                self.assertEqual(result, {"pid": info["pid"], **identity})
                connection.sendall(b"verified")
                self.assertEqual(connection.recv(8), b"verified")
            _stdout, stderr = child.communicate("done\n", timeout=5)
            self.assertEqual(child.returncode, 0, stderr)
        finally:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=5)
            for stream in (child.stdin, child.stdout, child.stderr):
                stream.close()


if __name__ == "__main__":
    unittest.main()
