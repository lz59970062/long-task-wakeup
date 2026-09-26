"""Explicit Windows same-user gateway for one shared Codex App Server.

The desktop's experimental websocket override has no bearer-header setting.
This foreground gateway authenticates each local TCP peer with Windows process
credentials, then injects a private token for the App Server it launched. It
forwards websocket bytes; it does not multiplex or interpret JSON-RPC calls.
Nothing here discovers, restarts, or changes a running desktop application.
"""
from __future__ import annotations

import argparse
import base64
import ctypes
from ctypes import wintypes
import errno
import hashlib
import json
import math
import os
from pathlib import Path
import re
import secrets
import select
import socket
import subprocess
import sys
import threading
import time
from typing import Any

from .platforms import windows, windows_io
from .platforms.windows_process import background_popen_kwargs, enter_worker_job, prepare_command, terminate_worker_children
from .storage import write_private_text, write_request


HEADER_LIMIT = 64 * 1024
MAX_CONNECTIONS = 32
_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
_FIELD_NAME = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")
_FAILED_BRIDGES = []  # retain failed-drain locks/log handles until OS process exit


class BridgeError(RuntimeError):
    pass


def _sharing_conflict(error: OSError) -> bool:
    winerror = getattr(error, "winerror", None)
    # Python's CRT open() can expose only errno EACCES (no winerror), while
    # native MoveFileEx/CreateFile report their Windows error number.
    return winerror in (5, 32, 33) or (winerror is None and isinstance(error, PermissionError)
                                      and error.errno in (errno.EACCES, errno.EPERM))


def _verify_peer(connection: socket.socket, expected_identity=None) -> dict[str, Any]:
    # Keep import lazy so parser/status helpers remain importable off Windows.
    from .platforms.windows_tcp import verify_loopback_peer
    return verify_loopback_peer(connection, expected_identity=expected_identity)


def _parent_snapshot() -> dict[int, int]:
    """A parent map is only a hint; authenticate every live node separately."""
    class Entry(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                    ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ctypes.c_size_t),
                    ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                    ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", wintypes.LONG),
                    ("dwFlags", wintypes.DWORD), ("szExeFile", wintypes.WCHAR * 260)]
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(Entry)]
    kernel.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(Entry)]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.CreateToolhelp32Snapshot(0x00000002, 0)
    if handle == ctypes.c_void_p(-1).value:
        raise BridgeError("Cannot inspect the App Server process tree")
    try:
        entry = Entry()
        entry.dwSize = ctypes.sizeof(entry)
        found = kernel.Process32FirstW(handle, ctypes.byref(entry))
        result = {}
        while found:
            result[int(entry.th32ProcessID)] = int(entry.th32ParentProcessID)
            found = kernel.Process32NextW(handle, ctypes.byref(entry))
        return result
    finally:
        kernel.CloseHandle(handle)


def verify_process_tree(peer: dict[str, Any], root_pid: int, root_identity: dict[str, Any]) -> None:
    """Reject recycled, exited, foreign-user or unrelated upstream processes."""
    parents = _parent_snapshot()
    expected_keys = ("sid", "boot_id", "machine_id", "creation_time")
    identity = {key: peer.get(key) for key in expected_keys}
    pid = peer.get("pid")
    seen = set()
    identities = []
    for _ in range(64):
        if type(pid) is not int or pid <= 0 or pid in seen:
            break
        seen.add(pid)
        current = windows.process_identity(pid)
        if current is None or current != identity:
            break
        if any(current.get(key) != root_identity.get(key) for key in ("sid", "boot_id", "machine_id")):
            break
        identities.append((pid, current))
        if pid == root_pid:
            if current != root_identity:
                break
            # Parent fields come from a snapshot, not ownership. Revalidate all
            # identities after walking the chain so exited/reused nodes fail.
            if all(windows.process_identity(node) == value for node, value in identities):
                return
            break
        parent = parents.get(pid)
        older = windows.process_identity(parent) if type(parent) is int else None
        if (older is None or type(older.get("creation_time")) is not int
                or type(current.get("creation_time")) is not int
                or older["creation_time"] > current["creation_time"]):
            break
        pid, identity = parent, older
    raise BridgeError("App Server connection is not owned by the launched process tree")


def read_http_header(connection: socket.socket, timeout: float) -> tuple[bytes, bytes]:
    deadline = time.monotonic() + timeout
    data = bytearray()
    while True:
        marker = data.find(b"\r\n\r\n")
        if marker >= 0:
            if marker + 4 > HEADER_LIMIT:
                raise BridgeError("Websocket handshake header is too large")
            return bytes(data[:marker + 4]), bytes(data[marker + 4:])
        if len(data) >= HEADER_LIMIT:
            raise BridgeError("Websocket handshake header is too large")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise BridgeError("Websocket handshake timed out")
        connection.settimeout(remaining)
        chunk = connection.recv(min(8192, HEADER_LIMIT - len(data)))
        if not chunk:
            raise BridgeError("Websocket handshake ended early")
        data.extend(chunk)


def _parse_header(header: bytes) -> tuple[str, dict[str, str]]:
    try:
        lines = header.decode("ascii").split("\r\n")
    except UnicodeError as exc:
        raise BridgeError("Invalid websocket handshake encoding") from exc
    if not header.endswith(b"\r\n\r\n") or len(header) > HEADER_LIMIT:
        raise BridgeError("Invalid websocket handshake header")
    fields = {}
    for line in lines[1:-2]:
        if not line or line[0].isspace() or ":" not in line:
            raise BridgeError("Invalid websocket handshake field")
        name, value = line.split(":", 1)
        if not _FIELD_NAME.fullmatch(name) or any(ord(char) < 32 or ord(char) == 127 for char in value):
            raise BridgeError("Invalid websocket handshake field")
        name = name.lower()
        if name in fields:
            raise BridgeError("Duplicate websocket handshake field")
        fields[name] = value.strip(" ")
    return lines[0], fields


def upstream_request(header: bytes, port: int, token: str) -> tuple[bytes, str]:
    first, fields = _parse_header(header)
    if first != "GET / HTTP/1.1":
        raise BridgeError("Only GET / websocket upgrades are accepted")
    if any(name in fields for name in ("origin", "authorization", "proxy-authorization", "content-length", "transfer-encoding")):
        raise BridgeError("Browser origins, supplied credentials and request bodies are not accepted")
    if (fields.get("upgrade", "").lower() != "websocket"
            or "upgrade" not in {part.strip().lower() for part in fields.get("connection", "").split(",")}
            or fields.get("sec-websocket-version") != "13"):
        raise BridgeError("A websocket version 13 upgrade is required")
    key = fields.get("sec-websocket-key", "")
    try:
        valid_key = len(base64.b64decode(key, validate=True)) == 16
    except (ValueError, base64.binascii.Error):
        valid_key = False
    if not valid_key:
        raise BridgeError("Invalid websocket key")
    lines = ["GET / HTTP/1.1", f"Host: 127.0.0.1:{port}", "Upgrade: websocket", "Connection: Upgrade",
             f"Sec-WebSocket-Key: {key}", "Sec-WebSocket-Version: 13", f"Authorization: Bearer {token}"]
    for name in ("sec-websocket-protocol", "sec-websocket-extensions"):
        if name in fields:
            lines.append(f"{name}: {fields[name]}")
    return ("\r\n".join(lines) + "\r\n\r\n").encode("ascii"), key


def validate_upgrade(header: bytes, key: str) -> None:
    first, fields = _parse_header(header)
    if (not first.startswith("HTTP/1.1 101 ") or fields.get("upgrade", "").lower() != "websocket"
            or "upgrade" not in {part.strip().lower() for part in fields.get("connection", "").split(",")}):
        raise BridgeError("App Server refused the websocket upgrade")
    expected = base64.b64encode(hashlib.sha1((key + _GUID).encode("ascii")).digest()).decode("ascii")
    if fields.get("sec-websocket-accept") != expected:
        raise BridgeError("App Server returned an invalid websocket accept key")


def _listener() -> socket.socket:
    result = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            result.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        result.bind(("127.0.0.1", 0))
        result.set_inheritable(False)
        result.listen(MAX_CONNECTIONS)
        return result
    except BaseException:
        result.close()
        raise


def _read_record(path: Path) -> dict[str, Any] | None:
    try:
        deadline = time.monotonic() + 0.5
        while True:
            try:
                text = path.read_text(encoding="utf-8")
                break
            except OSError as exc:
                if not _sharing_conflict(exc) or time.monotonic() >= deadline:
                    raise
                time.sleep(0.01)
        value = json.loads(text)
    except FileNotFoundError:
        return None
    except (ValueError, UnicodeError) as exc:
        raise BridgeError("Bridge metadata is not valid JSON") from exc
    if not isinstance(value, dict):
        raise BridgeError("Bridge metadata is not an object")
    return value


def _remove_file(path: Path) -> None:
    deadline = time.monotonic() + 0.5
    while True:
        try:
            path.unlink(missing_ok=True)
            return
        except OSError as exc:
            if not _sharing_conflict(exc) or time.monotonic() >= deadline:
                raise
            time.sleep(0.01)


def _identity_matches(record: dict[str, Any]) -> bool:
    pid = record.get("pid")
    identity = record.get("identity")
    return type(pid) is int and isinstance(identity, dict) and bool(identity) and windows.process_identity(pid) == identity


def bridge_status(metadata_file: Path) -> dict[str, Any]:
    record = _read_record(metadata_file)
    if record is None:
        return {"running": False, "metadata_file": str(metadata_file)}
    # Inspection is read-only: do not create a lock, fix ACLs, or clean records.
    return {**record, "running": _identity_matches(record), "metadata_file": str(metadata_file)}


def stop_bridge(metadata_file: Path, timeout: float = 10.0) -> dict[str, Any]:
    record = _read_record(metadata_file)
    if record is None or not _identity_matches(record):
        raise BridgeError("No bridge with the recorded live process identity")
    if record.get("active_clients", 0):
        raise BridgeError("Bridge still has active clients; close them before stopping")
    nonce = secrets.token_hex(16)
    request_file = metadata_file.with_name(metadata_file.name + ".stop.json")
    response_file = metadata_file.with_name(metadata_file.name + f".stop-{nonce}.json")
    write_request(request_file, {"nonce": nonce, "pid": record["pid"], "identity": record["identity"]})
    deadline = time.monotonic() + timeout
    try:
        while time.monotonic() < deadline:
            response = _read_record(response_file)
            if response is not None:
                if response.get("nonce") != nonce:
                    raise BridgeError("Invalid bridge stop response")
                if response.get("stopped"):
                    # The reply seals admission, but cleanup and Job teardown
                    # still happen before process exit. A successful stop must
                    # also allow an immediate restart of this metadata path.
                    while time.monotonic() < deadline:
                        if not windows.pid_is_running(record["pid"]):
                            return response
                        time.sleep(0.05)
                    raise BridgeError("Bridge accepted stop but has not exited yet")
                raise BridgeError("Bridge still has active clients; close them before stopping")
            time.sleep(0.05)
        raise BridgeError("Bridge stop request timed out")
    finally:
        _remove_file(response_file)


class DesktopBridge:
    def __init__(self, codex_home: Path, codex_bin: str, metadata_file: Path,
                 *, startup_timeout: float = 15.0, handshake_timeout: float = 5.0,
                 core_args: list[str] | None = None):
        self.codex_home = codex_home.resolve()
        self.codex_bin = codex_bin
        self.metadata_file = metadata_file.resolve()
        self.startup_timeout = startup_timeout
        self.handshake_timeout = handshake_timeout
        self.core_args = list(core_args) if core_args is not None else ["app-server"]
        self.listener = None
        self.upstream = None
        self.upstream_identity = None
        self.upstream_port = 0
        self.token = secrets.token_urlsafe(48)
        self.token_file = self.metadata_file.with_name(f".desktop-bridge-{secrets.token_hex(16)}.token")
        self.log_file = self.metadata_file.with_name(f"desktop-bridge-app-server-{secrets.token_hex(8)}.log")
        self._lock = None
        self._job_handle = None
        self._log = None
        self._mutex = threading.RLock()
        self._close_mutex = threading.RLock()
        self._clients: set[socket.socket] = set()
        self._stopping = threading.Event()
        self._published = False
        self._stop_response = None
        self._children_drained = False
        self._close_failure = None
        self._closed = False
        self.record: dict[str, Any] = {}

    def _publish(self) -> None:
        with self._mutex:
            self.record["active_clients"] = len(self._clients)
            # Ordinary Windows readers can briefly deny FILE_SHARE_DELETE.
            # Retry this transient publication conflict, never overwrite in
            # place or classify a persistent ACL failure as success.
            deadline = time.monotonic() + 0.5
            while True:
                try:
                    write_request(self.metadata_file, self.record)
                    break
                except OSError as exc:
                    if not _sharing_conflict(exc) or time.monotonic() >= deadline:
                        raise
                    time.sleep(0.01)
            self._published = True

    def _upstream_socket(self) -> tuple[socket.socket, dict[str, Any]]:
        connection = socket.create_connection(("127.0.0.1", self.upstream_port), timeout=self.handshake_timeout)
        try:
            peer = _verify_peer(connection)
            verify_process_tree(peer, self.upstream.pid, self.upstream_identity)
            return connection, peer
        except BaseException:
            connection.close()
            raise

    def _upgrade(self, request: bytes, key: str) -> tuple[socket.socket, bytes, bytes]:
        connection, peer = self._upstream_socket()
        try:
            connection.sendall(request)
            response, trailing = read_http_header(connection, self.handshake_timeout)
            validate_upgrade(response, key)
            _verify_peer(connection, expected_identity=peer)
            verify_process_tree(peer, self.upstream.pid, self.upstream_identity)
            return connection, response, trailing
        except BaseException:
            connection.close()
            raise

    def start(self) -> None:
        windows_io.require_windows("Codex Desktop bridge")
        if any(not math.isfinite(value) or value <= 0 for value in (self.startup_timeout, self.handshake_timeout)):
            raise BridgeError("Timeouts must be finite and positive")
        self._lock = windows_io.acquire_path_lock(self.metadata_file.with_name(self.metadata_file.name + ".lock"), blocking=False)
        if self._lock is None:
            raise BridgeError("A bridge already owns this metadata path")
        try:
            previous = _read_record(self.metadata_file)
            if previous and (type(previous.get("pid")) is not int or windows.pid_is_running(previous["pid"])):
                raise BridgeError("Refusing to replace metadata for a live or unverifiable process")
            # Do not rewrite an existing Codex profile's DACL. Only bridge
            # state/token/log directories need our protected private ACL.
            if not self.codex_home.exists():
                windows_io.ensure_private_directory(self.codex_home)
            elif not self.codex_home.is_dir():
                raise BridgeError("Codex home must be a directory")
            self._job_handle = enter_worker_job()
            self.listener = _listener()
            # App Server cannot adopt a Python listener. A port-reuse race is
            # harmless: authenticate the real owner before sending the token.
            reservation = _listener()
            self.upstream_port = reservation.getsockname()[1]
            reservation.close()
            write_private_text(self.token_file, self.token + "\n")
            self._log = windows_io.open_private_text(self.log_file)
            env = os.environ.copy()
            env["CODEX_HOME"] = str(self.codex_home)
            command = prepare_command([self.codex_bin, *self.core_args, "--listen", f"ws://127.0.0.1:{self.upstream_port}",
                                       "--ws-auth", "capability-token", "--ws-token-file", str(self.token_file)], env=env)
            self.upstream = subprocess.Popen(command, env=env, stdin=subprocess.DEVNULL, stdout=self._log,
                                             stderr=self._log, close_fds=True, **background_popen_kwargs())
            self.upstream_identity = windows.process_identity(self.upstream.pid)
            if self.upstream_identity is None:
                raise BridgeError("Cannot verify the launched App Server process")
            key = base64.b64encode(secrets.token_bytes(16)).decode("ascii")
            request = (f"GET / HTTP/1.1\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Version: 13\r\nSec-WebSocket-Key: {key}\r\n\r\n").encode("ascii")
            request, key = upstream_request(request, self.upstream_port, self.token)
            deadline = time.monotonic() + self.startup_timeout
            while True:
                if self.upstream.poll() is not None:
                    raise BridgeError("App Server exited before bridge startup completed")
                try:
                    connection, _, _ = self._upgrade(request, key)
                    connection.close()
                    break
                except (OSError, BridgeError):
                    if time.monotonic() >= deadline:
                        raise BridgeError("App Server could not be authenticated before startup timeout") from None
                    time.sleep(0.05)
            identity = windows.process_identity(os.getpid())
            if identity is None:
                raise BridgeError("Cannot verify the bridge process identity")
            self.record = {"version": 1, "url": f"ws://127.0.0.1:{self.listener.getsockname()[1]}",
                           "pid": os.getpid(), "identity": identity, "codex_home": str(self.codex_home)}
            self._publish()
        except BaseException:
            self.close()
            raise

    def _forward(self, front: socket.socket) -> None:
        upstream = None
        upgraded = False
        try:
            # No client bytes are read until reverse-tuple SID verification.
            peer = _verify_peer(front)
            header, trailing = read_http_header(front, self.handshake_timeout)
            if trailing:
                raise BridgeError("Early websocket data is not accepted")
            request, key = upstream_request(header, self.upstream_port, self.token)
            _verify_peer(front, expected_identity=peer)
            upstream, response, trailing = self._upgrade(request, key)
            front.sendall(response + trailing)
            upgraded = True
            # The handshake has a deadline; a live protocol stream may idle
            # or backpressure on large Desktop messages. Lifetime cleanup
            # shuts down sockets/Core, so it can cancel blocking I/O safely.
            front.settimeout(None)
            upstream.settimeout(None)
            while not self._stopping.is_set():
                ready, _, _ = select.select([front, upstream], [], [], 0.25)
                for source in ready:
                    data = source.recv(65536)
                    if not data:
                        return
                    (upstream if source is front else front).sendall(data)
        except (OSError, BridgeError, ValueError):
            if not upgraded:
                try:
                    front.sendall(b"HTTP/1.1 403 Forbidden\r\nConnection: close\r\nContent-Length: 0\r\n\r\n")
                except OSError:
                    pass
        finally:
            if upstream is not None:
                upstream.close()
            front.close()
            with self._mutex:
                self._clients.discard(front)
                if self._published and not self._stopping.is_set():
                    try:
                        self._publish()
                    except OSError:
                        self._stopping.set()

    def _check_stop(self) -> bool:
        path = self.metadata_file.with_name(self.metadata_file.name + ".stop.json")
        try:
            request = _read_record(path)
        except (OSError, BridgeError):
            return False
        if request is None:
            return False
        _remove_file(path)
        nonce = request.get("nonce", "")
        if (not isinstance(nonce, str) or not re.fullmatch(r"[0-9a-f]{32}", nonce)
                or request.get("pid") != self.record["pid"] or request.get("identity") != self.record["identity"]):
            return False
        with self._mutex:
            if self._clients:
                stopped = False
            else:
                # Seal admission before acknowledging; stop cannot race accept.
                self._stopping.set()
                self.listener.close()
                stopped = True
            response = self.metadata_file.with_name(self.metadata_file.name + f".stop-{nonce}.json")
            if stopped:
                # Announce completion only after child cleanup and local
                # resource release succeeded, not when stop was accepted.
                self._stop_response = (response, nonce)
            else:
                write_request(response, {"nonce": nonce, "stopped": False})
            return stopped

    def serve(self) -> None:
        if not self._published:
            raise BridgeError("Bridge has not been started")
        try:
            self.listener.settimeout(0.2)
            while not self._stopping.is_set():
                if self.upstream.poll() is not None:
                    raise BridgeError("App Server exited; the bridge has closed")
                if self._check_stop():
                    return
                try:
                    connection, _ = self.listener.accept()
                except socket.timeout:
                    continue
                connection.set_inheritable(False)
                with self._mutex:
                    if len(self._clients) >= MAX_CONNECTIONS:
                        connection.close()
                        continue
                    self._clients.add(connection)
                    try:
                        self._publish()
                    except BaseException:
                        self._clients.discard(connection)
                        connection.close()
                        raise
                threading.Thread(target=self._forward, args=(connection,), daemon=True).start()
        finally:
            self.close()

    def close(self) -> None:
        with self._close_mutex:
            if self._closed:
                return
            if self._close_failure is not None:
                raise BridgeError(f"Bridge child cleanup previously failed: {self._close_failure}") from self._close_failure
            try:
                self._close()
                self._closed = True
            except BaseException as exc:
                self._close_failure = exc
                if not self._children_drained:
                    # Keep singleton/log ownership and diagnostic metadata.
                    # Dropping the Python object must not release them before
                    # the containing process closes its kill-on-close Job.
                    _FAILED_BRIDGES.append(self)
                raise

    def _close(self) -> None:
        self._stopping.set()
        if self.listener is not None:
            self.listener.close()
        with self._mutex:
            for connection in list(self._clients):
                try:
                    connection.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                connection.close()
        if self._job_handle is not None:
            terminate_worker_children(self._job_handle, timeout=5.0)
        if self.upstream is not None:
            # The exact Job drain also includes the direct Core/launcher.
            # Reap its Popen handle without a second racing TerminateProcess.
            self.upstream.wait(timeout=0)
        self._children_drained = True
        try:
            if self._log is not None:
                self._log.close()
                self._log = None
            if self._published:
                current = _read_record(self.metadata_file)
                if current and current.get("identity") == self.record.get("identity") and current.get("pid") == os.getpid():
                    _remove_file(self.metadata_file)
                self._published = False
        finally:
            try:
                _remove_file(self.token_file)
            finally:
                if self._lock is not None:
                    self._lock[0].close()
                    self._lock = None
        # The process-wide Job remains open until OS exit, killing all App
        # Server descendants even when a CLI/node/venv wrapper was terminated.
        if self._stop_response is not None:
            response, nonce = self._stop_response
            write_request(response, {"nonce": nonce, "stopped": True})
            self._stop_response = None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--codex-home", type=Path, default=Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))))
    parser.add_argument("--codex-bin", default="codex")
    parser.add_argument("--metadata-file", type=Path)
    parser.add_argument("--startup-timeout", type=float, default=15.0)
    parser.add_argument("--handshake-timeout", type=float, default=5.0)
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--status", action="store_true")
    action.add_argument("--stop", action="store_true")
    args = parser.parse_args(argv)
    metadata = (args.metadata_file or args.codex_home / "long-task-wakeup" / "desktop-bridge.json").resolve()
    try:
        windows_io.require_windows("Codex Desktop bridge")
        if args.status:
            status = bridge_status(metadata)
            print(json.dumps(status, ensure_ascii=False))
            return 0 if status["running"] else 1
        elif args.stop:
            print(json.dumps(stop_bridge(metadata), ensure_ascii=False))
        else:
            bridge = DesktopBridge(args.codex_home, args.codex_bin, metadata,
                                   startup_timeout=args.startup_timeout, handshake_timeout=args.handshake_timeout)
            bridge.start()
            print(json.dumps(bridge.record, ensure_ascii=False), flush=True)
            bridge.serve()
        return 0
    except KeyboardInterrupt:
        return 130
    except (BridgeError, OSError, RuntimeError) as exc:
        # No request headers, bearer token, or upstream output in diagnostics.
        print(f"Desktop bridge: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
