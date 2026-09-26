"""Fail-closed Windows TCP bind-owner checks for private loopback transports.

An address being loopback is not authentication. Verify an already connected
socket before reading application data or forwarding credentials. These checks
identify the Windows TCP context-bind owner at the time of verification;
Windows sockets can subsequently be duplicated or delegated by that owner.
Callers must retain the verified socket and, when reconnecting to a particular
server, pin its PID and full process identity again.

Native API reference:
https://learn.microsoft.com/windows/win32/api/iphlpapi/nf-iphlpapi-getextendedtcptable
https://learn.microsoft.com/windows/win32/api/tcpmib/ns-tcpmib-mib_tcprow_owner_pid
"""
from __future__ import annotations

from collections.abc import Mapping
from contextlib import contextmanager
import ctypes
import os
import socket
import struct
import sys

from . import windows


_IDENTITY_FIELDS = frozenset({"boot_id", "machine_id", "sid", "creation_time"})
_TCP_ESTABLISHED = 5
_TCP_TABLE_OWNER_PID_ALL = 5
_ERROR_INSUFFICIENT_BUFFER = 122
_MAX_TABLE_BYTES = 16 * 1024 * 1024


class PeerVerificationError(OSError):
    """The socket's same-user process ownership could not be established."""


class _TcpRow(ctypes.Structure):
    _fields_ = [(name, ctypes.c_uint32) for name in (
        "state", "local_address", "local_port", "remote_address", "remote_port", "pid"
    )]


class _TcpTable(ctypes.Structure):
    _fields_ = [("count", ctypes.c_uint32), ("rows", _TcpRow * 1)]


def _get_extended_tcp_table():
    if sys.platform != "win32":
        raise PeerVerificationError("Windows TCP ownership queries require Windows")
    api = ctypes.WinDLL("iphlpapi", use_last_error=True).GetExtendedTcpTable
    api.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32),
                    ctypes.c_int32, ctypes.c_uint32, ctypes.c_int32, ctypes.c_uint32]
    api.restype = ctypes.c_uint32
    return api


def _read_tcp_rows() -> list[tuple[str, int, str, int, int, int]]:
    """Return (local address/port, remote address/port, PID, state) rows."""
    api = _get_extended_tcp_table()
    size = ctypes.c_uint32()
    status = api(None, ctypes.byref(size), False, socket.AF_INET,
                 _TCP_TABLE_OWNER_PID_ALL, 0)
    if status != _ERROR_INSUFFICIENT_BUFFER:
        raise PeerVerificationError(f"Windows TCP table size query failed ({status})")
    # The table may grow between size and data calls; never use partial data.
    for _attempt in range(4):
        capacity = size.value
        if not _TcpTable.rows.offset <= capacity <= _MAX_TABLE_BYTES:
            raise PeerVerificationError("Windows TCP table has an invalid size")
        buffer = ctypes.create_string_buffer(capacity)
        status = api(buffer, ctypes.byref(size), False, socket.AF_INET,
                     _TCP_TABLE_OWNER_PID_ALL, 0)
        if status == _ERROR_INSUFFICIENT_BUFFER:
            continue
        if status:
            raise PeerVerificationError(f"Windows TCP ownership query failed ({status})")
        if not _TcpTable.rows.offset <= size.value <= capacity:
            raise PeerVerificationError("Windows TCP table has an invalid result size")
        count = ctypes.c_uint32.from_buffer(buffer).value
        offset = _TcpTable.rows.offset
        if count > (size.value - offset) // ctypes.sizeof(_TcpRow):
            raise PeerVerificationError("Windows TCP table is incomplete")
        rows = []
        for index in range(count):
            row = _TcpRow.from_buffer(buffer, offset + index * ctypes.sizeof(_TcpRow))
            rows.append((
                socket.inet_ntoa(struct.pack("=I", row.local_address)),
                socket.ntohs(row.local_port & 0xffff),
                socket.inet_ntoa(struct.pack("=I", row.remote_address)),
                socket.ntohs(row.remote_port & 0xffff), row.pid, row.state,
            ))
        return rows
    raise PeerVerificationError("Windows TCP table kept changing during verification")


def _socket_endpoints(connection: socket.socket) -> tuple[tuple[str, int], tuple[str, int]]:
    if (connection.family != socket.AF_INET
            or connection.getsockopt(socket.SOL_SOCKET, socket.SO_TYPE) != socket.SOCK_STREAM
            or connection.proto not in (0, socket.IPPROTO_TCP)):
        raise PeerVerificationError("Peer verification requires an IPv4 TCP socket")
    local, remote = connection.getsockname(), connection.getpeername()
    for endpoint in (local, remote):
        if (not isinstance(endpoint, tuple) or len(endpoint) != 2
                or endpoint[0] != "127.0.0.1" or type(endpoint[1]) is not int
                or not 0 < endpoint[1] <= 65535):
            raise PeerVerificationError("Peer verification requires both endpoints at 127.0.0.1")
    return local, remote


def _peer_pid(endpoints: tuple[tuple[str, int], tuple[str, int]]) -> int:
    local, remote = endpoints
    # Read the peer's row, not this socket's row or a listening socket's row.
    matches = [row[4] for row in _read_tcp_rows()
               if row[:4] == (remote[0], remote[1], local[0], local[1])
               and row[5] == _TCP_ESTABLISHED]
    if len(matches) != 1 or type(matches[0]) is not int or matches[0] <= 0:
        raise PeerVerificationError("The connected TCP peer does not have one live owner")
    return matches[0]


def _identity(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping) or not _IDENTITY_FIELDS <= value.keys():
        raise PeerVerificationError("TCP peer process identity is unavailable")
    result = {key: value[key] for key in _IDENTITY_FIELDS}
    if (any(not isinstance(result[key], str) or not result[key]
            for key in ("boot_id", "machine_id", "sid"))
            or type(result["creation_time"]) is not int or result["creation_time"] <= 0):
        raise PeerVerificationError("TCP peer process identity is incomplete")
    return result


@contextmanager
def _process_guard(pid: int):
    """Keep a live native process object pinned during both observations."""
    kernel = windows._kernel32()
    handle = kernel.OpenProcess(0x1000 | 0x100000, False, pid)
    if not handle:
        raise PeerVerificationError("Cannot open the TCP peer process")
    try:
        if kernel.WaitForSingleObject(handle, 0) != 258:
            raise PeerVerificationError("TCP peer process has exited")
        yield
        if kernel.WaitForSingleObject(handle, 0) != 258:
            raise PeerVerificationError("TCP peer process exited during verification")
    finally:
        kernel.CloseHandle(handle)


def verify_loopback_peer(connection: socket.socket,
                         expected_identity: Mapping[str, object] | None = None, *,
                         expected_pid: int | None = None) -> dict[str, object]:
    """Verify this connected peer belongs to the current Windows user.

    Return ``{"pid": pid, **windows.process_identity(pid)}``. An optional full
    expected identity pins a known server across PID reuse; it may also contain
    the PID from an earlier verification result. A PID alone is not sufficient
    to pin process identity. On any verification failure the socket is closed
    and ``PeerVerificationError`` is raised. No application data is sent.
    """
    try:
        if sys.platform != "win32":
            raise PeerVerificationError("Windows TCP ownership queries require Windows")
        if expected_pid is not None and (type(expected_pid) is not int or expected_pid <= 0):
            raise PeerVerificationError("Expected TCP peer PID is invalid")
        expected = None
        if expected_identity is not None:
            expected = _identity(expected_identity)
            if set(expected_identity) - _IDENTITY_FIELDS - {"pid"}:
                raise PeerVerificationError("Expected TCP peer identity contains unknown fields")
            if "pid" in expected_identity:
                pinned_pid = expected_identity["pid"]
                if (type(pinned_pid) is not int or pinned_pid <= 0
                        or expected_pid is not None and expected_pid != pinned_pid):
                    raise PeerVerificationError("Expected TCP peer PID does not match its identity")
                expected_pid = pinned_pid
        descriptor = connection.fileno()
        if descriptor < 0:
            raise PeerVerificationError("TCP socket is closed")
        endpoints = _socket_endpoints(connection)
        own_identity = _identity(windows.process_identity(os.getpid()))
        pid = _peer_pid(endpoints)
        if expected_pid is not None and pid != expected_pid:
            raise PeerVerificationError("TCP peer PID differs from the expected process")
        with _process_guard(pid):
            peer_identity = _identity(windows.process_identity(pid))
            if any(peer_identity[key] != own_identity[key]
                   for key in ("sid", "machine_id", "boot_id")):
                raise PeerVerificationError("TCP peer does not belong to the current Windows user")
            if expected is not None and peer_identity != expected:
                raise PeerVerificationError("TCP peer process identity differs from the expected process")
            if _peer_pid(endpoints) != pid or _identity(windows.process_identity(pid)) != peer_identity:
                raise PeerVerificationError("TCP peer ownership changed during verification")
            if _identity(windows.process_identity(os.getpid())) != own_identity:
                raise PeerVerificationError("Current Windows user identity changed during verification")
            if connection.fileno() != descriptor or _socket_endpoints(connection) != endpoints:
                raise PeerVerificationError("TCP connection changed during verification")
        return {"pid": pid, **peer_identity}
    except Exception as error:
        try:
            connection.close()
        except Exception:
            pass
        if isinstance(error, PeerVerificationError):
            raise
        raise PeerVerificationError("Could not verify Windows TCP peer ownership") from error
