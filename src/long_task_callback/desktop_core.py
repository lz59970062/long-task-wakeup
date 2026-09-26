"""Desktop-owned Core launcher and transparent JSONL/websocket relay.

Set CODEX_CLI_PATH to the installed ltc-desktop-core.exe. The Desktop supplies
fresh app-tools pipe environment and its normal Core configuration arguments.
This launcher passes all of them to the real executable, changing only the
listening transport for an App Server invocation. It never initializes a
session or interprets JSON-RPC ids, requests, responses or approvals.
"""
from __future__ import annotations

import base64
import os
from pathlib import Path
import socket
import struct
import subprocess
import sys
import threading

from .desktop_bridge import DesktopBridge, BridgeError, _verify_peer, read_http_header, validate_upgrade
from .platforms.windows_process import background_popen_kwargs


REAL_CODEX_ENV = "CODEX_LONG_TASK_WAKEUP_DESKTOP_REAL_CODEX"
BRIDGE_FILE_ENV = "CODEX_LONG_TASK_WAKEUP_DESKTOP_BRIDGE_FILE"
# Desktop traffic includes images and large tool output; the callback client's
# 1 MiB limit is intentionally not reused here. Both frames and reassembled
# messages are bounded, with no unbounded cross-thread message queue.
MAX_MESSAGE_BYTES = 16 * 1024 * 1024


class RelayError(RuntimeError):
    pass


def real_core() -> Path:
    raw = os.environ.get(REAL_CODEX_ENV, "")
    path = Path(raw)
    if not raw or not path.is_absolute() or path.suffix.lower() != ".exe" or not path.is_file():
        raise RelayError("Desktop real Core must be an existing absolute .exe path")
    path = path.resolve()
    wrapper = Path(sys.argv[0]).resolve()
    if (os.path.normcase(str(path)) == os.path.normcase(str(wrapper))
            or path.name.lower() in ("ltc-desktop-core.exe", "ltc-desktop-core-script.py")):
        raise RelayError("Desktop real Core must not refer to the wrapper itself")
    try:
        if wrapper.exists() and path.samefile(wrapper):
            raise RelayError("Desktop real Core must not refer to the wrapper itself")
        with path.open("rb") as handle:
            if handle.read(2) != b"MZ":
                raise RelayError("Desktop real Core is not a Windows executable")
    except OSError as exc:
        raise RelayError("Desktop real Core could not be inspected") from exc
    return path


def app_server_arguments(arguments: list[str]) -> list[str] | None:
    """Return listening args, or None for an ordinary CLI/help/codegen call."""
    index = 0
    while index < len(arguments):
        value = arguments[index]
        if value in ("-c", "--config", "--enable", "--disable"):
            index += 2
        elif any(value.startswith(prefix) for prefix in ("--config=", "--enable=", "--disable=")):
            index += 1
        else:
            break
    if index >= len(arguments) or arguments[index] != "app-server":
        return None
    tail = arguments[index + 1:]
    if any(value in ("--help", "-h", "--version", "-V", "generate-ts", "generate-json-schema") for value in tail):
        return None
    result = arguments[:index + 1]
    position = 0
    while position < len(tail):
        value = tail[position]
        if value == "--listen" or value.startswith("--listen="):
            if value == "--listen":
                position += 1
                if position >= len(tail):
                    raise RelayError("App Server listen transport is missing")
                transport = tail[position]
            else:
                transport = value.split("=", 1)[1]
            if transport != "stdio://":
                raise RelayError("Desktop wrapper accepts only the default stdio transport")
        elif value.startswith("--ws-"):
            raise RelayError("Desktop wrapper owns the App Server websocket authentication flags")
        else:
            result.append(value)
        position += 1
    return result


class WebSocketRelay:
    def __init__(self, connection: socket.socket, stop: threading.Event, *, initial: bytes = b""):
        self.connection = connection
        self.stop = stop
        self.buffer = bytearray(initial)
        self.send_lock = threading.Lock()
        # Desktop responses/images can be large or backpressured. Keep writes
        # blocking rather than imposing a tiny shared socket deadline; the
        # lifetime coordinator calls shutdown() to cancel reads and writes.
        self.connection.settimeout(None)

    @classmethod
    def connect(cls, bridge: DesktopBridge, stop: threading.Event) -> "WebSocketRelay":
        port = bridge.listener.getsockname()[1]
        connection = socket.create_connection(("127.0.0.1", port), timeout=5)
        try:
            expected = {"pid": bridge.record["pid"], **bridge.record["identity"]}
            _verify_peer(connection, expected_identity=expected)
            key = base64.b64encode(os.urandom(16)).decode("ascii")
            request = (f"GET / HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                       f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode("ascii")
            connection.sendall(request)
            response, trailing = read_http_header(connection, 5)
            validate_upgrade(response, key)
            _verify_peer(connection, expected_identity=expected)
            return cls(connection, stop, initial=trailing)
        except BaseException:
            connection.close()
            raise

    def close(self) -> None:
        try:
            self.connection.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.connection.close()

    def send(self, opcode: int, payload: bytes) -> None:
        if len(payload) > MAX_MESSAGE_BYTES or (opcode >= 8 and len(payload) > 125):
            raise RelayError("Desktop websocket message exceeds the relay limit")
        length = len(payload)
        header = bytes([0x80 | opcode])
        if length < 126:
            header += bytes([0x80 | length])
        elif length <= 65535:
            header += b"\xfe" + struct.pack("!H", length)
        else:
            header += b"\xff" + struct.pack("!Q", length)
        mask = os.urandom(4)
        masked = bytes(value ^ mask[index % 4] for index, value in enumerate(payload))
        with self.send_lock:
            if self.stop.is_set():
                raise RelayError("Desktop relay has stopped")
            self.connection.sendall(header + mask + masked)

    def _exact(self, size: int) -> bytes:
        while len(self.buffer) < size:
            if self.stop.is_set():
                raise RelayError("Desktop relay has stopped")
            try:
                data = self.connection.recv(min(65536, size - len(self.buffer)))
            except socket.timeout:
                continue
            if not data:
                raise EOFError("App Server websocket closed")
            self.buffer.extend(data)
        value = bytes(self.buffer[:size])
        del self.buffer[:size]
        return value

    def receive(self) -> bytes:
        chunks = bytearray()
        fragmented = False
        while True:
            first, second = self._exact(2)
            final, opcode = bool(first & 0x80), first & 0x0F
            if first & 0x70 or second & 0x80:
                raise RelayError("Invalid App Server websocket framing")
            length = second & 0x7F
            if length == 126:
                length = struct.unpack("!H", self._exact(2))[0]
            elif length == 127:
                length = struct.unpack("!Q", self._exact(8))[0]
            if length > MAX_MESSAGE_BYTES:
                raise RelayError("App Server websocket frame exceeds the relay limit")
            if opcode >= 8 and (not final or length > 125):
                raise RelayError("Invalid App Server websocket control frame")
            payload = self._exact(length)
            if opcode == 8:
                raise EOFError("App Server websocket closed")
            if opcode == 9:
                self.send(10, payload)
                continue
            if opcode == 10:
                continue
            if opcode == 1 and not fragmented:
                fragmented = True
            elif opcode != 0 or not fragmented:
                raise RelayError("App Server must send text websocket messages")
            if len(chunks) + length > MAX_MESSAGE_BYTES:
                raise RelayError("App Server websocket message exceeds the relay limit")
            chunks.extend(payload)
            if final:
                result = bytes(chunks)
                result.decode("utf-8")
                return result


def _stdin_messages(fd: int, relay: WebSocketRelay) -> None:
    pending = bytearray()
    while not relay.stop.is_set():
        # Raw OS reads avoid a daemon thread owning Python's buffered stdin
        # lock during interpreter exit after an unrelated upstream failure.
        chunk = os.read(fd, 65536)
        if not chunk:
            if pending:
                pending.decode("utf-8")
                relay.send(1, bytes(pending))
            return
        pending.extend(chunk)
        while True:
            end = pending.find(b"\n")
            if end < 0:
                break
            if end > MAX_MESSAGE_BYTES:
                raise RelayError("Desktop JSONL message exceeds the relay limit")
            line = bytes(pending[:end])
            del pending[:end + 1]
            if line.endswith(b"\r"):
                line = line[:-1]
            if line.strip():
                line.decode("utf-8")
                relay.send(1, line)
        if len(pending) > MAX_MESSAGE_BYTES:
            raise RelayError("Desktop JSONL message exceeds the relay limit")


def _stdout_messages(fd: int, relay: WebSocketRelay) -> None:
    while not relay.stop.is_set():
        message = relay.receive()
        # JSON strings cannot contain literal CR/LF; these are insignificant
        # whitespace in a valid JSON message. Preserve all ids/data verbatim
        # while ensuring exactly one JSONL record per websocket message.
        line = memoryview(message.replace(b"\r", b" ").replace(b"\n", b" ") + b"\n")
        while line and not relay.stop.is_set():
            written = os.write(fd, line)
            if not written:
                raise BrokenPipeError("Desktop stdout closed")
            line = line[written:]


def run_app_server(executable: Path, arguments: list[str], metadata: Path) -> int:
    if os.name != "nt":
        raise RelayError("The Desktop Core wrapper currently requires Windows")
    if not metadata.is_absolute():
        raise RelayError("Desktop bridge metadata must be an absolute path")
    profile = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))).resolve()
    # Preserve the Desktop's fresh environment (especially its App Tools named
    # pipe paths) by creating the real Core as our child, inside the bridge Job.
    instance = DesktopBridge(profile, str(executable), metadata, core_args=arguments)
    stopped = threading.Event()
    failures: list[BaseException] = []
    relay = None
    server_thread = None

    def worker(operation, *values):
        try:
            operation(*values)
        except EOFError as exc:
            if not stopped.is_set():
                failures.append(exc)
        except BaseException as exc:
            if not stopped.is_set():
                failures.append(exc)
        finally:
            stopped.set()

    try:
        instance.start()
        server_thread = threading.Thread(target=worker, args=(instance.serve,), daemon=True)
        server_thread.start()
        relay = WebSocketRelay.connect(instance, stopped)
        # msvcrt text mode otherwise rewrites newlines and treats Ctrl-Z as EOF.
        import msvcrt
        msvcrt.setmode(sys.stdin.fileno(), os.O_BINARY)
        msvcrt.setmode(sys.stdout.fileno(), os.O_BINARY)
        threading.Thread(target=worker, args=(_stdin_messages, sys.stdin.fileno(), relay), daemon=True).start()
        threading.Thread(target=worker, args=(_stdout_messages, sys.stdout.fileno(), relay), daemon=True).start()
        while not stopped.wait(0.1):
            if instance.upstream.poll() is not None:
                raise RelayError("Desktop App Server exited")
        if failures:
            raise RelayError("Desktop protocol relay ended unexpectedly") from failures[0]
        return 0
    finally:
        # Desktop stdin owns lifetime: EOF closes *all* gateway clients, even
        # an LTC connection still open after the Desktop App Tools pipe died.
        stopped.set()
        if relay is not None:
            relay.close()
        instance._stopping.set()
        if server_thread is not None:
            server_thread.join(timeout=1)
        instance.close()


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    try:
        executable = real_core()
        listening = app_server_arguments(arguments)
        if listening is None:
            # --version/help/codegen stay read-only and bypass bridge state.
            return subprocess.call([str(executable), *arguments], stdin=sys.stdin, stdout=sys.stdout,
                                   stderr=sys.stderr, close_fds=True, **background_popen_kwargs())
        raw_metadata = os.environ.get(BRIDGE_FILE_ENV, "")
        if not raw_metadata:
            raise RelayError("Desktop bridge metadata path is not configured")
        return run_app_server(executable, listening, Path(raw_metadata))
    except KeyboardInterrupt:
        return 130
    except (RelayError, BridgeError, OSError, UnicodeError) as exc:
        # stdout belongs exclusively to the Desktop's JSONL protocol.
        print(f"Desktop Core wrapper: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
