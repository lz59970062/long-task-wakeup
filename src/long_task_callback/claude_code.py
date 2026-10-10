"""Claude Code installation and session discovery on the local host.

Claude Code is often installed without a ``claude`` launcher on PATH: the
desktop app bundles its own versioned binary (on macOS inside the app, on a
Linux host reached over SSH under ``~/.claude/remote``), nvm installs live in a
per-Node-version directory, and launchd/systemd services see a minimal PATH. Session transcripts are stored per original project directory,
so a headless resume must start where the session started. These helpers only
read the filesystem; they never launch Claude or change its configuration.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import shutil
import sys
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import NamedTuple, Optional

from .agents.claude import CLAUDE_BIN_ENV, CLAUDE_THREAD_ID_ENV as SESSION_ENV

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
    "~/.nvm/versions/node/*/bin/claude",  # newest Node version first
)
# The macOS desktop app keeps one bundle per version/build.
DESKTOP_BUNDLE_GLOB = "Library/Application Support/Claude/claude-code/*/*/claude.app/Contents/MacOS/claude"
# The desktop app's SSH sessions install one plain CLI per version on the remote
# host. It shares the host's ~/.claude sign-in, so unlike the macOS bundle it can
# resume headless whenever a standalone CLI could; it is only versioned.
DESKTOP_REMOTE_GLOB = ".claude/remote/ccd-cli/*"
SESSION_ID_PATTERN = re.compile(r"[A-Za-z0-9_-]+")
# A session's first records carry its starting cwd; never scan a whole transcript.
SESSION_CWD_SCAN_LINES = 200


class Executable(NamedTuple):
    path: str
    source: str  # configured | path | standalone | session | desktop-remote | desktop-bundle


Which = Callable[..., Optional[str]]


def _is_executable(path: Path) -> bool:
    return path.is_file() and os.access(path, os.X_OK)


def _home(environment: Mapping[str, str]) -> Path:
    return Path(environment.get("HOME") or Path.home())


def is_desktop_bundle(path: str | os.PathLike[str]) -> bool:
    return "/claude-code/" in str(path) and "claude.app/Contents/MacOS/" in str(path)


def is_desktop_remote(path: str | os.PathLike[str]) -> bool:
    return "/.claude/remote/ccd-cli/" in str(path)


def _source_of(path: str) -> str | None:
    if is_desktop_bundle(path):
        return "desktop-bundle"
    return "desktop-remote" if is_desktop_remote(path) else None


def _newest(home: Path, pattern: str) -> list[Path]:
    candidates = [path for path in home.glob(pattern) if _is_executable(path)]
    return sorted(candidates, key=lambda path: path.stat().st_mtime, reverse=True)


def desktop_bundles(environment: Mapping[str, str]) -> list[Path]:
    """Return bundled desktop binaries, newest first."""
    return _newest(_home(environment), DESKTOP_BUNDLE_GLOB)


def desktop_remote_binaries(environment: Mapping[str, str]) -> list[Path]:
    """Return the desktop app's SSH-session CLIs on this host, newest first."""
    return _newest(_home(environment), DESKTOP_REMOTE_GLOB)


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
            # configuration fails visibly. Only a versioned desktop binary
            # replaced by an app update is rediscovered.
            if _is_executable(expanded) or _source_of(configured) is None:
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
        return Executable(found, _source_of(found) or "path")
    for location in STANDALONE_LOCATIONS:
        matches = _newest(home, location[2:]) if "*" in location else [Path(location.replace("~", str(home), 1))]
        if matches and _is_executable(matches[0]):
            return Executable(str(matches[0]), "standalone")
    session_binary = environment.get(EXECPATH_ENV, "").strip()
    if session_binary and _is_executable(Path(session_binary)):
        return Executable(session_binary, _source_of(session_binary) or "session")
    for source, binaries in (("desktop-remote", desktop_remote_binaries(environment)),
                             ("desktop-bundle", desktop_bundles(environment))):
        if binaries:
            return Executable(str(binaries[0]), source)
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


# Live delivery: `ltc wait` ---------------------------------------------------
#
# Claude Code wakes a live session when a background shell command exits. A
# Claude session runs `ltc wait` in the background; it prints the session's
# callback when one is queued and exits, which wakes that session. While a
# waiter is alive the daemon leaves the session's callbacks alone. Only Claude
# callbacks are ever touched here; other agents keep the normal daemon route.

LIVE_ACK_GRACE_SECONDS = 30 * 60.0
LIVE_WATCHER_DIR_NAME = "live-watchers"
# A channel server (claude_channel.py) holds the same lock under this prefix.
CHANNEL_WATCHER_PREFIX = "channel-"


def _session_of(request: Mapping[str, object]) -> str | None:
    target = request.get("target")
    if request.get("agent") != "claude" or not isinstance(target, dict) or target.get("kind") != "session":
        return None
    value = target.get("value")
    return value if isinstance(value, str) and value else None


def live_watcher_dir(session: str) -> Path:
    from . import cli

    digest = hashlib.sha256(f"session:{session}".encode("utf-8")).hexdigest()
    return cli.target_lock_dir() / LIVE_WATCHER_DIR_NAME / digest


def register_live_watcher(session: str, *, prefix: str = "") -> tuple[object, Path]:
    """Lock a uniquely named file; the OS releases it if the waiter dies.

    The lock is taken under a staging name and renamed into place, so the
    daemon never observes an unlocked live file.
    """
    from . import cli

    directory = live_watcher_dir(session)
    directory.mkdir(parents=True, exist_ok=True)
    name = f"{prefix}{os.getpid()}-{secrets.token_hex(6)}"
    lock = cli.acquire_path_lock(directory / f".{name}.staging", blocking=False)
    if lock is None:
        raise RuntimeError("could not lock a fresh live waiter file")
    handle, staging = lock
    final = directory / f"{name}.lock"
    os.replace(staging, final)
    return handle, final


def release_live_watcher(lock: tuple[object, Path]) -> None:
    from . import cli

    try:
        lock[1].unlink()
    except FileNotFoundError:
        pass
    cli.release_owner_lock(lock, remove=False)


def live_watcher_is_held(request: Mapping[str, object]) -> bool:
    """Daemon hook: whether a live waiter owns delivery for this Claude callback."""
    session = _session_of(request)
    return session is not None and _held(session, "")


def channel_is_held(session: str) -> bool:
    """Whether an `ltc claude` channel is delivering this session's callbacks."""
    return _held(session, CHANNEL_WATCHER_PREFIX)


def _held(session: str, prefix: str) -> bool:
    from . import cli

    directory = live_watcher_dir(session)
    if not directory.is_dir():
        return False
    held = False
    for path in directory.glob(f"{prefix}*.lock"):
        lock = cli.acquire_path_lock(path, blocking=False)
        if lock is None:
            held = True
        else:
            release_live_watcher(lock)  # Its waiter exited; names are never reused.
    return held


def _claim(root: Path, path: Path) -> str | None:
    """Move one callback through running/ and return the prompt to print."""
    from . import cli

    request_id = path.stem
    delivery_lock = cli.acquire_owner_lock(root, cli.delivery_lock_id(request_id), blocking=False)
    if delivery_lock is None:
        return None
    target_lock = None
    running: Path | None = None
    try:
        try:
            request = cli.load_request(path)
        except FileNotFoundError:
            return None
        target_lock = cli.acquire_target_lock(request, blocking=False)
        if target_lock is None:
            return None  # The daemon is delivering to this session right now.
        try:
            running = cli.move_request(path, root / "running")
        except FileNotFoundError:
            return None
        request = cli.load_request(running)
        if cli.ack_path(root, request_id).exists() or cli.is_canceled(root, request_id):
            return None  # The finally block returns it; the daemon finalizes it.
        payload = {"prompt": str(request["prompt"]), "callback_hook_path": str(cli.callback_hook_path()),
                   "request": request, "queue_dir": str(root)}
        prompt = cli.select_delivery_prompt(payload)
        now = time.time()
        request["live_delivered_at"] = now
        request["next_attempt_at"] = now + LIVE_ACK_GRACE_SECONDS
        request["last_deferred_reason"] = "delivered to the live session by ltc wait; awaiting ACK"
        cli.write_request(running, request)
        return prompt
    finally:
        if running is not None and running.exists():
            try:
                cli.move_request(running, root / "pending")
            except OSError as exc:
                print(f"ltc: warning: could not return callback {request_id} to pending: {exc}", file=sys.stderr)
        cli.release_owner_lock(target_lock, remove=False)
        cli.release_owner_lock(delivery_lock, remove=False)


def _matches(request: Mapping[str, object], session: str, task: str | None) -> bool:
    return (_session_of(request) == session
            and (task is None or request.get("managed_task_id") == task)
            and request.get("retain_target_lease") is not True)


def _task_already_acknowledged(root: Path, session: str, task: str) -> bool:
    for path in (root / "done").glob("*.json"):
        try:
            request = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(request, dict) and _matches(request, session, task):
            return True
    return False


def wait(args: argparse.Namespace) -> int:
    """`ltc wait`: print this Claude session's unacknowledged callback(s), waiting if none yet.

    A callback keeps being returned until it is ACKed. If the session closes,
    the OS releases the waiter's lock and the daemon delivers as usual.
    """
    from . import cli

    session = args.session or os.environ.get(SESSION_ENV, "").strip()
    if not session:
        print(f"ltc wait is only for Claude Code sessions: {SESSION_ENV} is unset. "
              "Other agents receive callbacks from the daemon and should not run it.", file=sys.stderr)
        return 2
    root = cli.queue_dir(args).expanduser().absolute()
    cli.ensure_daemon_dirs(root)
    try:
        if args.task and not cli.managed_task_path(root, args.task).exists():
            raise ValueError(f"unknown task {args.task} in {root}")
    except ValueError as exc:
        print(f"ltc wait: {exc}", file=sys.stderr)
        return 2
    if channel_is_held(session):
        print("ltc wait: this session was started with `ltc claude`; its callbacks arrive through the "
              "ltc channel, so no waiter is needed", file=sys.stderr)
        return 0
    deadline = time.monotonic() + args.timeout if args.timeout else None
    watcher = register_live_watcher(session)
    print(f"ltc wait: watching Claude Code session {session}", file=sys.stderr)
    try:
        while True:
            prompts = []
            for state in ("pending", "failed"):
                for path in sorted((root / state).glob("*.json")):
                    try:
                        request = json.loads(path.read_text(encoding="utf-8"))
                    except (OSError, ValueError):
                        continue
                    if (isinstance(request, dict) and _matches(request, session, args.task)
                            and not cli.ack_path(root, path.stem).exists()):
                        prompt = _claim(root, path)
                        if prompt is not None:
                            prompts.append(prompt)
            if prompts:
                print("\n\n".join(prompts), flush=True)
                return 0
            if args.task and _task_already_acknowledged(root, session, args.task):
                print(f"ltc wait: task {args.task}'s callback is already acknowledged", file=sys.stderr)
                return 0
            if deadline is not None and time.monotonic() >= deadline:
                print("ltc wait: timed out; the callback will still be delivered when it arrives", file=sys.stderr)
                return 3
            time.sleep(max(0.05, args.poll_interval))
    except KeyboardInterrupt:
        return 130
    finally:
        release_live_watcher(watcher)
