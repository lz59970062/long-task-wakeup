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
import time


@dataclass(frozen=True)
class BridgeEndpoint:
    port: int
    pid: int
    identity: dict[str, object]

    @property
    def url(self) -> str:
        return f"ws://127.0.0.1:{self.port}"


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
