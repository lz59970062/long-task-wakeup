"""Push Claude Code callbacks into the open session through a channel.

Claude Code channels (research preview) let a local MCP server push events
into the running interactive session. ``ltc claude`` starts Claude with this
server as a development channel; the server lives exactly as long as that
window, holds the session's live-watcher lock while it can deliver, and
pushes each queued callback for the session as a ``<channel source="ltc">``
event. The daemon already defers to that lock, so a reopened window never
races a headless resume. A pushed callback still needs ``ltc ack``.

Claude Code never acknowledges channel events and silently drops them when
the channel is not registered, so the server only takes over when its parent
was started with the channel flag, and steps aside for the daemon if a pushed
callback stays unacknowledged past the ACK grace.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Optional

from . import claude_code
from .runtime import worker_command

SERVER_NAME = "ltc"
CHANNEL_FLAGS = ("--dangerously-load-development-channels", "--channels")
# Claude Code does not register a channel that negotiates 2026-07-28 or later.
PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
INSTRUCTIONS = (
    'Long Task Callback (ltc) results arrive as <channel source="ltc" callback_id="..." task_id="...">. '
    "Each one reports a background task launched from this session finishing. Inspect the files it names, "
    "then ACK with the command it gives. This channel replaces `ltc wait` in this session: "
    "do not start `ltc wait` here, or the same callback arrives twice."
)


def parent_command_line(pid: int) -> list[str]:
    try:
        return Path(f"/proc/{pid}/cmdline").read_bytes().decode("utf-8", "replace").split("\0")
    except OSError:
        pass
    try:
        output = subprocess.run(["ps", "-o", "command=", "-p", str(pid)], capture_output=True,
                                text=True, timeout=5, check=False).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    return output.split()


def channel_enabled(argv: list[str]) -> bool:
    """Whether Claude was launched with this server opted in as a channel."""
    for index, value in enumerate(argv):
        for flag in CHANNEL_FLAGS:
            if value == flag:
                names = argv[index + 1:]
            elif value.startswith(flag + "="):
                names = [value.split("=", 1)[1]]
            else:
                continue
            for name in names:
                if name.startswith("-"):
                    break
                if f"server:{SERVER_NAME}" in name.split(","):
                    return True
    return False


def session_of_process(pid: int, environment: Mapping[str, str]) -> Optional[str]:
    """Current session of the Claude process ``pid``; it changes on /clear or /resume."""
    path = claude_code.config_dir(environment) / "sessions" / f"{pid}.json"
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    session = record.get("sessionId") if isinstance(record, dict) else None
    if isinstance(session, str) and claude_code.SESSION_ID_PATTERN.fullmatch(session):
        return session
    return None


class ChannelServer:
    def __init__(self, root: Path, *, parent_pid: int, environment: Mapping[str, str],
                 poll_interval: float, ack_grace: float, out=None) -> None:
        self.root = root
        self.parent_pid = parent_pid
        self.environment = environment
        self.poll_interval = poll_interval
        self.ack_grace = ack_grace
        self.out = out or sys.stdout
        self.write_lock = threading.Lock()
        self.initialized = threading.Event()
        self.closed = threading.Event()
        self.enabled = channel_enabled(parent_command_line(parent_pid))
        self.session: Optional[str] = None
        self.watcher: Optional[tuple[object, Path]] = None
        self.pushed: dict[str, float] = {}
        self.stepped_aside = False

    # MCP over stdio: one JSON-RPC message per line ---------------------------

    def send(self, message: dict[str, object]) -> None:
        with self.write_lock:
            self.out.write(json.dumps(message, separators=(",", ":")) + "\n")
            self.out.flush()

    def handle(self, message: object) -> None:
        if not isinstance(message, dict) or "method" not in message:
            return
        method = message["method"]
        if method == "notifications/initialized":
            self.initialized.set()
        if "id" not in message:
            return
        if method == "initialize":
            params = message.get("params") or {}
            requested = params.get("protocolVersion") if isinstance(params, dict) else None
            version = requested if requested in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0]
            result: dict[str, object] = {
                "protocolVersion": version,
                "capabilities": {"experimental": {"claude/channel": {}}},
                "serverInfo": {"name": SERVER_NAME, "version": "1"},
                "instructions": INSTRUCTIONS,
            }
            self.send({"jsonrpc": "2.0", "id": message["id"], "result": result})
        elif method == "ping":
            self.send({"jsonrpc": "2.0", "id": message["id"], "result": {}})
        else:
            self.send({"jsonrpc": "2.0", "id": message["id"],
                       "error": {"code": -32601, "message": f"method not found: {method}"}})

    def read_stdin(self, stream) -> None:
        try:
            for line in stream:
                line = line.strip()
                if not line:
                    continue
                try:
                    self.handle(json.loads(line))
                except ValueError:
                    continue
        finally:
            self.closed.set()  # Claude closed the pipe: the window is gone.

    # Delivery ------------------------------------------------------------------

    def log(self, text: str) -> None:
        print(f"ltc channel: {text}", file=sys.stderr, flush=True)

    def release(self) -> None:
        if self.watcher is not None:
            claude_code.release_live_watcher(self.watcher)
            self.watcher = None

    def follow_session(self) -> None:
        session = session_of_process(self.parent_pid, self.environment)
        if session == self.session:
            return
        self.release()
        self.session = session
        self.pushed.clear()
        if session is not None:
            self.watcher = claude_code.register_live_watcher(session, prefix=claude_code.CHANNEL_WATCHER_PREFIX)
            self.log(f"delivering callbacks for Claude Code session {session}")

    def step_aside(self, callback_id: str) -> None:
        self.release()
        self.stepped_aside = True
        self.log(f"callback {callback_id} is still unacknowledged after the channel push; "
                 "the channel may not be registered, so the daemon takes over delivery")

    def poll(self) -> None:
        from . import cli

        if self.stepped_aside:
            return
        self.follow_session()
        if self.session is None:
            return
        now = time.time()
        for state in ("pending", "failed"):
            for path in sorted((self.root / state).glob("*.json")):
                callback_id = path.stem
                try:
                    request = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                if (not isinstance(request, dict) or not claude_code._matches(request, self.session, None)
                        or cli.ack_path(self.root, callback_id).exists()):
                    continue
                pushed_at = self.pushed.get(callback_id)
                if pushed_at is not None:
                    if now - pushed_at >= self.ack_grace:
                        self.step_aside(callback_id)
                        return
                    continue
                prompt = claude_code._claim(self.root, path)
                if prompt is None:
                    continue
                meta = {"callback_id": callback_id}
                if isinstance(request.get("managed_task_id"), str):
                    meta["task_id"] = request["managed_task_id"]
                self.send({"jsonrpc": "2.0", "method": "notifications/claude/channel",
                           "params": {"content": prompt, "meta": meta}})
                self.pushed[callback_id] = now
                self.log(f"pushed callback {callback_id}")

    def run(self, stream) -> int:
        reader = threading.Thread(target=self.read_stdin, args=(stream,), daemon=True)
        reader.start()
        if not self.enabled:
            # Loaded as a plain MCP server: events would be dropped, so never
            # claim callbacks or hold the lock the daemon defers to.
            self.log("not opted in as a channel; leaving delivery to `ltc wait` and the daemon")
            self.closed.wait()
            return 0
        try:
            while not self.closed.is_set():
                if self.initialized.is_set():
                    try:
                        self.poll()
                    except OSError as exc:
                        self.log(f"poll failed: {exc}")
                self.closed.wait(self.poll_interval)
        finally:
            self.release()
        return 0


def serve(args: argparse.Namespace) -> int:
    """Hidden `ltc _claude-channel`: started by Claude Code, never by hand."""
    from . import cli

    root = cli.queue_dir(args).expanduser().absolute()
    cli.ensure_daemon_dirs(root)
    server = ChannelServer(root, parent_pid=args.parent_pid or os.getppid(), environment=os.environ,
                           poll_interval=args.poll_interval, ack_grace=claude_code.LIVE_ACK_GRACE_SECONDS)
    return server.run(sys.stdin)


def mcp_config(root: Path, environment: Mapping[str, str]) -> str:
    """Server entry for --mcp-config; it must find the same locks as the daemon."""
    from . import cli

    command = worker_command("_claude-channel", "--queue-dir", str(root))
    server: dict[str, object] = {"command": command[0], "args": command[1:]}
    passed = {name: environment[name] for name in (cli.TARGET_LOCK_DIR_ENV, "CLAUDE_CONFIG_DIR", "CODEX_HOME")
              if environment.get(name)}
    if passed:
        server["env"] = passed  # MCP servers may not inherit Claude's whole environment.
    return json.dumps({"mcpServers": {SERVER_NAME: server}})


def launch_command(claude: str, root: Path, claude_args: list[str],
                   environment: Mapping[str, str] = os.environ) -> list[str]:
    return [claude, "--mcp-config", mcp_config(root, environment),
            "--dangerously-load-development-channels", f"server:{SERVER_NAME}", *claude_args]


def launch(args: argparse.Namespace) -> int:
    """`ltc claude [claude args]`: interactive Claude Code with the LTC channel."""
    from . import cli

    root = cli.queue_dir(args).expanduser().absolute()
    found = claude_code.discover_executable(os.environ)
    if found is None:
        print("ltc claude: Claude Code was not found; set LONG_TASK_WAKEUP_CLAUDE_BIN", file=sys.stderr)
        return 2
    claude_args = list(args.claude_args)
    if claude_args[:1] == ["--"]:
        claude_args = claude_args[1:]
    command = launch_command(found.path, root, claude_args)
    if os.name == "nt":
        return subprocess.call(command)
    os.execv(command[0], command)
    return 0  # pragma: no cover
