"""Claude Code installation and session discovery on the local host.

Claude Code is often installed without a ``claude`` launcher on PATH: the
desktop app bundles its own versioned binary, and launchd/systemd services see
a minimal PATH. Session transcripts are stored per original project directory,
so a headless resume must start where the session started. These helpers only
read the filesystem; they never launch Claude or change its configuration.
"""

from __future__ import annotations

import json
import os
import re
import shutil
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import NamedTuple, Optional

from .agents.claude import CLAUDE_BIN_ENV

EXECPATH_ENV = "CLAUDE_CODE_EXECPATH"
ENTRYPOINT_ENV = "CLAUDE_CODE_ENTRYPOINT"
DESKTOP_ENTRYPOINT = "claude-desktop"

# Standalone installers (native, npm global, Homebrew, bun) in preference order.
STANDALONE_LOCATIONS = (
    "~/.local/bin/claude",
    "~/.claude/local/claude",
    "/opt/homebrew/bin/claude",
    "/usr/local/bin/claude",
    "~/.npm-global/bin/claude",
    "~/.bun/bin/claude",
)
# The macOS desktop app keeps one bundle per version/build.
DESKTOP_BUNDLE_GLOB = "Library/Application Support/Claude/claude-code/*/*/claude.app/Contents/MacOS/claude"
SESSION_ID_PATTERN = re.compile(r"[A-Za-z0-9_-]+")
# A session's first records carry its starting cwd; never scan a whole transcript.
SESSION_CWD_SCAN_LINES = 200


class Executable(NamedTuple):
    path: str
    source: str  # configured | path | standalone | session | desktop-bundle


Which = Callable[..., Optional[str]]


def _is_executable(path: Path) -> bool:
    return path.is_file() and os.access(path, os.X_OK)


def _home(environment: Mapping[str, str]) -> Path:
    return Path(environment.get("HOME") or Path.home())


def is_desktop_bundle(path: str | os.PathLike[str]) -> bool:
    return "/claude-code/" in str(path) and "claude.app/Contents/MacOS/" in str(path)


def desktop_bundles(environment: Mapping[str, str]) -> list[Path]:
    """Return bundled desktop binaries, newest first."""
    candidates = [path for path in _home(environment).glob(DESKTOP_BUNDLE_GLOB) if _is_executable(path)]
    return sorted(candidates, key=lambda path: path.stat().st_mtime, reverse=True)


def discover_executable(environment: Mapping[str, str], *, which: Which = shutil.which) -> Executable | None:
    """Find a Claude Code binary usable outside the launching session.

    An explicit configuration wins. A standalone CLI is preferred to the
    desktop bundle because the bundle authenticates through its host app and
    usually cannot run headless on its own. Bare names are resolved by
    ``which`` against the current process PATH.
    """
    home = _home(environment)
    configured = environment.get(CLAUDE_BIN_ENV, "").strip()
    if configured:
        expanded = Path(configured).expanduser()
        if expanded.is_absolute() or os.sep in configured:
            # Honor an explicit path even when it is missing, so a broken
            # configuration fails visibly. Only a versioned desktop bundle
            # replaced by an app update is rediscovered.
            if _is_executable(expanded) or not is_desktop_bundle(expanded):
                return Executable(str(expanded), "configured")
        else:
            found = which(configured)
            if found:
                return Executable(found, "configured")
            if configured != "claude":
                # Setup records the bare default when nothing was found; any
                # other name is a deliberate choice that must not be replaced.
                return Executable(configured, "configured")
    found = which("claude")
    if found:
        return Executable(found, "desktop-bundle" if is_desktop_bundle(found) else "path")
    for location in STANDALONE_LOCATIONS:
        path = Path(location.replace("~", str(home), 1))
        if _is_executable(path):
            return Executable(str(path), "standalone")
    session_binary = environment.get(EXECPATH_ENV, "").strip()
    if session_binary and _is_executable(Path(session_binary)):
        return Executable(session_binary, "desktop-bundle" if is_desktop_bundle(session_binary) else "session")
    bundles = desktop_bundles(environment)
    if bundles:
        return Executable(str(bundles[0]), "desktop-bundle")
    return None


def with_resolved_executable(environment: Mapping[str, str], *, which: Which = shutil.which) -> dict[str, str]:
    """Return ``environment`` whose configured Claude binary is runnable when possible."""
    resolved = dict(environment)
    found = discover_executable(environment, which=which)
    if found is not None:
        resolved[CLAUDE_BIN_ENV] = found.path
    return resolved


def is_desktop_session(environment: Mapping[str, str]) -> bool:
    return environment.get(ENTRYPOINT_ENV, "").strip() == DESKTOP_ENTRYPOINT


def config_dir(environment: Mapping[str, str]) -> Path:
    configured = environment.get("CLAUDE_CONFIG_DIR", "").strip()
    return Path(configured).expanduser() if configured else _home(environment) / ".claude"


def find_session_file(session_id: str, environment: Mapping[str, str]) -> Path | None:
    if not SESSION_ID_PATTERN.fullmatch(session_id):
        return None
    matches = [path for path in (config_dir(environment) / "projects").glob(f"*/{session_id}.jsonl") if path.is_file()]
    if not matches:
        return None
    return max(matches, key=lambda path: path.stat().st_mtime)


def session_project_cwd(session_id: str, environment: Mapping[str, str]) -> str | None:
    """Return the directory a session started in, if it still exists.

    Claude Code stores a session under a slug of its starting directory and
    ``--resume <id>`` looks the session up from the current directory, so a
    later ``cd`` inside the session must not change where it is resumed.
    """
    path = find_session_file(session_id, environment)
    if path is None:
        return None
    try:
        with path.open(encoding="utf-8", errors="replace") as handle:
            for index, line in enumerate(handle):
                if index >= SESSION_CWD_SCAN_LINES:
                    break
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                cwd = record.get("cwd") if isinstance(record, dict) else None
                if isinstance(cwd, str) and cwd:
                    return cwd if Path(cwd).is_dir() else None
    except OSError:
        return None
    return None


def resume_cwd(request: Mapping[str, object], environment: Mapping[str, str]) -> str | None:
    target = request.get("target")
    if not isinstance(target, dict) or target.get("kind") != "session":
        return None
    value = target.get("value")
    return session_project_cwd(value, environment) if isinstance(value, str) else None
