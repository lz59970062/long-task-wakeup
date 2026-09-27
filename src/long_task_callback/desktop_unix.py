"""Desktop-owned macOS Core and transparent stdio / private Unix socket relay."""
from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path
import signal
import stat
import subprocess
import sys
import threading
import time

from .desktop_core import BRIDGE_FILE_ENV, RelayError, WebSocketRelay, _stdin_messages, _stdout_messages
from .platforms import macos
from .storage import write_request


def prepare_socket(metadata: Path) -> tuple[Path, int]:
    if not metadata.is_absolute():
        raise RelayError("Desktop bridge metadata must be an absolute path")
    path = metadata.parent / "core.sock"
    if len(os.fsencode(path)) > 103:
        raise RelayError("macOS Desktop socket path exceeds 103 bytes; choose a shorter bridge directory")
    metadata.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = metadata.parent.lstat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) & 0o077):
        raise RelayError("Desktop bridge directory must be private and owned by the current user")
    descriptor = os.open(str(metadata) + ".lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(descriptor)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) & 0o077):
            raise RelayError("Desktop bridge lock must be a private regular file")
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if any(p.exists() or p.is_symlink() for p in (path, metadata)):
            raise RelayError("Desktop bridge state already exists; inspect its owner before recovery")
        return path, descriptor
    except BaseException:
        os.close(descriptor)
        raise


def run_app_server(executable: Path, arguments: list[str]) -> int:
    from .cli import AppServerConnection
    from .desktop_connection import UnixBridgeEndpoint

    raw = os.environ.get(BRIDGE_FILE_ENV, "")
    if not raw:
        raise RelayError("macOS Desktop bridge metadata path is not configured")
    metadata = Path(raw)
    path, descriptor = prepare_socket(metadata)
    stopped = threading.Event()
    failures: list[BaseException] = []
    handlers = {}
    process = relay = record = socket_identity = None

    def stop(_signal, _frame):
        stopped.set()

    def worker(operation, *values):
        try:
            operation(*values)
        except BaseException as exc:
            if not stopped.is_set():
                failures.append(exc)
        finally:
            stopped.set()

    try:
        for number in (signal.SIGTERM, signal.SIGINT):
            handlers[number] = signal.signal(number, stop)
        # Preserve Desktop's flags and fresh tool environment. Only this child
        # is stopped at EOF; independent launchd business jobs remain alive.
        process = subprocess.Popen(
            [str(executable), *arguments, "--listen", f"unix://{path}"],
            stdin=subprocess.DEVNULL, stdout=sys.stderr, stderr=sys.stderr,
            close_fds=True, start_new_session=True, umask=0o077,
        )
        deadline = time.monotonic() + 15
        while not stopped.is_set():
            if path.is_socket():
                info = path.lstat()
                socket_identity = (info.st_dev, info.st_ino)
                if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) & 0o077:
                    raise RelayError("Desktop Core created an unverified socket")
                break
            if process.poll() is not None or time.monotonic() >= deadline:
                raise RelayError("Desktop Core did not create its Unix socket")
            stopped.wait(0.05)
        if stopped.is_set():
            return 0
        identity = macos.process_identity(process.pid)
        if identity is None:
            raise RelayError("Desktop Core identity could not be verified")
        # Codex may publish the requested path as a private symlink to its
        # short, per-process socket. Record the actual endpoint, then verify
        # its peer PID before passing Desktop bytes.
        actual_socket = path.resolve(strict=True)
        connection = AppServerConnection(UnixBridgeEndpoint(actual_socket, process.pid, identity), 5)
        connection.connect()
        relay = WebSocketRelay(connection.socket, stopped, initial=connection.buffer)
        record = {"version": 1, "transport": "unix", "socket": str(actual_socket),
                  "pid": process.pid, "identity": identity,
                  "codex_home": str(Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))).resolve())}
        write_request(metadata, record)
        threading.Thread(target=worker, args=(_stdin_messages, sys.stdin.fileno(), relay), daemon=True).start()
        threading.Thread(target=worker, args=(_stdout_messages, sys.stdout.fileno(), relay), daemon=True).start()
        while not stopped.wait(0.1):
            if process.poll() is not None:
                raise RelayError("Desktop App Server exited")
        if failures:
            raise RelayError("Desktop protocol relay ended unexpectedly") from failures[0]
        return 0
    finally:
        stopped.set()
        if relay is not None:
            relay.close()
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        if record is not None:
            try:
                if json.loads(metadata.read_text(encoding="utf-8")) == record:
                    metadata.unlink()
            except (OSError, ValueError):
                pass
        if socket_identity is not None:
            try:
                info = path.lstat()
                if (stat.S_ISSOCK(info.st_mode) or stat.S_ISLNK(info.st_mode)) and (info.st_dev, info.st_ino) == socket_identity:
                    path.unlink()
            except FileNotFoundError:
                pass
        os.close(descriptor)  # Keep the lock inode for future launches.
        for number, handler in handlers.items():
            signal.signal(number, handler)
