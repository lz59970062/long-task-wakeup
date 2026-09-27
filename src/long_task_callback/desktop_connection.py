"""Explicit discovery for the experimental Windows Desktop bridge.

The metadata contains no transport credential. A connected TCP peer must still
match its live process identity before any callback or HTTP bytes are sent.
"""
from __future__ import annotations

from dataclasses import dataclass
import errno
import json
import os
from pathlib import Path
import re
import stat
import sys
import time


@dataclass(frozen=True)
class BridgeEndpoint:
    port: int
    pid: int
    identity: dict[str, object]

    @property
    def url(self) -> str:
        return f"ws://127.0.0.1:{self.port}"


@dataclass(frozen=True)
class UnixBridgeEndpoint:
    path: Path
    pid: int
    identity: dict[str, object]


def load_unix_bridge_endpoint(path: Path, profile: Path) -> UnixBridgeEndpoint:
    from .platforms import macos

    if sys.platform != "darwin" or not path.is_absolute():
        raise ValueError("macOS Desktop bridge requires an absolute metadata path")
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) & 0o077 or info.st_size > 16_384):
        raise ValueError("Desktop bridge metadata must be a private regular file")
    record = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(record, dict) or record.get("version") != 1 or record.get("transport") != "unix":
        raise ValueError("unsupported macOS Desktop bridge metadata")
    raw = record.get("socket")
    pid, identity, saved_profile = record.get("pid"), record.get("identity"), record.get("codex_home")
    if (not isinstance(raw, str) or not Path(raw).is_absolute() or len(os.fsencode(raw)) > 103
            or type(pid) is not int or pid <= 0 or not isinstance(identity, dict)
            or not isinstance(saved_profile, str) or not Path(saved_profile).is_absolute()
            or Path(saved_profile).resolve() != profile.resolve()):
        raise ValueError("Desktop bridge identity/profile is invalid")
    socket_path = Path(raw)
    parent = socket_path.parent.lstat()
    endpoint = socket_path.lstat()
    if (not stat.S_ISDIR(parent.st_mode) or parent.st_uid != os.geteuid()
            or stat.S_IMODE(parent.st_mode) & 0o077
            or not stat.S_ISSOCK(endpoint.st_mode) or endpoint.st_uid != os.geteuid()
            or stat.S_IMODE(endpoint.st_mode) & 0o077):
        raise ValueError(f"Desktop bridge socket must be private and owned by this user (directory mode {oct(stat.S_IMODE(parent.st_mode))}, endpoint mode {oct(endpoint.st_mode)})")
    live = macos.process_identity(pid)
    if live is None or live != identity or live.get("uid") != os.geteuid():
        raise ValueError("Desktop bridge process identity is stale or unverified")
    return UnixBridgeEndpoint(socket_path, pid, identity)


def load_bridge_endpoint(path: Path, profile: Path) -> BridgeEndpoint:
    from .platforms import windows

    if os.name != "nt":
        raise ValueError("the Desktop bridge currently requires native Windows")
    if not path.is_absolute():
        raise ValueError("Desktop bridge metadata must use an absolute path")
    deadline = time.monotonic() + 0.5
    while True:
        try:
            if path.stat().st_size > 16_384:
                raise ValueError("Desktop bridge metadata is too large")
            record = json.loads(path.read_text(encoding="utf-8"))
            break
        except PermissionError as error:
            # Windows readers can briefly conflict with atomic publication.
            # Bounded retries do not turn persistent denial into trust.
            code = getattr(error, "winerror", None)
            transient = (code in (5, 32, 33)
                         or code is None and error.errno in (errno.EACCES, errno.EPERM))
            if not transient or time.monotonic() >= deadline:
                raise
            time.sleep(0.01)
    if not isinstance(record, dict) or record.get("version") != 1:
        raise ValueError("unsupported Desktop bridge metadata")
    url = record.get("url")
    match = re.fullmatch(r"ws://127\.0\.0\.1:([1-9][0-9]{0,4})", url) if isinstance(url, str) else None
    if match is None or int(match[1]) > 65535:
        raise ValueError("Desktop bridge requires an explicit IPv4 loopback endpoint")
    pid = record.get("pid")
    identity = record.get("identity")
    saved_profile = record.get("codex_home")
    if (type(pid) is not int or pid <= 0 or not isinstance(identity, dict)
            or not isinstance(saved_profile, str) or not Path(saved_profile).is_absolute()
            or os.path.normcase(str(Path(saved_profile).resolve())) != os.path.normcase(str(profile.resolve()))):
        raise ValueError("Desktop bridge identity/profile does not match this deployment")
    current = windows.process_identity(os.getpid())
    live = windows.process_identity(pid)
    if not live or not current or live != identity or live.get("sid") != current.get("sid"):
        raise ValueError("Desktop bridge process identity is stale or unverified")
    return BridgeEndpoint(int(match[1]), pid, identity)
