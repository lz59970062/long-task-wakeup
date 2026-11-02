"""DeepSeek Harness installation and session metadata on the local host.

DSH keeps a session's durable log under ``$DSH_HOME/sessions`` and a plain-JSON
projection-cache row under ``$DSH_HOME/storages``. The cache row records the
directory the session started in. Everything here only reads files and locates
the launcher; no DSH session is opened or changed.
"""

from __future__ import annotations

import json
import os
import re
import shutil
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import NamedTuple, Optional

from .agents.dsh import DSH_BIN_ENV

SESSION_ID_PATTERN = re.compile(r"session-[A-Za-z0-9-]+")
STANDALONE_LOCATIONS = (
    "~/.local/bin/dsh",
    "/opt/homebrew/bin/dsh",
    "/usr/local/bin/dsh",
    "/Applications/DeepSeek Harness.app/Contents/Resources/runtime/cli/bin/dsh",
)

Which = Callable[..., Optional[str]]


class Executable(NamedTuple):
    path: str
    source: str  # configured | path | standalone


def _home(environment: Mapping[str, str]) -> Path:
    return Path(environment.get("HOME") or Path.home())


def _is_executable(path: Path) -> bool:
    return path.is_file() and os.access(path, os.X_OK)


def dsh_home(environment: Mapping[str, str]) -> Path:
    configured = environment.get("DSH_HOME", "").strip()
    return Path(configured).expanduser() if configured else _home(environment) / ".dsh"


def is_valid_session(session_id: str) -> bool:
    return bool(SESSION_ID_PATTERN.fullmatch(session_id))


def session_cache_path(session_id: str, environment: Mapping[str, str]) -> Path | None:
    if not is_valid_session(session_id):
        return None
    return dsh_home(environment) / "storages" / "session_projcache" / "sessions" / f"{session_id}.json"


def session_cwd(session_id: str, environment: Mapping[str, str]) -> str | None:
    """Return the directory a session started in, if it still exists."""
    path = session_cache_path(session_id, environment)
    if path is None:
        return None
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    identity = record.get("record", {}).get("identity") if isinstance(record, dict) else None
    cwd = identity.get("cwd") if isinstance(identity, dict) else None
    if isinstance(cwd, str) and cwd and Path(cwd).is_dir():
        return cwd
    return None


def session_exists(session_id: str, environment: Mapping[str, str]) -> bool:
    if not is_valid_session(session_id):
        return False
    root = dsh_home(environment) / "sessions"
    if root.is_dir() and any(root.glob(f"*/{session_id}")):
        return True
    path = session_cache_path(session_id, environment)
    return bool(path is not None and path.is_file())


def discover_executable(environment: Mapping[str, str], *, which: Which = shutil.which) -> Executable | None:
    """Find a ``dsh`` launcher usable outside the invoking DSH shell."""
    home = _home(environment)
    configured = environment.get(DSH_BIN_ENV, "").strip()
    if configured:
        expanded = Path(configured).expanduser()
        if expanded.is_absolute() or os.sep in configured:
            return Executable(str(expanded), "configured")
        found = which(configured)
        if found:
            return Executable(found, "configured")
        if configured != "dsh":
            return Executable(configured, "configured")
    found = which("dsh")
    if found:
        return Executable(found, "path")
    for location in STANDALONE_LOCATIONS:
        path = Path(location.replace("~", str(home), 1))
        if _is_executable(path):
            return Executable(str(path), "standalone")
    return None


def with_resolved_executable(environment: Mapping[str, str], *, which: Which = shutil.which) -> dict[str, str]:
    resolved = dict(environment)
    found = discover_executable(environment, which=which)
    if found is not None:
        resolved[DSH_BIN_ENV] = found.path
    return resolved
