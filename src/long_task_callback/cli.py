from __future__ import annotations

import argparse
import base64
from email.message import EmailMessage
import hashlib
import importlib.resources as resources
import json
import os
import re
import secrets
import signal
import shlex
import shutil
import socket
import struct
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

try:
    import fcntl
except ImportError:  # pragma: no cover - the durable run lifecycle is POSIX-only
    fcntl = None

from . import __version__

DEFAULT_APPROVALS_REVIEWER = "auto_review"
DEFAULT_APPROVAL_POLICY = "on-request"
DEFAULT_SANDBOX_MODE = "workspace-write"
DEFAULT_RETRIES = 3
DEFAULT_RETRY_DELAY = 30.0
DEFAULT_RETRY_BACKOFF = 2.0
DEFAULT_RESUME_TIMEOUT = 3600.0
DEFAULT_GOAL_IDLE_SECONDS = 3 * 60 * 60
DEFAULT_BLOCKED_EMAIL_SECONDS = 12 * 60 * 60
DEFAULT_SUPERVISOR_CONF_DIR = "/etc/supervisor/conf.d"
RELOAD_PROTOCOL_VERSION = 1
CODEX_THREAD_ID_ENV = "CODEX_THREAD_ID"
SHELL_HOOK_BEGIN = "# >>> codex-long-task-wakeup pending status >>>"
SHELL_HOOK_END = "# <<< codex-long-task-wakeup pending status <<<"
ACTIVE_STATE = "active"
LOCKS_STATE = "locks"
TARGET_LOCK_DIR_ENV = "CODEX_LONG_TASK_WAKEUP_TARGET_LOCK_DIR"
PROXY_ENV_FILE_ENV = "CODEX_LONG_TASK_WAKEUP_PROXY_ENV_FILE"
DESKTOP_APP_SERVER_ENV = "CODEX_LONG_TASK_WAKEUP_DESKTOP_APP_SERVER"
APP_SERVER_SOCKET_ENV = "CODEX_LONG_TASK_WAKEUP_APP_SERVER_SOCKET"
ALLOW_APP_SERVER_SOCKET_OVERRIDE_ENV = "CODEX_LONG_TASK_WAKEUP_ALLOW_APP_SERVER_SOCKET_OVERRIDE"
PROXY_ENV_NAMES = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "NO_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
    "no_proxy",
)


@dataclass
class BackgroundResume:
    process: subprocess.Popen[str]
    target_key: str
    deadline: float
    request_id: str


_BACKGROUND_RESUMES: dict[int, BackgroundResume] = {}


class TargetLeaseUnavailable(RuntimeError):
    """Another queue is already delivering a callback to the same target."""


def build_prompt(
    args: argparse.Namespace,
    duration: float | None = None,
    acknowledgement: str | None = None,
) -> str:
    lines = [
        "[long-task-callback]",
        "A long-running task explicitly called back into Codex.",
        f"Callback time: {datetime.now().astimezone().isoformat(timespec='seconds')}",
        f"Task: {args.task}",
        f"Working directory: {args.cwd}",
    ]
    if args.command:
        lines.append(f"Command: {args.command}")
    if duration is not None:
        lines.append(f"Duration: {duration:.1f}s")
    if args.exit_code is not None:
        lines.append(f"Exit code: {args.exit_code}")
    if args.message:
        lines.extend(["", "Callback message:", args.message])

    lines.extend(
        [
            "",
            "Please inspect the result and any relevant artifacts.",
            "Decide whether the original goal is complete, blocked, or needs another action.",
            "Continue if the next step is clear and safe; otherwise ask the user one concise question.",
        ]
    )
    if acknowledgement:
        lines.extend(["", build_acknowledgement_text(acknowledgement)])
    return "\n".join(lines)


def truthy_env(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def queue_dir(args: argparse.Namespace | None = None) -> Path:
    explicit = getattr(args, "queue_dir", None) if args is not None else None
    path = explicit or os.environ.get("CODEX_LONG_TASK_WAKEUP_QUEUE_DIR")
    if path:
        return Path(path).expanduser()
    return codex_home() / "long-task-wakeup" / "queue"


def ack_path(root: Path, request_id: str) -> Path:
    return root / "acks" / f"{request_id}.json"


def request_path(root: Path, state: str, request_id: str) -> Path:
    return root / state / f"{request_id}.json"


def codex_command() -> str:
    return os.environ.get("CODEX_LONG_TASK_WAKEUP_CODEX_BIN", "codex")


def systemd_user_dir() -> Path:
    config_home = os.environ.get("XDG_CONFIG_HOME")
    if config_home:
        return Path(config_home).expanduser() / "systemd" / "user"
    return Path("~/.config/systemd/user").expanduser()


def systemd_quote(value: str) -> str:
    if any(char in value for char in "\x00\n\r"):
        raise ValueError("systemd values must not contain NUL, carriage return, or newline characters")
    if value and all(char not in value for char in " \t\n\"'\\"):
        return value
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def supervisor_quote(value: str) -> str:
    if any(char in value for char in "\x00\n\r"):
        raise ValueError("supervisor values must not contain NUL, carriage return, or newline characters")
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%") + '"'


def service_name(name: str) -> str:
    return name if name.endswith(".service") else f"{name}.service"


def program_name(name: str) -> str:
    return name.removesuffix(".service")


def console_script_path() -> str:
    command = shutil.which("codex-long-task-wakeup")
    if command:
        return command
    return sys.argv[0]


def codex_bin_path(args: argparse.Namespace) -> str:
    if args.codex_bin:
        return str(Path(args.codex_bin).expanduser())
    command = shutil.which("codex")
    return command or "codex"


def systemd_service_text(args: argparse.Namespace) -> str:
    command = daemon_command(args)
    exec_start = " ".join(systemd_quote(part) for part in command)
    codex_bin = codex_bin_path(args)
    path = args.path or os.environ.get("PATH", "")
    return "\n".join(
        [
            "[Unit]",
            "Description=Codex Long Task Wakeup Daemon",
            "Documentation=https://github.com/lz59970062/long-task-wakeup",
            "After=network-online.target",
            "Wants=network-online.target",
            "",
            "[Service]",
            "Type=simple",
            f"ExecStart={exec_start}",
            "ExecReload=/bin/kill -HUP $MAINPID",
            "Restart=always",
            f"RestartSec={args.restart_sec}",
            "UMask=0077",
            "NoNewPrivileges=yes",
            "Environment=PYTHONUNBUFFERED=1",
            f"Environment={systemd_quote(f'PATH={path}')}",
            f"Environment={systemd_quote(f'CODEX_LONG_TASK_WAKEUP_CODEX_BIN={codex_bin}')}",
            f"Environment={systemd_quote(f'{DESKTOP_APP_SERVER_ENV}=1')}",
            f"Environment={systemd_quote(f'{PROXY_ENV_FILE_ENV}={service_proxy_env_path()}')}",
            f"EnvironmentFile=-{service_proxy_env_path()}",
            "",
            "[Install]",
            "WantedBy=default.target",
            "",
        ]
    )


def daemon_command(args: argparse.Namespace) -> list[str]:
    command = [
        getattr(args, "exec_start", None) or console_script_path(),
        "daemon",
        "--interval",
        str(args.interval),
        "--retries",
        str(args.retries),
        "--retry-delay",
        str(args.retry_delay),
        "--retry-backoff",
        str(args.retry_backoff),
        "--resume-timeout",
        str(getattr(args, "resume_timeout", DEFAULT_RESUME_TIMEOUT)),
    ]
    if args.queue_dir:
        command.extend(["--queue-dir", str(Path(args.queue_dir).expanduser())])
    return command


def running_in_container() -> bool:
    if Path("/.dockerenv").exists():
        return True
    container = os.environ.get("container", "").strip().lower()
    return container in {"docker", "podman", "oci", "containerd"}


def supervisor_conf_dir() -> Path:
    path = os.environ.get("CODEX_LONG_TASK_WAKEUP_SUPERVISOR_CONF_DIR", DEFAULT_SUPERVISOR_CONF_DIR)
    return Path(path).expanduser()


def supervisor_config_path(args: argparse.Namespace) -> Path:
    return supervisor_conf_dir() / f"{program_name(args.name)}.conf"


def supervisor_config_text(args: argparse.Namespace) -> str:
    root = daemon_state_dir()
    log_path = root / "supervisor.log"
    command = " ".join(shlex.quote(part) for part in daemon_command(args))
    environment = ",".join(
        f"{name}={supervisor_quote(value)}" for name, value in daemon_environment(args, include_proxy_values=False).items()
    )
    return "\n".join(
        [
            f"[program:{program_name(args.name)}]",
            f"command={command}",
            "autostart=true",
            "autorestart=true",
            "startsecs=3",
            "startretries=10",
            "stopsignal=TERM",
            "stopasgroup=true",
            "killasgroup=true",
            "redirect_stderr=true",
            f"stdout_logfile={log_path}",
            "stdout_logfile_maxbytes=10MB",
            "stdout_logfile_backups=3",
            f"environment={environment}",
            "",
        ]
    )


def resolve_target(args: argparse.Namespace) -> tuple[dict[str, str], str]:
    if args.session:
        return {"kind": "session", "value": args.session}, "--session"
    if args.last:
        return {"kind": "last"}, "--last"

    session = os.environ.get(CODEX_THREAD_ID_ENV, "").strip()
    if session:
        return {"kind": "session", "value": session}, CODEX_THREAD_ID_ENV

    raise SystemExit(
        f"Cannot determine the callback session: {CODEX_THREAD_ID_ENV} is unset. "
        "Run from Codex or pass --session <id>; use --last only as an explicit unsafe fallback."
    )


def bind_target(args: argparse.Namespace) -> tuple[dict[str, str], str]:
    cached_target = getattr(args, "_callback_target", None)
    cached_source = getattr(args, "_callback_target_source", None)
    if isinstance(cached_target, dict) and isinstance(cached_source, str):
        return cached_target, cached_source

    target, source = resolve_target(args)
    args._callback_target = target
    args._callback_target_source = source
    if target["kind"] == "session":
        print(
            f"codex-long-task-wakeup: callback bound to session {target['value']} via {source}",
            file=sys.stderr,
        )
    else:
        print(
            "codex-long-task-wakeup: warning: --last is not session-safe and may wake an unrelated active thread",
            file=sys.stderr,
        )
    return target, source


def routing_text(request: dict[str, object]) -> str:
    target = request["target"]
    source = request["target_source"]
    if isinstance(target, dict) and target.get("kind") == "session":
        return "\n".join(
            [
                "Callback routing:",
                f"- Bound session: {target['value']}",
                f"- Binding source: {source}",
                "- Resume only this session; never redirect this callback to --last or another active thread.",
            ]
        )
    return "\n".join(
        [
            "Callback routing warning:",
            "- Target: most recently active Codex session (--last)",
            "- This unsafe fallback was explicitly requested and may reach an unrelated thread.",
        ]
    )


def attach_routing_text(prompt: str, request: dict[str, object]) -> str:
    return f"{prompt}\n\n{routing_text(request)}"


def make_request(args: argparse.Namespace, prompt: str) -> dict[str, object]:
    target, target_source = bind_target(args)
    request = {
        "version": 1,
        "id": uuid.uuid4().hex,
        "created_at": time.time(),
        "cwd": args.cwd,
        "target": target,
        "target_source": target_source,
        "prompt": prompt,
        "approvals_reviewer": getattr(args, "approvals_reviewer", None) or DEFAULT_APPROVALS_REVIEWER,
        "approval_policy": getattr(args, "approval_policy", None) or DEFAULT_APPROVAL_POLICY,
        "sandbox_mode": getattr(args, "sandbox_mode", None) or DEFAULT_SANDBOX_MODE,
    }
    goal_id = getattr(args, "goal_id", None)
    if goal_id:
        request["goal_id"] = goal_id
    return request


def prepare_request_for_queue(root: Path, request: dict[str, object], prompt: str) -> dict[str, object]:
    request_id = str(request["id"])
    request["queue_dir"] = str(root)
    request["lifecycle_state"] = "pending"
    acknowledgement = " ".join(
        shlex.quote(part)
        for part in [
            console_script_path(),
            "ack",
            "--queue-dir",
            str(root),
            "--id",
            request_id,
        ]
    )
    routed_prompt = attach_routing_text(prompt, request)
    request["prompt"] = f"{routed_prompt}\n\n{build_acknowledgement_text(acknowledgement)}"
    return request


def enqueue_existing_request(root: Path, request: dict[str, object], prompt: str) -> int:
    ensure_daemon_dirs(root)
    request = prepare_request_for_queue(root, request, prompt)
    request_id = str(request["id"])
    target = request_path(root, "pending", request_id)
    goal_lock: tuple[object, Path] | None = None
    try:
        goal_id = request.get("goal_id")
        goal: dict[str, object] | None = None
        if not request.get("goal_reminder") and goal_id is not None:
            if not isinstance(goal_id, str):
                raise ValueError("callback goal id must be a string")
            goal_path(root, goal_id)
            goal_lock = acquire_owner_lock(root, goal_lock_id(goal_id), blocking=True)
            goal = load_goal(root, goal_id)
            if goal.get("state") != "active":
                raise ValueError(f"goal {goal_id} is not active")
        write_request(target, request)
        if goal is not None:
            goal["last_external_callback_at"] = float(request.get("created_at", time.time()))
            goal.pop("last_reminder_at", None)
            write_goal(root, goal)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"codex-long-task-wakeup: warning: failed to enqueue callback: {exc}", file=sys.stderr)
        return 1
    finally:
        if goal_lock is not None:
            release_owner_lock(goal_lock, remove=False)

    print(f"codex-long-task-wakeup: queued callback {request_id} in {root}", file=sys.stderr)
    return 0


def enqueue_request(args: argparse.Namespace, prompt: str) -> int:
    root = queue_dir(args)
    request = make_request(args, prompt)
    return enqueue_existing_request(root, request, prompt)


def should_enqueue(args: argparse.Namespace) -> bool:
    return bool(getattr(args, "via_daemon", False)) or truthy_env("CODEX_LONG_TASK_WAKEUP_VIA_DAEMON")


def resume_command(request: dict[str, object]) -> list[str]:
    cmd = [codex_command(), "exec", "resume", "--all"]
    approvals_reviewer = request.get("approvals_reviewer", DEFAULT_APPROVALS_REVIEWER)
    if isinstance(approvals_reviewer, str) and approvals_reviewer:
        cmd.extend(["-c", f"approvals_reviewer={json.dumps(approvals_reviewer)}"])
    approval_policy = request.get("approval_policy", DEFAULT_APPROVAL_POLICY)
    if isinstance(approval_policy, str) and approval_policy:
        cmd.extend(["-c", f"approval_policy={json.dumps(approval_policy)}"])
    sandbox_mode = request.get("sandbox_mode", DEFAULT_SANDBOX_MODE)
    if isinstance(sandbox_mode, str) and sandbox_mode:
        cmd.extend(["-c", f"sandbox_mode={json.dumps(sandbox_mode)}"])
    queue_root = request.get("queue_dir")
    if isinstance(queue_root, str) and queue_root:
        cmd.extend(["-c", f"sandbox_workspace_write.writable_roots=[{json.dumps(queue_root)}]"])
    target = request.get("target")
    if not isinstance(target, dict):
        raise ValueError("request target must be an object")
    kind = target.get("kind")
    if kind == "session":
        value = target.get("value")
        if not isinstance(value, str) or not value:
            raise ValueError("session target requires a non-empty value")
        cmd.append(value)
    elif kind == "last":
        cmd.append("--last")
    else:
        raise ValueError("request target kind must be 'session' or 'last'")
    cmd.append("-")
    return cmd


def resume_codex(args: argparse.Namespace, prompt: str) -> int:
    if should_enqueue(args):
        return enqueue_request(args, prompt)

    request = make_request(args, prompt)
    routed_prompt = attach_routing_text(prompt, request)
    if args.dry_run:
        print(routed_prompt)
        return 0
    cmd = resume_command(request)

    try:
        result = subprocess.run(
            cmd,
            input=routed_prompt,
            text=True,
            cwd=args.cwd,
            check=False,
        )
    except OSError as exc:
        print(f"codex-long-task-wakeup: warning: failed to run Codex callback: {exc}", file=sys.stderr)
        return 127

    if result.returncode != 0:
        print(
            f"codex-long-task-wakeup: warning: Codex callback exited with {result.returncode}",
            file=sys.stderr,
        )
    return result.returncode


def ensure_daemon_dirs(root: Path) -> None:
    root_existed = root.exists()
    root.mkdir(parents=True, exist_ok=True)
    if not root_existed:
        fsync_directory(root.parent)
    created = False
    for name in (ACTIVE_STATE, LOCKS_STATE, "pending", "running", "done", "failed", "canceled", "acks", "goals"):
        path = root / name
        if path.exists():
            continue
        path.mkdir(parents=True, exist_ok=True)
        created = True
    if created:
        fsync_directory(root)


def goal_path(root: Path, goal_id: str) -> Path:
    if not goal_id or any(character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_" for character in goal_id):
        raise ValueError("goal id must contain only letters, numbers, '-' or '_'")
    return root / "goals" / f"{goal_id}.json"


def load_goal(root: Path, goal_id: str) -> dict[str, object]:
    path = goal_path(root, goal_id)
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("id") != goal_id:
        raise ValueError(f"invalid goal record {goal_id}")
    return data


def write_goal(root: Path, goal: dict[str, object]) -> None:
    goal_id = goal.get("id")
    if not isinstance(goal_id, str):
        raise ValueError("goal requires an id")
    write_request(goal_path(root, goal_id), goal)


def goal_lock_id(goal_id: str) -> str:
    return f"goal-{goal_id}"


def clear_blocked_goal_email(goal: dict[str, object]) -> None:
    for field in (
        "blocked_email_to",
        "blocked_email_after_seconds",
        "blocked_email_attempted_at",
        "blocked_email_attempts",
        "blocked_email_next_attempt_at",
        "blocked_email_result",
        "blocked_email_exhausted_at",
    ):
        goal.pop(field, None)


def is_single_email_recipient(value: str) -> bool:
    return bool(re.fullmatch(r"[^\s@,;<>]+@[^\s@,;<>]+", value))


def queued_goal_reminder_exists(root: Path, goal_id: str) -> bool:
    for state in (ACTIVE_STATE, "pending", "running"):
        for path in (root / state).glob("*.json"):
            try:
                request = load_request(path)
            except (OSError, ValueError, json.JSONDecodeError):
                continue
            if request.get("goal_id") == goal_id and request.get("goal_reminder"):
                return True
    return False


def latest_queued_goal_callback_at(root: Path, goal_id: str) -> float | None:
    latest: float | None = None
    for state in (ACTIVE_STATE, "pending", "running", "done", "failed", "canceled"):
        for path in (root / state).glob("*.json"):
            try:
                request = load_request(path)
                created_at = float(request["created_at"])
            except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
                continue
            if request.get("goal_id") == goal_id and not request.get("goal_reminder"):
                latest = created_at if latest is None else max(latest, created_at)
    return latest


def update_goal_from_callback(root: Path, request: dict[str, object]) -> None:
    if request.get("goal_reminder"):
        return
    goal_id = request.get("goal_id")
    if not isinstance(goal_id, str):
        return
    lock = acquire_owner_lock(root, goal_lock_id(goal_id), blocking=True)
    try:
        goal = load_goal(root, goal_id)
        if goal.get("state") != "active":
            return
        goal["last_external_callback_at"] = float(request.get("created_at", time.time()))
        goal.pop("last_reminder_at", None)
        write_goal(root, goal)
    finally:
        release_owner_lock(lock, remove=False)


def load_request(path: Path) -> dict[str, object]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("request must be a JSON object")
    if data.get("version") != 1:
        raise ValueError("unsupported request version")
    if not isinstance(data.get("id"), str) or data["id"] != path.stem:
        raise ValueError("request id must match its filename")
    if not isinstance(data.get("cwd"), str):
        raise ValueError("request cwd must be a string")
    if not isinstance(data.get("prompt"), str):
        raise ValueError("request prompt must be a string")
    resume_command(data)
    return data


def build_acknowledgement_text(command: str) -> str:
    return "\n".join(
        [
            "Callback acknowledgement:",
            "After you have successfully resumed this callback and inspected the relevant result,",
            "mark the callback as received by running this command:",
            command,
            "This resume is configured to make the callback queue writable and to use automatic approval review when the Codex CLI supports it.",
            "The wakeup daemon will retry this callback until the acknowledgement marker exists or retries are exhausted.",
        ]
    )


def next_attempt_at(path: Path) -> float:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return 0.0
    value = data.get("next_attempt_at") if isinstance(data, dict) else None
    return float(value) if isinstance(value, (int, float)) else 0.0


def callback_target_key(request: dict[str, object]) -> str | None:
    target = request.get("target")
    if not isinstance(target, dict):
        return None
    kind = target.get("kind")
    value = target.get("value")
    if kind == "session" and isinstance(value, str) and value:
        return f"session:{value}"
    if kind == "last":
        return "last"
    return None


def target_lock_dir() -> Path:
    override = os.environ.get(TARGET_LOCK_DIR_ENV)
    if override:
        return Path(override).expanduser()
    return codex_home() / "long-task-wakeup" / "target-locks"


def target_lock_path(request: dict[str, object]) -> Path | None:
    key = callback_target_key(request)
    if key is None:
        return None
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
    return target_lock_dir() / f"{digest}.lock"


def retained_target_lease_path(request: dict[str, object]) -> Path | None:
    path = target_lock_path(request)
    if path is None:
        return None
    return path.with_suffix(".retained.json")


def retained_target_lease_lock_path(request: dict[str, object]) -> Path | None:
    path = target_lock_path(request)
    if path is None:
        return None
    return path.with_suffix(".retained.lock")


def acquire_retained_target_lease_lock(
    request: dict[str, object],
    *,
    blocking: bool,
) -> tuple[object, Path] | None:
    path = retained_target_lease_lock_path(request)
    if path is None:
        return None
    return acquire_path_lock(path, blocking=blocking)


def retained_target_lease_is_held(request: dict[str, object]) -> bool:
    path = retained_target_lease_path(request)
    if path is None or not path.exists():
        return False
    lock = acquire_retained_target_lease_lock(request, blocking=False)
    if lock is None:
        return True
    try:
        if not path.exists():
            return False
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return True
        if not isinstance(record, dict):
            return True
        request_id = record.get("request_id")
        queue_root = record.get("queue_dir")
        if not isinstance(request_id, str) or not isinstance(queue_root, str) or not queue_root:
            return True
        root = Path(queue_root)
        if not ack_path(root, request_id).exists() and not request_path(root, "canceled", request_id).exists():
            return True
        try:
            path.unlink()
            fsync_directory(path.parent)
        except OSError:
            return True
        return False
    finally:
        release_owner_lock(lock, remove=False)


def retain_target_lease(root: Path, request: dict[str, object]) -> None:
    """Persist a cross-queue recovery lease while the target flock is held."""
    path = retained_target_lease_path(request)
    request_id = request.get("id")
    if path is None or not isinstance(request_id, str) or not request_id:
        raise ValueError("manual recovery requires a session target and request id")
    record = {
        "version": 1,
        "request_id": request_id,
        "queue_dir": str(root.resolve()),
        "recorded_at": time.time(),
    }
    lock = acquire_retained_target_lease_lock(request, blocking=True)
    if lock is None:  # pragma: no cover - blocking lock acquisition always succeeds
        raise RuntimeError(f"could not lock retained target lease: {path}")
    try:
        if path.exists():
            try:
                existing = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise RuntimeError(f"retained target lease is unreadable: {path}") from exc
            if not isinstance(existing, dict) or (
                existing.get("request_id") != request_id
                or existing.get("queue_dir") != str(root.resolve())
            ):
                raise RuntimeError(f"retained target lease already exists: {path}")
            return
        write_request(path, record)
    finally:
        release_owner_lock(lock, remove=False)


def release_retained_target_lease(root: Path, request: dict[str, object]) -> None:
    """Release only the cross-queue recovery lease owned by this callback."""
    path = retained_target_lease_path(request)
    request_id = request.get("id")
    if path is None or not isinstance(request_id, str):
        return
    lock = acquire_retained_target_lease_lock(request, blocking=True)
    if lock is None:  # pragma: no cover - blocking lock acquisition always succeeds
        raise RuntimeError(f"could not lock retained target lease: {path}")
    try:
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"retained target lease is unreadable: {path}") from exc
        if not isinstance(record, dict):
            raise RuntimeError(f"retained target lease is malformed: {path}")
        if record.get("request_id") != request_id or record.get("queue_dir") != str(root.resolve()):
            return
        path.unlink()
        fsync_directory(path.parent)
    finally:
        release_owner_lock(lock, remove=False)


def acquire_path_lock(path: Path, *, blocking: bool) -> tuple[object, Path] | None:
    if fcntl is None:  # pragma: no cover - supported deployments are POSIX
        raise RuntimeError("durable callback ownership requires POSIX flock support")
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+", encoding="utf-8")
    os.set_inheritable(handle.fileno(), False)
    operation = fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB)
    try:
        fcntl.flock(handle.fileno(), operation)
    except BlockingIOError:
        handle.close()
        return None
    return handle, path


def acquire_target_lock(
    request: dict[str, object],
    *,
    blocking: bool,
) -> tuple[object, Path] | None:
    path = target_lock_path(request)
    if path is None:
        return None
    return acquire_path_lock(path, blocking=blocking)


def target_lock_is_held(request: dict[str, object]) -> bool:
    path = target_lock_path(request)
    if path is None:
        return False
    if not path.exists():
        return False
    lock = acquire_path_lock(path, blocking=False)
    if lock is None:
        return True
    release_owner_lock(lock, remove=False)
    return False


def reap_background_resumes() -> None:
    now = time.monotonic()
    for pid, delivery in list(_BACKGROUND_RESUMES.items()):
        if delivery.process.poll() is not None:
            finish_delivery_worker(delivery.process)
            _BACKGROUND_RESUMES.pop(pid, None)
            continue
        if now >= delivery.deadline:
            print(
                f"codex-long-task-wakeup: warning: acknowledged resume {delivery.request_id} "
                "exceeded its timeout; terminating the resume process",
                file=sys.stderr,
            )
            stop_resume_process(delivery.process)
            _BACKGROUND_RESUMES.pop(pid, None)


def target_has_live_resume(root: Path, request: dict[str, object]) -> bool:
    reap_background_resumes()
    key = callback_target_key(request)
    if key is None:
        return False
    if target_lock_is_held(request):
        return True
    if retained_target_lease_is_held(request):
        return True
    if any(delivery.target_key == key for delivery in _BACKGROUND_RESUMES.values()):
        return True
    for state in ("running", "canceled", "failed"):
        for path in (root / state).glob("*.json"):
            try:
                live_request = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            if (
                isinstance(live_request, dict)
                and callback_target_key(live_request) == key
                and (
                    (state == "failed" and live_request.get("retain_target_lease") is True)
                    or delivery_lock_is_held(root, path.stem)
                )
            ):
                return True
    return False


def select_pending(root: Path, now: float) -> Path | None:
    for path in sorted((root / "pending").glob("*.json")):
        if next_attempt_at(path) > now:
            continue
        try:
            request = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return path
        if isinstance(request, dict) and target_has_live_resume(root, request):
            continue
        return path
    return None


def retry_delay(args: argparse.Namespace, attempt: int) -> float:
    delay = max(0.0, float(getattr(args, "retry_delay", DEFAULT_RETRY_DELAY)))
    backoff = max(1.0, float(getattr(args, "retry_backoff", DEFAULT_RETRY_BACKOFF)))
    return delay * (backoff ** max(0, attempt - 1))


def terminate_process_group(process: subprocess.Popen[str], grace: float = 5.0) -> None:
    if process.poll() is not None:
        process.wait()
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        process.wait()
        return
    try:
        process.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait()


class AppServerProtocolError(RuntimeError):
    """The local Codex App Server did not accept a desktop callback."""


class AppServerTimeout(AppServerProtocolError):
    """The local Codex App Server did not respond before the deadline."""


class AppServerRpcError(AppServerProtocolError):
    """The local Codex App Server explicitly rejected a JSON-RPC request."""


def desktop_app_server_socket(request: dict[str, object]) -> Path | None:
    target = request.get("target")
    if (
        not truthy_env(DESKTOP_APP_SERVER_ENV)
        or not isinstance(target, dict)
        or target.get("kind") != "session"
        or not isinstance(target.get("value"), str)
        or not target["value"]
    ):
        return None
    configured = os.environ.get(APP_SERVER_SOCKET_ENV)
    if configured and truthy_env(ALLOW_APP_SERVER_SOCKET_OVERRIDE_ENV):
        return Path(configured).expanduser()
    return codex_home() / "app-server-control" / "app-server-control.sock"


def desktop_sandbox_policy(request: dict[str, object]) -> dict[str, object] | None:
    if request.get("sandbox_mode") != "workspace-write":
        return None
    queue_root = request.get("queue_dir")
    if not isinstance(queue_root, str) or not queue_root:
        return None
    root = Path(queue_root).expanduser()
    if not root.is_absolute():
        return None
    return {"type": "workspaceWrite", "writableRoots": [str(root)]}


class AppServerConnection:
    def __init__(self, path: Path, timeout: float) -> None:
        self.path = path
        self.timeout = timeout
        self.socket: socket.socket | None = None
        self.buffer = b""
        self.request_id = 0
        self.notifications: list[dict[str, object]] = []

    def connect(self) -> None:
        if not self.path.is_socket():
            raise AppServerProtocolError(f"control socket unavailable at {self.path}")
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        deadline = time.monotonic() + self.timeout
        try:
            connection.settimeout(max(0.1, deadline - time.monotonic()))
            connection.connect(str(self.path))
            self._verify_peer_uid(connection)
            key = base64.b64encode(secrets.token_bytes(16)).decode("ascii")
            request = (
                "GET / HTTP/1.1\r\n"
                "Host: localhost\r\n"
                "Upgrade: websocket\r\n"
                "Connection: Upgrade\r\n"
                f"Sec-WebSocket-Key: {key}\r\n"
                "Sec-WebSocket-Version: 13\r\n\r\n"
            )
            connection.sendall(request.encode("ascii"))
            self.socket = connection
            header = self._read_headers(deadline)
            lines = header.decode("iso-8859-1").split("\r\n")
            expected_accept = base64.b64encode(
                hashlib.sha1(f"{key}258EAFA5-E914-47DA-95CA-C5AB0DC85B11".encode("ascii")).digest()
            ).decode("ascii")
            headers = {
                name.strip().lower(): value.strip()
                for line in lines[1:]
                if ":" in line
                for name, value in [line.split(":", 1)]
            }
            if (
                not lines
                or lines[0] != "HTTP/1.1 101 Switching Protocols"
                or headers.get("sec-websocket-accept") != expected_accept
                or not self._header_has_token(headers, "connection", "upgrade")
                or not self._header_has_token(headers, "upgrade", "websocket")
            ):
                raise AppServerProtocolError("control socket rejected WebSocket upgrade")
        except BaseException:
            connection.close()
            self.socket = None
            raise

    def close(self) -> None:
        if self.socket is not None:
            self.socket.close()
            self.socket = None

    def request(self, method: str, params: dict[str, object]) -> object:
        self.request_id += 1
        request_id = self.request_id
        self._send_frame(0x1, json.dumps({"id": request_id, "method": method, "params": params}))
        deadline = time.monotonic() + self.timeout
        while True:
            response = self._receive_json(deadline)
            if "method" in response:
                self.notifications.append(response)
                continue
            if response.get("id") != request_id:
                raise AppServerProtocolError("control socket returned an unexpected response id")
            if "error" in response:
                raise AppServerRpcError(f"App Server {method} error: {response['error']}")
            if "result" not in response:
                raise AppServerProtocolError(f"App Server {method} returned no result")
            return response["result"]

    def notify(self, method: str, params: dict[str, object]) -> None:
        self._send_frame(0x1, json.dumps({"method": method, "params": params}))

    def wait_for_turn_completion(self, thread_id: str, turn_id: str, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while True:
            if self.notifications:
                message = self.notifications.pop(0)
            else:
                try:
                    message = self._receive_json(deadline)
                except AppServerTimeout:
                    return False
            params = message.get("params")
            if (
                message.get("method") == "turn/completed"
                and isinstance(params, dict)
                and params.get("threadId") == thread_id
                and isinstance(params.get("turn"), dict)
                and params["turn"].get("id") == turn_id
            ):
                return True

    @staticmethod
    def _header_has_token(headers: dict[str, str], name: str, expected: str) -> bool:
        return expected in {token.strip().lower() for token in headers.get(name, "").split(",")}

    @staticmethod
    def _verify_peer_uid(connection: socket.socket) -> None:
        if not hasattr(socket, "SO_PEERCRED"):
            return
        try:
            credentials = connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
            _peer_pid, peer_uid, _peer_gid = struct.unpack("3i", credentials)
        except OSError as exc:
            raise AppServerProtocolError(f"could not verify control-socket peer: {exc}") from exc
        if peer_uid != os.geteuid():
            raise AppServerProtocolError("control socket peer does not belong to this user")

    def _read_headers(self, deadline: float) -> bytes:
        while b"\r\n\r\n" not in self.buffer:
            if len(self.buffer) >= 65_536:
                raise AppServerProtocolError("control socket response headers exceed 64 KiB")
            self.buffer += self._receive_bytes(4096, deadline)
        header, self.buffer = self.buffer.split(b"\r\n\r\n", 1)
        return header

    def _receive_json(self, deadline: float) -> dict[str, object]:
        opcode, payload = self._receive_message(deadline)
        if opcode == 0x8:
            raise AppServerProtocolError("control socket closed during request")
        if opcode != 0x1:
            raise AppServerProtocolError("control socket sent a non-text message")
        try:
            message = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise AppServerProtocolError(f"invalid control-socket response: {exc}") from exc
        if not isinstance(message, dict):
            raise AppServerProtocolError("control socket response was not a JSON object")
        if "method" in message and not isinstance(message["method"], str):
            raise AppServerProtocolError("control socket notification method was not a string")
        if "id" not in message and "method" not in message:
            raise AppServerProtocolError("control socket response had neither id nor method")
        return message

    def _receive_message(self, deadline: float) -> tuple[int, bytes]:
        final, opcode, payload = self._receive_frame(deadline)
        while opcode == 0x9:
            self._send_frame(0xA, payload)
            final, opcode, payload = self._receive_frame(deadline)
        if final or opcode in (0x8, 0xA):
            return opcode, payload
        if opcode != 0x1:
            raise AppServerProtocolError("control socket started an unsupported fragmented message")
        chunks = [payload]
        while True:
            final, continuation_opcode, continuation = self._receive_frame(deadline)
            if continuation_opcode == 0x9:
                self._send_frame(0xA, continuation)
                continue
            if continuation_opcode != 0x0:
                raise AppServerProtocolError("control socket interrupted a fragmented message")
            chunks.append(continuation)
            if sum(len(chunk) for chunk in chunks) > 1_048_576:
                raise AppServerProtocolError("control socket message exceeds 1 MiB")
            if final:
                return opcode, b"".join(chunks)

    def _receive_frame(self, deadline: float) -> tuple[bool, int, bytes]:
        header = self._read_exact(2, deadline)
        final = bool(header[0] & 0x80)
        if header[0] & 0x70:
            raise AppServerProtocolError("control socket frame used unsupported RSV bits")
        opcode = header[0] & 0x0F
        masked = bool(header[1] & 0x80)
        if masked:
            raise AppServerProtocolError("control socket server frame must not be masked")
        length = header[1] & 0x7F
        if length == 126:
            length = struct.unpack("!H", self._read_exact(2, deadline))[0]
        elif length == 127:
            length = struct.unpack("!Q", self._read_exact(8, deadline))[0]
        if opcode >= 0x8 and (not final or length > 125):
            raise AppServerProtocolError("control socket sent an invalid control frame")
        if length > 1_048_576:
            raise AppServerProtocolError("control socket frame exceeds 1 MiB")
        mask = self._read_exact(4, deadline) if masked else None
        payload = self._read_exact(length, deadline)
        if mask is not None:
            payload = bytes(value ^ mask[index % 4] for index, value in enumerate(payload))
        return final, opcode, payload

    def _send_frame(self, opcode: int, text: str | bytes) -> None:
        if self.socket is None:
            raise AppServerProtocolError("control socket is not connected")
        payload = text.encode("utf-8") if isinstance(text, str) else text
        if len(payload) > 1_048_576:
            raise AppServerProtocolError("control socket request exceeds 1 MiB")
        if len(payload) < 126:
            header = bytes([0x80 | opcode, 0x80 | len(payload)])
        elif len(payload) <= 0xFFFF:
            header = bytes([0x80 | opcode, 0x80 | 126]) + struct.pack("!H", len(payload))
        else:
            header = bytes([0x80 | opcode, 0x80 | 127]) + struct.pack("!Q", len(payload))
        mask = secrets.token_bytes(4)
        encoded = bytes(value ^ mask[index % 4] for index, value in enumerate(payload))
        self.socket.sendall(header + mask + encoded)

    def _read_exact(self, size: int, deadline: float) -> bytes:
        while len(self.buffer) < size:
            self.buffer += self._receive_bytes(size - len(self.buffer), deadline)
        value, self.buffer = self.buffer[:size], self.buffer[size:]
        return value

    def _receive_bytes(self, size: int, deadline: float) -> bytes:
        if self.socket is None:
            raise AppServerProtocolError("control socket is not connected")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise AppServerTimeout("control socket request timed out")
        self.socket.settimeout(remaining)
        try:
            value = self.socket.recv(size)
        except socket.timeout as exc:
            raise AppServerTimeout("control socket request timed out") from exc
        except OSError as exc:
            raise AppServerProtocolError(f"control socket read failed: {exc}") from exc
        if not value:
            raise AppServerProtocolError("control socket closed unexpectedly")
        return value


class DesktopAppServerDelivery:
    def __init__(self, connection: AppServerConnection | None, thread_id: str, turn_id: str | None) -> None:
        self.connection = connection
        self.thread_id = thread_id
        self.turn_id = turn_id

    def wait_for_completion(self, timeout: float) -> bool:
        if self.connection is None or self.turn_id is None:
            time.sleep(timeout)
            return False
        try:
            return self.connection.wait_for_turn_completion(self.thread_id, self.turn_id, timeout)
        except AppServerProtocolError as exc:
            print(
                f"codex-long-task-wakeup: desktop App Server completion stream unavailable: {exc}; "
                "retaining the callback lease until timeout",
                file=sys.stderr,
            )
            self.close()
            return False

    def close(self) -> None:
        if self.connection is not None:
            self.connection.close()
            self.connection = None


def start_desktop_app_server_turn(payload: dict[str, object]) -> DesktopAppServerDelivery | None:
    request = payload.get("request")
    if not isinstance(request, dict):
        return None
    socket_path = desktop_app_server_socket(request)
    target = request.get("target")
    sandbox_policy = desktop_sandbox_policy(request)
    if socket_path is None or sandbox_policy is None or not isinstance(target, dict) or not isinstance(target.get("value"), str):
        return None
    connection = AppServerConnection(socket_path, min(15.0, max(1.0, float(payload["timeout"]))))
    turn_start_submitted = False
    try:
        connection.connect()
        connection.request(
            "initialize",
            {
                "clientInfo": {"name": "long-task-wakeup", "version": __version__},
                "capabilities": {"experimentalApi": True},
            },
        )
        connection.notify("initialized", {})
        connection.request("thread/resume", {"threadId": target["value"], "excludeTurns": True})
        turn_params: dict[str, object] = {
            "threadId": target["value"],
            "cwd": str(payload["cwd"]),
            "input": [{"type": "text", "text": str(payload["prompt"])}],
            "sandboxPolicy": sandbox_policy,
        }
        approval_policy = request.get("approval_policy")
        if isinstance(approval_policy, str) and approval_policy:
            turn_params["approvalPolicy"] = approval_policy
        approvals_reviewer = request.get("approvals_reviewer")
        if isinstance(approvals_reviewer, str) and approvals_reviewer:
            turn_params["approvalsReviewer"] = approvals_reviewer
        turn_start_submitted = True
        started = connection.request("turn/start", turn_params)
        turn_id = None
        if isinstance(started, dict) and isinstance(started.get("turn"), dict):
            candidate = started["turn"].get("id")
            turn_id = candidate if isinstance(candidate, str) and candidate else None
        if turn_id is None:
            print(
                "codex-long-task-wakeup: desktop App Server accepted turn/start without a turn id; "
                "retaining the callback lease until timeout",
                file=sys.stderr,
            )
            connection.close()
            return DesktopAppServerDelivery(None, target["value"], None)
        delivery = DesktopAppServerDelivery(connection, target["value"], turn_id)
        connection = None
        return delivery
    except AppServerRpcError as exc:
        if turn_start_submitted:
            print(
                f"codex-long-task-wakeup: desktop App Server rejected turn/start: {exc}; falling back to CLI",
                file=sys.stderr,
            )
        else:
            print(f"codex-long-task-wakeup: desktop App Server delivery unavailable: {exc}; falling back to CLI", file=sys.stderr)
        return None
    except (OSError, ValueError, TypeError, AppServerProtocolError) as exc:
        if turn_start_submitted:
            print(
                f"codex-long-task-wakeup: desktop App Server turn/start outcome is unknown: {exc}; "
                "waiting for ACK or timeout without CLI fallback",
                file=sys.stderr,
            )
            return DesktopAppServerDelivery(None, target["value"], None)
        print(f"codex-long-task-wakeup: desktop App Server delivery unavailable: {exc}; falling back to CLI", file=sys.stderr)
        return None
    finally:
        if connection is not None:
            connection.close()


def delivery_worker_main() -> int:
    """Own one Codex resume and its locks without leaking them into Codex."""
    encoded_fds = os.environ.get("CODEX_LONG_TASK_DELIVERY_LOCK_FDS")
    if encoded_fds:
        lock_fds = [int(value) for value in json.loads(encoded_fds)]
    else:  # Backward compatibility with delivery workers launched by 0.4.1.
        lock_fds = [int(os.environ["CODEX_LONG_TASK_DELIVERY_LOCK_FD"])]
    payload = json.load(sys.stdin)
    command = payload["command"]
    prompt = str(payload["prompt"])
    timeout = max(1.0, float(payload["timeout"]))
    result: dict[str, object] = {"returncode": 127}
    process: subprocess.Popen[str] | None = None
    desktop_delivery: DesktopAppServerDelivery | None = None

    class WorkerInterrupted(Exception):
        def __init__(self, signum: int) -> None:
            self.signum = signum

    def interrupt(signum: int, _frame: object) -> None:
        raise WorkerInterrupted(signum)

    signal.signal(signal.SIGTERM, interrupt)
    signal.signal(signal.SIGINT, interrupt)
    try:
        if Path(str(payload["ack_path"])).exists():
            result = {"returncode": 0, "skipped": "already_acknowledged"}
        elif Path(str(payload["canceled_path"])).exists():
            result = {"returncode": 0, "skipped": "canceled"}
        elif (desktop_delivery := start_desktop_app_server_turn(payload)) is not None:
            deadline = time.monotonic() + timeout
            while True:
                acknowledged = Path(str(payload["ack_path"])).exists()
                canceled = Path(str(payload["canceled_path"])).exists()
                if canceled:
                    result = {"returncode": 0, "delivery": "desktop_app_server", "skipped": "canceled"}
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    request = payload.get("request")
                    queue_root = payload.get("queue_dir")
                    if isinstance(request, dict) and isinstance(queue_root, str):
                        retain_target_lease(Path(queue_root), request)
                        if Path(str(payload["ack_path"])).exists() or Path(str(payload["canceled_path"])).exists():
                            release_retained_target_lease(Path(queue_root), request)
                            result = {
                                "returncode": 0,
                                "delivery": "desktop_app_server",
                                "skipped": "acknowledged_or_canceled_after_timeout",
                            }
                            break
                    result = {
                        "returncode": 125,
                        "timed_out": True,
                        "delivery": "desktop_app_server",
                        "manual_recovery_required": True,
                    }
                    break
                if desktop_delivery.wait_for_completion(min(0.1, remaining)):
                    acknowledged = Path(str(payload["ack_path"])).exists()
                    result = {
                        "returncode": 0 if acknowledged else 1,
                        "delivery": "desktop_app_server",
                        "turn_completed": True,
                    }
                    break
        else:
            process = subprocess.Popen(
                [str(part) for part in command],
                stdin=subprocess.PIPE,
                stdout=sys.stderr,
                stderr=sys.stderr,
                text=True,
                cwd=str(payload["cwd"]),
                close_fds=True,
                start_new_session=True,
            )
            try:
                process.communicate(input=prompt, timeout=timeout)
                result = {"returncode": int(process.returncode)}
            except subprocess.TimeoutExpired:
                terminate_process_group(process)
                result = {"returncode": 124, "timed_out": True}
    except WorkerInterrupted as exc:
        if process is not None:
            terminate_process_group(process)
        result = {"returncode": 128 + exc.signum, "interrupted": exc.signum}
    except BaseException as exc:
        if process is not None:
            terminate_process_group(process)
        result = {"returncode": 127, "error": f"{type(exc).__name__}: {exc}"}
    finally:
        if desktop_delivery is not None:
            desktop_delivery.close()
        for lock_fd in lock_fds:
            try:
                os.close(lock_fd)
            except OSError:
                pass

    try:
        sys.stdout.write(json.dumps(result, ensure_ascii=False) + "\n")
        sys.stdout.flush()
    except BrokenPipeError:
        pass
    return 0


def delivery_worker_command() -> list[str]:
    code = "from long_task_callback.cli import delivery_worker_main; raise SystemExit(delivery_worker_main())"
    return [sys.executable, "-c", code]


def finish_delivery_worker(process: subprocess.Popen[str]) -> int:
    process.wait()
    output = ""
    if process.stdout is not None:
        output = process.stdout.read()
        process.stdout.close()
    for line in reversed(output.splitlines()):
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        returncode = payload.get("returncode") if isinstance(payload, dict) else None
        if isinstance(returncode, int):
            return returncode
    return process.returncode if process.returncode != 0 else 127


def stop_resume_process(process: subprocess.Popen[str]) -> None:
    if process.poll() is None:
        try:
            process.terminate()
        except ProcessLookupError:
            pass
    try:
        process.wait(timeout=7.0)
    except subprocess.TimeoutExpired:
        try:
            process.kill()
        except ProcessLookupError:
            pass
        process.wait()
    finish_delivery_worker(process)


def run_resume_until_exit_or_ack(
    root: Path,
    request_id: str,
    request: dict[str, object],
    args: argparse.Namespace,
) -> tuple[subprocess.CompletedProcess[str] | None, bool, bool]:
    command = resume_command(request)
    delivery_lock = acquire_owner_lock(root, delivery_lock_id(request_id), blocking=False)
    if delivery_lock is None:
        return None, ack_path(root, request_id).exists(), True
    if ack_path(root, request_id).exists():
        release_owner_lock(delivery_lock, remove=False)
        return subprocess.CompletedProcess(command, 0), True, False
    if is_canceled(root, request_id):
        release_owner_lock(delivery_lock, remove=False)
        return None, False, False
    target_lock = acquire_target_lock(request, blocking=False)
    if target_lock is None:
        release_owner_lock(delivery_lock, remove=False)
        raise TargetLeaseUnavailable(callback_target_key(request) or "unknown target")
    if retained_target_lease_is_held(request):
        release_owner_lock(delivery_lock, remove=False)
        release_owner_lock(target_lock, remove=False)
        raise TargetLeaseUnavailable(callback_target_key(request) or "unknown target")
    if ack_path(root, request_id).exists():
        release_owner_lock(delivery_lock, remove=False)
        release_owner_lock(target_lock, remove=False)
        return subprocess.CompletedProcess(command, 0), True, False
    if is_canceled(root, request_id):
        release_owner_lock(delivery_lock, remove=False)
        release_owner_lock(target_lock, remove=False)
        return None, False, False
    delivery_handle, _ = delivery_lock
    target_handle, _ = target_lock
    timeout = max(1.0, float(getattr(args, "resume_timeout", DEFAULT_RESUME_TIMEOUT)))
    deadline = time.monotonic() + timeout
    payload = {
        "command": command,
        "prompt": str(request["prompt"]),
        "cwd": str(request["cwd"]),
        "request": request,
        "queue_dir": str(root),
        "timeout": timeout,
        "ack_path": str(ack_path(root, request_id)),
        "canceled_path": str(request_path(root, "canceled", request_id)),
    }
    env = os.environ.copy()
    lock_fds = (delivery_handle.fileno(), target_handle.fileno())
    env["CODEX_LONG_TASK_DELIVERY_LOCK_FDS"] = json.dumps(lock_fds)
    env["CODEX_LONG_TASK_DELIVERY_LOCK_FD"] = str(delivery_handle.fileno())
    try:
        process = subprocess.Popen(
            delivery_worker_command(),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
            close_fds=True,
            pass_fds=lock_fds,
            env=env,
        )
    except BaseException:
        release_owner_lock(delivery_lock, remove=False)
        release_owner_lock(target_lock, remove=False)
        raise
    close_parent_lock_copy(delivery_lock)
    close_parent_lock_copy(target_lock)
    try:
        if process.stdin is not None:
            try:
                process.stdin.write(json.dumps(payload, ensure_ascii=False))
                process.stdin.flush()
            except BrokenPipeError:
                pass
            finally:
                process.stdin.close()

        while True:
            acked = ack_path(root, request_id).exists()
            canceled = is_canceled(root, request_id)
            returncode = process.poll()
            if acked or canceled:
                if returncode is None:
                    key = callback_target_key(request)
                    if key is not None:
                        _BACKGROUND_RESUMES[process.pid] = BackgroundResume(
                            process=process,
                            target_key=key,
                            deadline=deadline,
                            request_id=request_id,
                        )
                else:
                    child_returncode = finish_delivery_worker(process)
                    return subprocess.CompletedProcess(command, child_returncode), acked, False
                result = subprocess.CompletedProcess(command, returncode if returncode is not None else 0) if acked else None
                return result, acked, returncode is None
            if returncode is not None:
                child_returncode = finish_delivery_worker(process)
                return subprocess.CompletedProcess(command, child_returncode), ack_path(root, request_id).exists(), False
            if time.monotonic() >= deadline:
                stop_resume_process(process)
                return subprocess.CompletedProcess(command, 124), ack_path(root, request_id).exists(), False
            time.sleep(0.1)
    except BaseException:
        stop_resume_process(process)
        raise


def fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def write_request(path: Path, request: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.stem}.{uuid.uuid4().hex}.tmp")
    try:
        with tmp.open("w", encoding="utf-8") as handle:
            handle.write(json.dumps(request, ensure_ascii=False, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        fsync_directory(path.parent)
    except Exception:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def move_request(source: Path, destination_dir: Path) -> Path:
    destination_dir.mkdir(parents=True, exist_ok=True)
    destination = destination_dir / source.name
    if source == destination:
        return destination
    if destination.exists():
        try:
            source_data = json.loads(source.read_text(encoding="utf-8"))
            destination_data = json.loads(destination.read_text(encoding="utf-8"))
            source_id = source_data.get("id") if isinstance(source_data, dict) else None
            destination_id = destination_data.get("id") if isinstance(destination_data, dict) else None
        except Exception as exc:
            raise FileExistsError(f"cannot validate callback collision at {destination}") from exc
        if (
            not isinstance(source_id, str)
            or not isinstance(source_data, dict)
            or not isinstance(destination_data, dict)
            or source_data.get("version") != 1
            or destination_data.get("version") != 1
            or source_id != destination_id
            or source_id != source.stem
            or source_data != destination_data
        ):
            raise FileExistsError(f"refusing to collapse divergent callback records at {destination}")
        try:
            source.unlink()
        except FileNotFoundError:
            pass
        else:
            fsync_directory(source.parent)
        return destination
    os.replace(source, destination)
    if destination_dir != source.parent:
        fsync_directory(destination_dir)
    fsync_directory(source.parent)
    return destination


def owner_lock_path(root: Path, request_id: str) -> Path:
    return root / LOCKS_STATE / f"{request_id}.lock"


def acquire_owner_lock(root: Path, request_id: str, *, blocking: bool) -> tuple[object, Path] | None:
    if fcntl is None:  # pragma: no cover - supported deployments are POSIX
        raise RuntimeError("durable callback ownership requires POSIX flock support")
    ensure_daemon_dirs(root)
    path = owner_lock_path(root, request_id)
    handle = path.open("a+", encoding="utf-8")
    os.set_inheritable(handle.fileno(), False)
    operation = fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB)
    try:
        fcntl.flock(handle.fileno(), operation)
    except BlockingIOError:
        handle.close()
        return None
    return handle, path


def release_owner_lock(lock: tuple[object, Path] | None, *, remove: bool) -> None:
    if lock is None:
        return
    handle, path = lock
    try:
        if fcntl is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()
    if remove:
        try:
            path.unlink()
            fsync_directory(path.parent)
        except FileNotFoundError:
            pass


def delivery_lock_id(request_id: str) -> str:
    return f"delivery-{request_id}"


def delivery_lock_is_held(root: Path, request_id: str) -> bool:
    lock = acquire_owner_lock(root, delivery_lock_id(request_id), blocking=False)
    if lock is None:
        return True
    release_owner_lock(lock, remove=False)
    return False


def close_parent_lock_copy(lock: tuple[object, Path]) -> None:
    """Close without LOCK_UN after pass_fds transfers the lock to a child."""
    handle, _ = lock
    handle.close()


def current_boot_id() -> str | None:
    try:
        value = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return value or None


def active_prompt(args: argparse.Namespace) -> str:
    command = args.command or " ".join(args.wrapped_command)
    return "\n".join(
        [
            "[long-task-callback-active]",
            f"Task: {args.task}",
            f"Working directory: {args.cwd}",
            f"Command: {command}",
            "State: armed before task launch; this record is not dispatchable.",
        ]
    )


def make_active_request(args: argparse.Namespace, started_at: float) -> dict[str, object]:
    request = make_request(args, active_prompt(args))
    request.update(
        {
            "queue_dir": str(queue_dir(args)),
            "lifecycle_state": ACTIVE_STATE,
            "task": args.task,
            "command": args.command or " ".join(args.wrapped_command),
            "started_at": started_at,
            "wrapper_pid": os.getpid(),
            "boot_id": current_boot_id(),
            "launch_phase": "prelaunch",
        }
    )
    return request


def request_already_transitioned(root: Path, request_id: str) -> bool:
    return any(request_path(root, state, request_id).exists() for state in ("pending", "running", "done", "failed", "canceled"))


def is_canceled(root: Path, request_id: str) -> bool:
    return request_path(root, "canceled", request_id).exists()


def remove_live_request_copies(root: Path, request_id: str) -> None:
    """Remove dispatchable copies after a durable cancellation tombstone exists."""
    for _ in range(2):
        removed = False
        for state in (ACTIVE_STATE, "pending", "running", "failed"):
            path = request_path(root, state, request_id)
            try:
                path.unlink()
            except FileNotFoundError:
                continue
            fsync_directory(path.parent)
            removed = True
        if not removed:
            break


def discard_unlaunched_active(root: Path, request_id: str) -> None:
    path = request_path(root, ACTIVE_STATE, request_id)
    try:
        path.unlink()
    except FileNotFoundError:
        return
    except OSError as exc:
        print(f"codex-long-task-wakeup: warning: could not remove unlaunched callback {request_id}: {exc}", file=sys.stderr)
        return
    try:
        fsync_directory(path.parent)
    except OSError as exc:
        print(
            f"codex-long-task-wakeup: warning: removed unlaunched active callback {request_id} "
            f"but could not sync its directory: {exc}",
            file=sys.stderr,
        )


def best_effort_disarm(
    root: Path,
    request_id: str,
    lock: tuple[object, Path] | None,
) -> None:
    try:
        discard_unlaunched_active(root, request_id)
    except Exception as exc:
        print(f"codex-long-task-wakeup: warning: pre-arm record cleanup failed: {exc}", file=sys.stderr)
    try:
        release_owner_lock(lock, remove=True)
    except Exception as exc:
        print(f"codex-long-task-wakeup: warning: pre-arm lock cleanup failed: {exc}", file=sys.stderr)


def transition_active_to_pending(root: Path, request: dict[str, object], prompt: str) -> int:
    request_id = str(request["id"])
    source = request_path(root, ACTIVE_STATE, request_id)
    if is_canceled(root, request_id):
        remove_live_request_copies(root, request_id)
        print(f"codex-long-task-wakeup: callback {request_id} was canceled; suppressing finalization", file=sys.stderr)
        return 0
    if not source.exists():
        if request_already_transitioned(root, request_id):
            return 0
        print(
            f"codex-long-task-wakeup: warning: active callback {request_id} disappeared before finalization",
            file=sys.stderr,
        )
        return 1
    prepare_request_for_queue(root, request, prompt)
    if is_canceled(root, request_id):
        remove_live_request_copies(root, request_id)
        return 0
    write_request(source, request)
    if is_canceled(root, request_id):
        remove_live_request_copies(root, request_id)
        return 0
    try:
        move_request(source, root / "pending")
    except FileNotFoundError:
        if is_canceled(root, request_id) or request_already_transitioned(root, request_id):
            if is_canceled(root, request_id):
                remove_live_request_copies(root, request_id)
            return 0
        raise
    if is_canceled(root, request_id):
        remove_live_request_copies(root, request_id)
        return 0
    print(f"codex-long-task-wakeup: queued callback {request_id} in {root}", file=sys.stderr)
    return 0


def recovery_prompt(request: dict[str, object], now: float) -> str:
    started_at = request.get("started_at")
    duration = now - float(started_at) if isinstance(started_at, (int, float)) else None
    recovered_args = argparse.Namespace(
        task=str(request.get("task") or request_task(request)),
        cwd=str(request["cwd"]),
        command=str(request.get("command") or ""),
        exit_code=None,
        message=(
            "The long-task wrapper disappeared before recording task completion. "
            "The task outcome and exit status are unknown, and the wrapped child may still be running. "
            "Inspect process state and artifacts before retrying or launching the next stage."
        ),
    )
    return build_prompt(recovered_args, duration)


def recover_active(root: Path) -> int:
    ensure_daemon_dirs(root)
    recovered = 0
    for path in sorted((root / ACTIVE_STATE).glob("*.json")):
        request_id = path.stem
        lock = acquire_owner_lock(root, request_id, blocking=False)
        if lock is None:
            continue
        remove_lock = False
        try:
            if not path.exists():
                continue
            if is_canceled(root, request_id):
                remove_live_request_copies(root, request_id)
                remove_lock = True
                continue
            request = load_request(path)
            if request.get("launch_phase") == "prelaunch":
                discard_unlaunched_active(root, request_id)
                remove_lock = True
                print(
                    f"codex-long-task-wakeup: discarded callback {request_id}; wrapped task was not launch-committed",
                    file=sys.stderr,
                )
                continue
            if request.get("outcome") == "completed" and isinstance(request.get("exit_code"), int):
                move_request(path, root / "pending")
                recovered += 1
                remove_lock = True
                print(
                    f"codex-long-task-wakeup: recovered completed callback {request_id} with its recorded exit code",
                    file=sys.stderr,
                )
                continue
            request["lifecycle_state"] = "recovered_unknown"
            request["recovered_at"] = time.time()
            request["recovery_reason"] = "wrapper_owner_lock_released_before_completion"
            request["outcome"] = "unknown"
            prompt = recovery_prompt(request, float(request["recovered_at"]))
            if transition_active_to_pending(root, request, prompt) == 0:
                recovered += 1
                remove_lock = True
                print(
                    f"codex-long-task-wakeup: recovered orphaned active callback {request_id} with unknown outcome",
                    file=sys.stderr,
                )
        except Exception as exc:
            print(
                f"codex-long-task-wakeup: warning: failed to recover active callback {request_id}: {exc}",
                file=sys.stderr,
            )
        finally:
            release_owner_lock(lock, remove=remove_lock)
    return recovered


def cancel_one(root: Path, request_id: str, message: str | None = None) -> bool:
    ensure_daemon_dirs(root)
    canceled = request_path(root, "canceled", request_id)
    if canceled.exists():
        remove_live_request_copies(root, request_id)
        print(f"codex-long-task-wakeup: callback {request_id} is already canceled in {root}", file=sys.stderr)
        return True

    for state in (ACTIVE_STATE, "pending", "running", "failed"):
        source = request_path(root, state, request_id)
        if not source.exists():
            continue
        try:
            data = json.loads(source.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                data = {}
        except Exception:
            data = {}
        data.update(
            {
                "id": request_id,
                "canceled_at": time.time(),
                "canceled_from": state,
            }
        )
        if message:
            data["cancel_message"] = message
        # Publish the tombstone first. The wrapper finalizer and daemon both
        # honor it, so cancellation wins even if a state transition races us.
        write_request(canceled, data)
        remove_live_request_copies(root, request_id)
        try:
            release_retained_target_lease(root, data)
        except RuntimeError as exc:
            print(
                f"codex-long-task-wakeup: warning: could not release retained target lease for "
                f"{request_id}: {exc}",
                file=sys.stderr,
            )
        print(f"codex-long-task-wakeup: canceled callback {request_id} from {state} in {root}", file=sys.stderr)
        return True

    print(f"codex-long-task-wakeup: warning: active callback {request_id} not found in {root}", file=sys.stderr)
    return False


def cancel_all(root: Path, message: str | None = None) -> int:
    ensure_daemon_dirs(root)
    request_ids: set[str] = set()
    for state in (ACTIVE_STATE, "pending", "running", "failed"):
        request_ids.update(path.stem for path in (root / state).glob("*.json"))
    canceled = 0
    for request_id in sorted(request_ids):
        if cancel_one(root, request_id, message):
            canceled += 1
    print(f"codex-long-task-wakeup: canceled {canceled} active callback(s) in {root}", file=sys.stderr)
    return canceled


def process_one(root: Path, args: argparse.Namespace) -> bool:
    ensure_daemon_dirs(root)
    path = select_pending(root, time.time())
    if path is None:
        return False

    request_id = path.stem
    if is_canceled(root, request_id):
        remove_live_request_copies(root, request_id)
        return True
    try:
        running = move_request(path, root / "running")
    except FileNotFoundError:
        return True
    except Exception as exc:
        print(f"codex-long-task-wakeup: warning: could not claim {path.name}: {exc}", file=sys.stderr)
        return True
    if is_canceled(root, request_id):
        remove_live_request_copies(root, request_id)
        return True

    try:
        request = load_request(running)
        request_id = str(request.get("id", running.stem))
        if ack_path(root, request_id).exists():
            move_request(running, root / "done")
            return True
        attempts = int(request.get("attempts", 0)) + 1
        request["attempts"] = attempts
        request.pop("next_attempt_at", None)
        write_request(running, request)
        try:
            result, acked, resume_still_running = run_resume_until_exit_or_ack(root, request_id, request, args)
        except TargetLeaseUnavailable as exc:
            # A daemon for another queue won the cross-queue race after this
            # request was selected. Put it back without consuming a retry.
            request["attempts"] = max(0, attempts - 1)
            request["last_deferred_reason"] = f"target lease held: {exc}"
            if not running.exists() or is_canceled(root, request_id):
                return True
            write_request(running, request)
            move_request(running, root / "pending")
            return True
        if resume_still_running:
            return True
        if result is None:
            return True
        if result.returncode == 124 and not acked:
            request["last_error"] = "Codex resume timed out"
            print(
                f"codex-long-task-wakeup: warning: daemon callback {running.name} timed out",
                file=sys.stderr,
            )
        if result.returncode == 125 and not acked:
            request["last_error"] = "Desktop callback outcome is unknown; automatic retry suppressed to prevent duplicate delivery"
            request["retain_target_lease"] = True
            destination_dir = root / "failed"
            print(
                f"codex-long-task-wakeup: warning: daemon callback {running.name} has an unknown Desktop outcome; "
                "manual recovery is required to avoid duplicate delivery",
                file=sys.stderr,
            )
            if running.exists():
                write_request(running, request)
            else:
                return True
        else:
            if result.returncode == 125 and acked:
                release_retained_target_lease(root, request)
            destination_dir = root / ("done" if acked else "failed")
        if result.returncode != 0:
            print(
                f"codex-long-task-wakeup: warning: daemon callback {running.name} exited with {result.returncode}",
                file=sys.stderr,
            )
        if not acked and result.returncode != 125:
            request.setdefault("last_error", f"missing acknowledgement marker after exit {result.returncode}")
            max_retries = max(0, int(getattr(args, "retries", DEFAULT_RETRIES)))
            if attempts <= max_retries:
                delay = retry_delay(args, attempts)
                request["next_attempt_at"] = time.time() + delay
                if not running.exists():
                    print(
                        f"codex-long-task-wakeup: callback {request_id} disappeared from running; assuming it was canceled",
                        file=sys.stderr,
                    )
                    return True
                write_request(running, request)
                move_request(running, root / "pending")
                print(
                    f"codex-long-task-wakeup: warning: daemon callback {running.name} was not acknowledged; "
                    f"retry {attempts}/{max_retries} in {delay:.1f}s",
                    file=sys.stderr,
                )
                return True
    except Exception as exc:
        destination_dir = root / "failed"
        print(f"codex-long-task-wakeup: warning: daemon failed to process {running.name}: {exc}", file=sys.stderr)

    if not running.exists():
        print(f"codex-long-task-wakeup: callback {running.stem} disappeared from running; assuming it was canceled", file=sys.stderr)
        return True
    try:
        finalized = move_request(running, destination_dir)
        # Close the final-attempt race with ack(): the marker may become
        # durable after ack() looked for failed/<id>, but just before this
        # process moved running/<id> there.  Once failed/ is visible, either
        # this reconciliation or ack() itself will converge it to done/.
        if destination_dir == root / "failed" and ack_path(root, request_id).exists():
            try:
                move_request(finalized, root / "done")
                if request.get("retain_target_lease") is True:
                    release_retained_target_lease(root, request)
            except FileNotFoundError:
                pass
    except FileNotFoundError:
        return True
    except Exception as exc:
        print(f"codex-long-task-wakeup: warning: could not finalize {running.name}: {exc}", file=sys.stderr)
    return True


def recover_running(root: Path) -> None:
    ensure_daemon_dirs(root)
    for path in sorted((root / "running").glob("*.json")):
        if delivery_lock_is_held(root, path.stem):
            continue
        try:
            request = load_request(path)
            request_id = str(request.get("id", path.stem))
            destination = root / ("done" if ack_path(root, request_id).exists() else "pending")
            move_request(path, destination)
        except FileNotFoundError:
            if is_canceled(root, path.stem):
                continue
            print(
                f"codex-long-task-wakeup: running callback {path.stem} changed state during recovery",
                file=sys.stderr,
            )
        except Exception as exc:
            print(f"codex-long-task-wakeup: warning: failed to recover {path.name}: {exc}", file=sys.stderr)
            if not path.exists():
                continue
            try:
                move_request(path, root / "failed")
            except FileNotFoundError:
                continue
            except Exception as move_exc:
                print(
                    f"codex-long-task-wakeup: warning: could not quarantine {path.name}: {move_exc}",
                    file=sys.stderr,
                )


def process_goal_reminders(root: Path) -> bool:
    now = time.time()
    for path in sorted((root / "goals").glob("*.json")):
        lock: tuple[object, Path] | None = None
        try:
            goal_id = path.stem
            lock = acquire_owner_lock(root, goal_lock_id(goal_id), blocking=True)
            goal = load_goal(root, goal_id)
            if goal.get("state") != "active":
                continue
            last_callback = float(goal["last_external_callback_at"])
            observed_callback = latest_queued_goal_callback_at(root, goal_id)
            if observed_callback is not None and observed_callback > last_callback:
                goal["last_external_callback_at"] = observed_callback
                goal.pop("last_reminder_at", None)
                write_goal(root, goal)
                last_callback = observed_callback
            idle_seconds = float(goal.get("idle_seconds", DEFAULT_GOAL_IDLE_SECONDS))
            if now - last_callback < idle_seconds:
                continue
            last_reminder = goal.get("last_reminder_at")
            if last_reminder is not None and now - float(last_reminder) < idle_seconds:
                continue
            if queued_goal_reminder_exists(root, goal_id):
                goal["last_reminder_at"] = now
                write_goal(root, goal)
                return True
            request = {
                "version": 1, "id": uuid.uuid4().hex, "created_at": now, "cwd": goal["cwd"],
                "target": goal["target"], "target_source": goal.get("target_source", "goal"),
                "goal_id": goal_id, "goal_reminder": True, "prompt": "",
            }
            goal_ack_command = " ".join(
                shlex.quote(part)
                for part in [console_script_path(), "goal", "ack", "--queue-dir", str(root), "--id", goal_id]
            )
            prompt = (f"[goal-inactivity-reminder]\nGoal: {goal.get('task', goal_id)}\n"
                      f"No new callback has been queued for {idle_seconds / 3600:.0f} hour(s). "
                      f"If complete, run: {goal_ack_command} --state completed. If conditions are not ready, run: "
                      f"{goal_ack_command} --state blocked_conditions --condition \"specific missing prerequisite\". "
                      "Otherwise continue the goal and schedule the next callback.")
            if enqueue_existing_request(root, request, prompt) != 0:
                return True
            goal["last_reminder_at"] = now
            write_goal(root, goal)
            return True
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
            print(f"codex-long-task-wakeup: warning: could not process goal reminder {path.name}: {exc}", file=sys.stderr)
        finally:
            release_owner_lock(lock, remove=False)
    return False


def daemon_reexec_command(args: argparse.Namespace) -> list[str]:
    return [sys.executable, "-m", "long_task_callback", *daemon_command(args)[1:]]


def daemon_has_live_delivery_workers(root: Path) -> bool:
    if _BACKGROUND_RESUMES:
        return True
    for path in (root / "running").glob("*.json"):
        if delivery_lock_is_held(root, path.stem):
            return True
    return False


def daemon(args: argparse.Namespace) -> int:
    root = queue_dir(args)
    ensure_daemon_dirs(root)
    daemon_lock = acquire_owner_lock(root, "daemon-singleton", blocking=False)
    if daemon_lock is None:
        print(f"codex-long-task-wakeup: another daemon already owns {root}", file=sys.stderr)
        return 1

    reload_requested = False
    reload_deferred_reported = False

    def request_reload(_signum: int, _frame: object) -> None:
        nonlocal reload_requested
        reload_requested = True

    previous_hup_handler: object | None = None
    if hasattr(signal, "SIGHUP"):
        previous_hup_handler = signal.signal(signal.SIGHUP, request_reload)
    try:
        load_service_proxy_environment()
        write_daemon_runtime()
        recover_running(root)
        print(f"codex-long-task-wakeup: daemon watching {root}", file=sys.stderr)

        processed = 0
        while True:
            if reload_requested:
                reap_background_resumes()
                if daemon_has_live_delivery_workers(root):
                    if not reload_deferred_reported:
                        print(
                            "codex-long-task-wakeup: reload deferred until live delivery workers exit; "
                            "their leases and resumed Codex processes will not be interrupted.",
                            file=sys.stderr,
                        )
                        reload_deferred_reported = True
                else:
                    load_service_proxy_environment()
                    print(
                        "codex-long-task-wakeup: reloading daemon in place after delivery workers drained.",
                        file=sys.stderr,
                    )
                    os.execv(sys.executable, daemon_reexec_command(args))
                    raise RuntimeError("daemon reload exec unexpectedly returned")
            reap_background_resumes()
            recover_running(root)
            recover_active(root)
            if process_goal_reminders(root):
                continue
            if process_blocked_goal_email(root):
                continue
            did_work = process_one(root, args)
            if did_work:
                processed += 1
                if args.max_items is not None and processed >= args.max_items:
                    return 0
                continue
            if args.once:
                return 0
            time.sleep(args.interval)
    finally:
        if previous_hup_handler is not None and hasattr(signal, "SIGHUP"):
            signal.signal(signal.SIGHUP, previous_hup_handler)
        clear_daemon_runtime()
        # Keep the singleton inode stable. Unlinking a lock file after unlock
        # can let two newly starting daemons lock different inodes.
        release_owner_lock(daemon_lock, remove=False)


def done(args: argparse.Namespace) -> int:
    callback_code = resume_codex(args, build_prompt(args))
    return callback_code if args.strict else 0


def run_unarmed_via_daemon(args: argparse.Namespace, started: float, reason: str) -> int:
    print(
        "codex-long-task-wakeup: UNARMED WARNING: durable pre-launch callback persistence failed; "
        f"running with legacy post-exit best effort ({reason})",
        file=sys.stderr,
    )
    exit_code = 1
    task_returned = False
    try:
        completed = subprocess.run(
            args.wrapped_command,
            cwd=args.cwd,
            shell=False,
            check=False,
            close_fds=True,
        )
        exit_code = completed.returncode
        task_returned = True
        return exit_code
    finally:
        duration = time.time() - started
        callback_args = args
        if task_returned:
            args.exit_code = exit_code
        else:
            callback_args = argparse.Namespace(**vars(args))
            callback_args.exit_code = None
            interruption = "The unarmed wrapper did not observe a child return code; task outcome is unknown."
            callback_args.message = (
                f"{args.message}\n\n{interruption}" if getattr(args, "message", None) else interruption
            )
        callback_code = enqueue_request(callback_args, build_prompt(callback_args, duration))
        if task_returned and args.strict and exit_code == 0 and callback_code != 0:
            raise SystemExit(callback_code)


def run_via_daemon(args: argparse.Namespace) -> int:
    started = time.time()
    root = queue_dir(args)
    request = make_active_request(args, started)
    request_id = str(request["id"])
    try:
        lock = acquire_owner_lock(root, request_id, blocking=True)
    except (OSError, RuntimeError) as exc:
        if args.strict:
            print(f"codex-long-task-wakeup: refusing to launch unarmed task: {exc}", file=sys.stderr)
            return 125
        return run_unarmed_via_daemon(args, started, str(exc))
    if lock is None:
        reason = "failed to acquire callback owner lock"
        if args.strict:
            print(f"codex-long-task-wakeup: refusing to launch unarmed task: {reason}", file=sys.stderr)
            return 125
        return run_unarmed_via_daemon(args, started, reason)
    try:
        active_path = request_path(root, ACTIVE_STATE, request_id)
        write_request(active_path, request)
        request["launch_phase"] = "launch_committed"
        request["launch_committed_at"] = time.time()
        write_request(active_path, request)
    except OSError as exc:
        best_effort_disarm(root, request_id, lock)
        if args.strict:
            print(
                f"codex-long-task-wakeup: refusing to launch unarmed task because active callback persistence failed: {exc}",
                file=sys.stderr,
            )
            return 125
        return run_unarmed_via_daemon(args, started, str(exc))

    print(f"codex-long-task-wakeup: armed callback {request_id} before task launch", file=sys.stderr)
    exit_code = 1
    task_returned = False
    try:
        completed = subprocess.run(
            args.wrapped_command,
            cwd=args.cwd,
            shell=False,
            check=False,
            close_fds=True,
        )
        exit_code = completed.returncode
        task_returned = True
        return exit_code
    finally:
        args.command = args.command or " ".join(args.wrapped_command)
        duration = time.time() - started
        request["duration_seconds"] = duration
        if task_returned:
            args.exit_code = exit_code
            prompt = build_prompt(args, duration)
            request["completed_at"] = time.time()
            request["exit_code"] = exit_code
            request["outcome"] = "completed"
        else:
            interrupted_args = argparse.Namespace(**vars(args))
            interrupted_args.exit_code = None
            interruption = (
                "The callback wrapper stopped waiting before it observed a child return code. "
                "The task outcome is unknown and the wrapped child or descendants may still be running. "
                "Inspect processes and artifacts before retrying."
            )
            interrupted_args.message = (
                f"{args.message}\n\n{interruption}" if getattr(args, "message", None) else interruption
            )
            prompt = build_prompt(interrupted_args, duration)
            request["interrupted_at"] = time.time()
            request.pop("exit_code", None)
            request["outcome"] = "unknown"
            request["recovery_reason"] = "wrapper_did_not_observe_child_return"
        callback_code = 1
        try:
            callback_code = transition_active_to_pending(root, request, prompt)
        except Exception as exc:
            print(f"codex-long-task-wakeup: warning: failed to finalize callback {request_id}: {exc}", file=sys.stderr)
        finally:
            try:
                release_owner_lock(lock, remove=True)
            except Exception as exc:
                callback_code = 1
                print(f"codex-long-task-wakeup: warning: failed to clean callback lock {request_id}: {exc}", file=sys.stderr)
        if task_returned and args.strict and exit_code == 0 and callback_code != 0:
            raise SystemExit(callback_code)


def run(args: argparse.Namespace) -> int:
    if not args.wrapped_command:
        raise SystemExit("run mode requires a command after --")

    args.command = args.command or shlex.join(args.wrapped_command)
    bind_target(args)
    if should_enqueue(args):
        return run_via_daemon(args)

    started = time.time()
    exit_code = 1
    task_returned = False
    try:
        completed = subprocess.run(
            args.wrapped_command,
            cwd=args.cwd,
            shell=False,
            check=False,
        )
        exit_code = completed.returncode
        task_returned = True
        return exit_code
    finally:
        args.command = args.command or " ".join(args.wrapped_command)
        duration = time.time() - started
        callback_args = args
        if task_returned:
            args.exit_code = exit_code
        else:
            callback_args = argparse.Namespace(**vars(args))
            callback_args.exit_code = None
            interruption = "The callback wrapper did not observe a child return code; task outcome is unknown."
            callback_args.message = (
                f"{args.message}\n\n{interruption}" if getattr(args, "message", None) else interruption
            )
        prompt = build_prompt(callback_args, duration)
        callback_code = resume_codex(callback_args, prompt)
        if task_returned and args.strict and exit_code == 0 and callback_code != 0:
            raise SystemExit(callback_code)


def add_common_flags(parser: argparse.ArgumentParser) -> None:
    target = parser.add_mutually_exclusive_group()
    target.add_argument(
        "--session",
        help=f"Codex session id to resume (default: ${CODEX_THREAD_ID_ENV} from the launching Codex thread)",
    )
    target.add_argument(
        "--last",
        action="store_true",
        help="Unsafely resume the most recent Codex session instead of binding the launching thread",
    )
    parser.add_argument("--cwd", default=os.getcwd(), help="Working directory for resumed Codex")
    parser.add_argument("--task", default="long task", help="Human-readable task name")
    parser.add_argument("--command", help="Original command text")
    parser.add_argument("--exit-code", type=int, help="Completed task exit code")
    parser.add_argument("--message", help="Extra callback message")
    parser.add_argument(
        "--via-daemon",
        action="store_true",
        help="Queue the wakeup request for codex-long-task-wakeup daemon instead of running codex exec resume here",
    )
    parser.add_argument("--queue-dir", help="Wakeup queue directory for --via-daemon")
    parser.add_argument("--goal-id", help="Explicit goal record to update when this callback is queued")
    parser.add_argument(
        "--approvals-reviewer",
        default=os.environ.get("CODEX_LONG_TASK_WAKEUP_APPROVALS_REVIEWER", DEFAULT_APPROVALS_REVIEWER),
        help="Codex approvals_reviewer config value used when resuming (default: auto_review)",
    )
    parser.add_argument(
        "--approval-policy",
        default=os.environ.get("CODEX_LONG_TASK_WAKEUP_APPROVAL_POLICY", DEFAULT_APPROVAL_POLICY),
        help="Codex approval_policy config value used when resuming (default: on-request)",
    )
    parser.add_argument(
        "--sandbox-mode",
        default=os.environ.get("CODEX_LONG_TASK_WAKEUP_SANDBOX_MODE", DEFAULT_SANDBOX_MODE),
        help="Codex sandbox_mode config value used when resuming (default: workspace-write)",
    )
    parser.add_argument("--dry-run", action="store_true", help="Print the wakeup prompt instead of resuming Codex")
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Propagate callback failure. By default callback failure never changes task success or exit code.",
    )


def codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME", "~/.codex")).expanduser()


def install_skill(args: argparse.Namespace) -> int:
    target_root = Path(args.path).expanduser() if args.path else codex_home() / "skills"
    target = target_root / "long-task-callback"
    if target.exists() and not args.force:
        print(
            f"Skill already exists at {target}. Re-run with --force to overwrite.",
            file=sys.stderr,
        )
        return 1

    package_root = resources.files("long_task_callback").joinpath("skill")
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True, exist_ok=True)

    for item in package_root.iterdir():
        destination = target / item.name
        if item.is_dir():
            shutil.copytree(item, destination)
        else:
            with resources.as_file(item) as source:
                shutil.copy2(source, destination)

    print(f"Installed Codex skill to {target}")
    return 0


def run_systemctl(args: list[str]) -> int:
    command = ["systemctl", "--user", *args]
    result = subprocess.run(command, check=False)
    if result.returncode != 0:
        print(f"codex-long-task-wakeup: warning: {' '.join(shlex.quote(part) for part in command)} exited with {result.returncode}", file=sys.stderr)
    return result.returncode


def systemd_service_is_active(name: str) -> bool:
    result = subprocess.run(
        ["systemctl", "--user", "is-active", "--quiet", name],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return result.returncode == 0


def run_supervisorctl(args: list[str]) -> int:
    command = ["supervisorctl", *args]
    result = subprocess.run(command, check=False)
    if result.returncode != 0:
        print(f"codex-long-task-wakeup: warning: {' '.join(shlex.quote(part) for part in command)} exited with {result.returncode}", file=sys.stderr)
    return result.returncode


def daemon_state_dir() -> Path:
    return codex_home() / "long-task-wakeup"


def service_proxy_env_path() -> Path:
    return daemon_state_dir() / "service-proxy.env"


def daemon_runtime_path() -> Path:
    return daemon_state_dir() / "daemon-runtime.json"


def parse_proxy_environment_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise ValueError(f"could not read proxy environment file {path}: {exc}") from exc

    for line_number, raw_line in enumerate(lines, start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        if "=" not in line:
            raise ValueError(f"invalid environment assignment in {path}:{line_number}")
        name, value = line.split("=", 1)
        name = name.strip()
        if name not in PROXY_ENV_NAMES:
            continue
        value = value.strip()
        if value.startswith(("'", '"')):
            try:
                parsed = shlex.split(value, comments=False, posix=True)
            except ValueError as exc:
                raise ValueError(f"invalid quoted value in {path}:{line_number}") from exc
            if len(parsed) != 1:
                raise ValueError(f"invalid proxy value in {path}:{line_number}")
            value = parsed[0]
        if "\x00" in value or "\n" in value or "\r" in value:
            raise ValueError(f"unsafe proxy value in {path}:{line_number}")
        if any(char.isspace() or char in "'\\\"" for char in value):
            raise ValueError(
                f"proxy value in {path}:{line_number} contains characters unsafe for a systemd environment file; "
                "percent-encode them in the proxy URL"
            )
        values[name] = value
    return values


def write_proxy_environment_file(path: Path, values: dict[str, str]) -> None:
    if not values:
        raise ValueError("proxy environment source did not define any supported proxy variables")
    path.parent.mkdir(parents=True, exist_ok=True)
    contents = "".join(f"{name}={values[name]}\n" for name in PROXY_ENV_NAMES if name in values)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("w", encoding="utf-8") as handle:
            os.chmod(temporary, 0o600)
            handle.write(contents)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
        fsync_directory(path.parent)
    except Exception:
        try:
            temporary.unlink()
        except OSError:
            pass
        raise


def configure_proxy_environment(args: argparse.Namespace) -> Path | None:
    source = getattr(args, "proxy_env_file", None)
    inherit = bool(getattr(args, "inherit_proxy", False))
    clear = bool(getattr(args, "clear_proxy", False))
    if sum(bool(option) for option in (source, inherit, clear)) > 1:
        raise ValueError("--proxy-env-file, --inherit-proxy, and --clear-proxy cannot be used together")
    target = service_proxy_env_path()
    if clear:
        try:
            target.unlink()
            fsync_directory(target.parent)
        except FileNotFoundError:
            pass
        print(f"Cleared proxy environment file at {target}; values were not printed.")
        return None
    if source:
        values = parse_proxy_environment_file(Path(source).expanduser())
    elif inherit:
        values = {name: os.environ[name] for name in PROXY_ENV_NAMES if os.environ.get(name)}
        for name, value in values.items():
            if any(char.isspace() or char in "'\\\"\x00\n\r" for char in value):
                raise ValueError(
                    f"proxy value in environment variable {name} contains characters unsafe for a systemd environment file; "
                    "percent-encode them in the proxy URL"
                )
    else:
        return target if target.exists() else None

    write_proxy_environment_file(target, values)
    print(f"Configured proxy environment file at {target}; values are not printed.")
    return target


def configured_proxy_environment() -> dict[str, str]:
    path = service_proxy_env_path()
    return parse_proxy_environment_file(path) if path.exists() else {}


def load_service_proxy_environment() -> None:
    source = Path(os.environ.get(PROXY_ENV_FILE_ENV, service_proxy_env_path())).expanduser()
    values = parse_proxy_environment_file(source) if source.exists() else {}
    for name in PROXY_ENV_NAMES:
        os.environ.pop(name, None)
    os.environ.update(values)


def daemon_environment(args: argparse.Namespace, *, include_proxy_values: bool = True) -> dict[str, str]:
    values = {
        "PYTHONUNBUFFERED": "1",
        "CODEX_LONG_TASK_WAKEUP_CODEX_BIN": codex_bin_path(args),
        DESKTOP_APP_SERVER_ENV: os.environ.get(DESKTOP_APP_SERVER_ENV, "1"),
        "PATH": getattr(args, "path", None) or os.environ.get("PATH", ""),
        PROXY_ENV_FILE_ENV: str(service_proxy_env_path()),
    }
    if include_proxy_values:
        values.update(configured_proxy_environment())
    return values


def add_proxy_environment_flags(parser: argparse.ArgumentParser) -> None:
    proxy_source = parser.add_mutually_exclusive_group()
    proxy_source.add_argument(
        "--proxy-env-file",
        help="Read only proxy variables from this .env-style file and persist them for the daemon service",
    )
    proxy_source.add_argument(
        "--inherit-proxy",
        action="store_true",
        help="Persist proxy variables from this command environment for the daemon service",
    )
    proxy_source.add_argument(
        "--clear-proxy",
        action="store_true",
        help="Remove the persisted proxy environment file before updating the daemon service",
    )


def write_daemon_runtime() -> None:
    path = daemon_runtime_path()
    payload = {
        "pid": os.getpid(),
        "boot_id": current_boot_id(),
        "reload_protocol": RELOAD_PROTOCOL_VERSION,
        "version": __version__,
        "started_at": time.time(),
    }
    write_request(path, payload)


def daemon_supports_hot_reload(expected_pid: int | None = None) -> bool:
    path = daemon_runtime_path()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return False
    if not isinstance(payload, dict) or payload.get("reload_protocol") != RELOAD_PROTOCOL_VERSION:
        return False
    pid = payload.get("pid")
    return isinstance(pid, int) and (expected_pid is None or pid == expected_pid) and pid_is_running(pid)


def clear_daemon_runtime() -> None:
    path = daemon_runtime_path()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return
    if isinstance(payload, dict) and payload.get("pid") == os.getpid():
        try:
            path.unlink()
            fsync_directory(path.parent)
        except FileNotFoundError:
            pass


def read_pid(path: Path) -> int | None:
    try:
        return int(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def pid_is_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def start_standalone_daemon(args: argparse.Namespace) -> int:
    root = daemon_state_dir()
    root.mkdir(parents=True, exist_ok=True)
    pid_path = root / "daemon.pid"
    log_path = root / "daemon.log"

    existing_pid = read_pid(pid_path)
    if existing_pid is not None and pid_is_running(existing_pid):
        if has_running_callbacks(args):
            print(
                "codex-long-task-wakeup: deferred standalone daemon reload because callback delivery is running; "
                "the existing daemon was left untouched.",
                file=sys.stderr,
            )
            return 0
        elif daemon_supports_hot_reload(existing_pid):
            try:
                os.kill(existing_pid, signal.SIGHUP)
            except ProcessLookupError:
                print(
                    "codex-long-task-wakeup: standalone daemon exited while preparing reload; starting a replacement.",
                    file=sys.stderr,
                )
            else:
                print(f"Reloading standalone wakeup daemon with pid {existing_pid}.")
                print(f"codex-long-task-wakeup: standalone daemon already running with pid {existing_pid}")
                print(f"codex-long-task-wakeup: log file: {log_path}")
                return 0
        else:
            print(f"codex-long-task-wakeup: standalone daemon already running with pid {existing_pid}")
            print(f"codex-long-task-wakeup: log file: {log_path}")
            return 0

    env = os.environ.copy()
    env.update(daemon_environment(args))

    log_file = log_path.open("ab")
    try:
        process = subprocess.Popen(
            daemon_command(args),
            stdin=subprocess.DEVNULL,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            close_fds=True,
            start_new_session=True,
            env=env,
        )
    finally:
        log_file.close()

    pid_path.write_text(f"{process.pid}\n", encoding="utf-8")
    time.sleep(0.2)
    returncode = process.poll()
    if returncode is not None:
        print(
            f"codex-long-task-wakeup: warning: standalone daemon exited immediately with {returncode}; see {log_path}",
            file=sys.stderr,
        )
        return returncode

    print(f"Started standalone wakeup daemon with pid {process.pid}")
    print(f"Standalone daemon pid file: {pid_path}")
    print(f"Standalone daemon log file: {log_path}")
    return 0


def start_supervisord_if_available() -> int:
    if shutil.which("supervisord") is None:
        return 1
    result = subprocess.run(["supervisord"], check=False)
    if result.returncode != 0:
        print("codex-long-task-wakeup: warning: supervisord failed to start", file=sys.stderr)
    return result.returncode


def install_supervisor(args: argparse.Namespace) -> int:
    root = daemon_state_dir()
    root.mkdir(parents=True, exist_ok=True)
    target = supervisor_config_path(args)
    if target.exists() and not args.force:
        print(f"Supervisor program already exists at {target}. Re-run with --force to overwrite.", file=sys.stderr)
        return 1

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(supervisor_config_text(args), encoding="utf-8")
    print(f"Installed supervisor program to {target}")

    if not args.now:
        return 0
    if has_running_callbacks(args):
        print(
            "codex-long-task-wakeup: deferred supervisor update because callback delivery is running; "
            "the installed config will activate after those callbacks drain.",
            file=sys.stderr,
        )
        return 0
    if shutil.which("supervisorctl") is None:
        print("codex-long-task-wakeup: warning: supervisorctl not found; supervisor config was written but not loaded", file=sys.stderr)
        return 1

    status = run_supervisorctl(["reread"])
    if status != 0 and start_supervisord_if_available() == 0:
        status = run_supervisorctl(["reread"])
    status = run_supervisorctl(["update"]) or status
    status = run_supervisorctl(["start", program_name(args.name)]) or status
    if status == 0:
        print(f"Started supervisor-managed wakeup daemon: {program_name(args.name)}")
    return status


def start_daemon_fallback(args: argparse.Namespace) -> int:
    if running_in_container() and (shutil.which("supervisorctl") or shutil.which("supervisord")):
        print("codex-long-task-wakeup: starting supervisor daemon fallback", file=sys.stderr)
        return install_supervisor(args)
    print("codex-long-task-wakeup: starting standalone daemon fallback", file=sys.stderr)
    return start_standalone_daemon(args)


def has_running_callbacks(args: argparse.Namespace) -> bool:
    root = queue_dir(args)
    return any((root / "running").glob("*.json"))


def install_systemd(args: argparse.Namespace) -> int:
    name = service_name(args.name)
    target = systemd_user_dir() / name
    if target.exists() and not args.force:
        print(f"Service already exists at {target}. Re-run with --force to overwrite.", file=sys.stderr)
        return 1
    try:
        if args.print:
            print(systemd_service_text(args), end="")
            return 0
        configure_proxy_environment(args)
        text = systemd_service_text(args)
    except ValueError as exc:
        print(f"codex-long-task-wakeup: error: {exc}", file=sys.stderr)
        return 2

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    print(f"Installed systemd user service to {target}")

    status = 0
    if shutil.which("systemctl"):
        status = run_systemctl(["daemon-reload"])
        if status != 0 and args.now:
            print(
                "codex-long-task-wakeup: warning: systemd user service could not be reloaded; using fallback daemon",
                file=sys.stderr,
            )
            return start_daemon_fallback(args)
        if args.enable:
            status = run_systemctl(["enable", name]) or status
        if args.now:
            if has_running_callbacks(args):
                if daemon_supports_hot_reload():
                    print("Requesting safe wakeup daemon reload after live delivery workers drain.")
                    status = run_systemctl(["reload", name]) or status
                    if status != 0:
                        print(
                            "codex-long-task-wakeup: reload failed; refusing to restart while callback delivery is running.",
                            file=sys.stderr,
                        )
                        return status
                else:
                    print(
                        "codex-long-task-wakeup: deferred daemon activation because callback delivery is running; "
                        "the installed unit is updated, but the existing daemon was left untouched. "
                        "Drain running callbacks, then rerun setup --now to activate this version.",
                        file=sys.stderr,
                    )
                    return status
            elif daemon_supports_hot_reload():
                print("Reloading the running wakeup daemon in place.")
                status = run_systemctl(["reload", name]) or status
                if status != 0:
                    print(
                        "codex-long-task-wakeup: reload failed; refusing to fall back to restart automatically.",
                        file=sys.stderr,
                    )
                    return status
            elif systemd_service_is_active(name):
                print(
                    "codex-long-task-wakeup: deferred activation because the running daemon does not advertise "
                    "safe hot reload; the unit is updated but the process was left untouched.",
                    file=sys.stderr,
                )
                return status
            else:
                action = "restart" if args.enable else "start"
                status = run_systemctl([action, name]) or status
            if status != 0:
                print(
                    "codex-long-task-wakeup: warning: systemd user service could not be started; using fallback daemon",
                    file=sys.stderr,
                )
                fallback_status = start_daemon_fallback(args)
                if fallback_status == 0:
                    return 0
    else:
        print("codex-long-task-wakeup: warning: systemctl not found; service file was written but not loaded", file=sys.stderr)
        if args.enable:
            print("codex-long-task-wakeup: warning: --enable has no effect without systemd", file=sys.stderr)
        if args.now:
            status = start_daemon_fallback(args)

    return status


def setup(args: argparse.Namespace) -> int:
    skill_args = argparse.Namespace(path=args.skill_path, force=args.force)
    skill_status = install_skill(skill_args)
    if skill_status != 0:
        return skill_status

    systemd_args = argparse.Namespace(
        name=args.name,
        queue_dir=args.queue_dir,
        interval=args.interval,
        retries=args.retries,
        retry_delay=args.retry_delay,
        retry_backoff=args.retry_backoff,
        resume_timeout=getattr(args, "resume_timeout", DEFAULT_RESUME_TIMEOUT),
        restart_sec=args.restart_sec,
        exec_start=args.exec_start,
        codex_bin=args.codex_bin,
        path=args.path,
        proxy_env_file=getattr(args, "proxy_env_file", None),
        inherit_proxy=bool(getattr(args, "inherit_proxy", False)),
        clear_proxy=bool(getattr(args, "clear_proxy", False)),
        force=args.force,
        enable=args.enable,
        now=args.now,
        print=False,
    )
    return install_systemd(systemd_args)


def ack(args: argparse.Namespace) -> int:
    root = queue_dir(args)
    ensure_daemon_dirs(root)
    marker = ack_path(root, args.id)
    payload = {
        "id": args.id,
        "marked_at": time.time(),
    }
    if args.message:
        payload["message"] = args.message
    write_request(marker, payload)
    for state in ("failed", "running"):
        source = request_path(root, state, args.id)
        if not source.exists():
            continue
        try:
            request = load_request(source)
            if state == "failed":
                move_request(source, root / "done")
            release_retained_target_lease(root, request)
        except FileNotFoundError:
            continue
        break
    print(f"codex-long-task-wakeup: acknowledged callback {args.id} in {root}", file=sys.stderr)
    return 0


def goal_start(args: argparse.Namespace) -> int:
    root = queue_dir(args)
    ensure_daemon_dirs(root)
    target, source = resolve_target(args)
    goal_id = args.id or uuid.uuid4().hex
    try:
        path = goal_path(root, goal_id)
    except ValueError as exc:
        print(f"codex-long-task-wakeup: error: {exc}", file=sys.stderr)
        return 2
    if args.idle_seconds <= 0:
        print("codex-long-task-wakeup: error: --idle-seconds must be positive", file=sys.stderr)
        return 2
    lock = acquire_owner_lock(root, goal_lock_id(goal_id), blocking=True)
    try:
        if path.exists():
            print(f"codex-long-task-wakeup: goal {goal_id} already exists", file=sys.stderr)
            return 1
        now = time.time()
        write_goal(root, {"version": 1, "id": goal_id, "task": args.task, "cwd": args.cwd, "target": target,
                          "target_source": source, "state": "active", "created_at": now,
                          "last_external_callback_at": now, "idle_seconds": args.idle_seconds})
    finally:
        release_owner_lock(lock, remove=False)
    print(goal_id)
    return 0


def goal_ack(args: argparse.Namespace) -> int:
    root = queue_dir(args)
    try:
        goal_path(root, args.id)
    except ValueError as exc:
        print(f"codex-long-task-wakeup: error: {exc}", file=sys.stderr)
        return 2
    if args.state == "blocked_conditions":
        if not args.condition or not args.condition.strip():
            print("codex-long-task-wakeup: error: --condition is required for blocked_conditions", file=sys.stderr)
            return 2
        if args.email_after <= 0:
            print("codex-long-task-wakeup: error: --email-after must be positive", file=sys.stderr)
            return 2
        if args.email_to and not is_single_email_recipient(args.email_to):
            print("codex-long-task-wakeup: error: invalid email recipient", file=sys.stderr)
            return 2
        configured_recipient = os.environ.get("CODEX_LONG_TASK_WAKEUP_BLOCKED_EMAIL_TO")
        if args.email_to and args.email_to != configured_recipient:
            print("codex-long-task-wakeup: error: --email-to must match CODEX_LONG_TASK_WAKEUP_BLOCKED_EMAIL_TO", file=sys.stderr)
            return 2
    lock = acquire_owner_lock(root, goal_lock_id(args.id), blocking=True)
    try:
        try:
            goal = load_goal(root, args.id)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            print(f"codex-long-task-wakeup: error: {exc}", file=sys.stderr)
            return 1
        if goal.get("state") == "completed" and args.state != "completed":
            print(f"codex-long-task-wakeup: error: completed goal {args.id} is terminal", file=sys.stderr)
            return 1
        now = time.time()
        goal["state"] = args.state
        goal["state_changed_at"] = now
        if args.message:
            goal["message"] = args.message
        if args.state == "blocked_conditions":
            clear_blocked_goal_email(goal)
            goal["condition"] = args.condition
            goal["blocked_email_to"] = args.email_to
            goal["blocked_email_after_seconds"] = args.email_after
        else:
            goal.pop("condition", None)
            clear_blocked_goal_email(goal)
        write_goal(root, goal)
    finally:
        release_owner_lock(lock, remove=False)
    print(f"codex-long-task-wakeup: goal {args.id} acknowledged as {args.state}")
    return 0


def process_blocked_goal_email(root: Path) -> bool:
    now = time.time()
    configured_recipient = os.environ.get("CODEX_LONG_TASK_WAKEUP_BLOCKED_EMAIL_TO")
    for path in sorted((root / "goals").glob("*.json")):
        lock: tuple[object, Path] | None = None
        try:
            goal_id = path.stem
            lock = acquire_owner_lock(root, goal_lock_id(goal_id), blocking=True)
            goal = load_goal(root, goal_id)
            if goal.get("state") != "blocked_conditions":
                continue
            recipient = goal.get("blocked_email_to")
            changed_at = float(goal.get("state_changed_at", now))
            delay = float(goal.get("blocked_email_after_seconds", DEFAULT_BLOCKED_EMAIL_SECONDS))
            if (
                not isinstance(recipient, str)
                or not recipient
                or recipient != configured_recipient
                or not is_single_email_recipient(recipient)
                or goal.get("blocked_email_result") == "accepted"
                or goal.get("blocked_email_exhausted_at")
                or now < changed_at + delay
            ):
                continue
            next_attempt = float(goal.get("blocked_email_next_attempt_at", changed_at + delay))
            if now < next_attempt:
                continue
            goal["blocked_email_attempted_at"] = now
            attempts = int(goal.get("blocked_email_attempts", 0)) + 1
            goal["blocked_email_attempts"] = attempts
            write_goal(root, goal)
            task = str(goal.get("task", goal.get("id"))).replace("\r", " ").replace("\n", " ")
            message = EmailMessage()
            message["To"] = recipient
            message["Subject"] = f"Long-task goal remains blocked: {task}"
            message.set_content(
                f"Goal: {task}\nBlocked condition: {goal.get('condition')}\n"
                f"State id: {goal.get('id')}\nThis confirms local MTA acceptance only."
            )
            try:
                result = subprocess.run([os.environ.get("CODEX_LONG_TASK_WAKEUP_SENDMAIL", "/usr/sbin/sendmail"), "-t", "-oi"],
                                        input=message.as_string(), text=True, stdout=subprocess.DEVNULL,
                                        stderr=subprocess.DEVNULL, timeout=30, check=False)
                goal["blocked_email_result"] = "accepted" if result.returncode == 0 else f"exit_{result.returncode}"
            except (OSError, subprocess.TimeoutExpired) as exc:
                goal["blocked_email_result"] = f"failed_{type(exc).__name__}"
            if goal["blocked_email_result"] != "accepted" and attempts < 3:
                goal["blocked_email_next_attempt_at"] = now + 3600 * (2 ** (attempts - 1))
            elif goal["blocked_email_result"] != "accepted":
                goal["blocked_email_exhausted_at"] = now
            write_goal(root, goal)
            return True
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            print(f"codex-long-task-wakeup: warning: could not process blocked-goal email {path.name}: {exc}", file=sys.stderr)
        finally:
            release_owner_lock(lock, remove=False)
    return False


def goal_resume(args: argparse.Namespace) -> int:
    root = queue_dir(args)
    try:
        goal_path(root, args.id)
    except ValueError as exc:
        print(f"codex-long-task-wakeup: error: {exc}", file=sys.stderr)
        return 2
    lock = acquire_owner_lock(root, goal_lock_id(args.id), blocking=True)
    try:
        try:
            goal = load_goal(root, args.id)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            print(f"codex-long-task-wakeup: error: {exc}", file=sys.stderr)
            return 1
        if goal.get("state") == "completed":
            print("codex-long-task-wakeup: error: completed goals are terminal", file=sys.stderr)
            return 1
        if goal.get("state") != "blocked_conditions":
            print("codex-long-task-wakeup: error: only blocked goals can be resumed", file=sys.stderr)
            return 1
        now = time.time()
        goal.update({"state": "active", "state_changed_at": now, "last_external_callback_at": now})
        goal.pop("condition", None)
        goal.pop("last_reminder_at", None)
        clear_blocked_goal_email(goal)
        write_goal(root, goal)
    finally:
        release_owner_lock(lock, remove=False)
    print(f"codex-long-task-wakeup: goal {args.id} resumed")
    return 0


def cancel(args: argparse.Namespace) -> int:
    root = queue_dir(args)
    if args.all:
        cancel_all(root, args.message)
        return 0
    return 0 if cancel_one(root, args.id, args.message) else 1


def request_task(request: dict[str, object]) -> str:
    prompt = request.get("prompt")
    if isinstance(prompt, str):
        for line in prompt.splitlines():
            if line.startswith("Task: "):
                return line.removeprefix("Task: ").strip() or "long task"
    return "long task"


def request_target(request: dict[str, object]) -> str:
    target = request.get("target")
    if not isinstance(target, dict):
        return "unknown target"
    if target.get("kind") == "session":
        value = str(target.get("value", "unknown"))
        return f"session {value}"
    if target.get("kind") == "last":
        return "unsafe --last"
    return "unknown target"


def queued_items(root: Path, states: list[str]) -> list[tuple[str, Path, dict[str, object]]]:
    items: list[tuple[str, Path, dict[str, object]]] = []
    for state in states:
        for path in sorted((root / state).glob("*.json")):
            try:
                request = load_request(path)
            except Exception as exc:
                request = {"id": path.stem, "prompt": "Task: unreadable callback", "last_error": str(exc)}
            items.append((state, path, request))
    items.sort(key=lambda item: float(item[2].get("created_at", 0.0)))
    return items


def status(args: argparse.Namespace) -> int:
    root = queue_dir(args)
    states = args.state or ["pending", "running"]
    items = queued_items(root, states)
    if not items:
        if not args.quiet_empty:
            print(f"No pending long-task callbacks in {root}")
        return 0

    counts = {state: sum(1 for item in items if item[0] == state) for state in states}
    summary = ", ".join(f"{state}={count}" for state, count in counts.items() if count)
    print(f"[long-task-callback] {len(items)} item(s) need attention ({summary})")
    limit = max(1, args.limit)
    for state, path, request in items[:limit]:
        request_id = str(request.get("id", path.stem))
        line = f"  - [{state}] {request_task(request)} | {request_target(request)} | id {request_id}"
        if state == "running" and ack_path(root, request_id).exists():
            line += " | acknowledged; resumed Codex process still active"
        error = request.get("last_error")
        if isinstance(error, str) and error:
            line += f" | {error}"
        print(line)
    if len(items) > limit:
        print(f"  ... {len(items) - limit} more item(s)")
    print("  Inspect: codex-long-task-wakeup status --state pending --state running")
    return 0


def shell_hook_text(command: str) -> str:
    quoted = shlex.quote(command)
    return "\n".join(
        [
            SHELL_HOOK_BEGIN,
            'if [[ -z "${CODEX_LONG_TASK_STATUS_SHOWN:-}" ]]; then',
            "    export CODEX_LONG_TASK_STATUS_SHOWN=1",
            "    if command -v timeout >/dev/null 2>&1; then",
            f"        timeout 2s {quoted} status --shell-hook --quiet-empty 2>/dev/null || true",
            "    else",
            f"        {quoted} status --shell-hook --quiet-empty 2>/dev/null || true",
            "    fi",
            "fi",
            SHELL_HOOK_END,
        ]
    )


def install_shell_hook(args: argparse.Namespace) -> int:
    rc_file = Path(args.rc_file or "~/.bashrc").expanduser()
    command = args.command or console_script_path()
    block = shell_hook_text(command)
    existing = rc_file.read_text(encoding="utf-8") if rc_file.exists() else ""

    start = existing.find(SHELL_HOOK_BEGIN)
    end = existing.find(SHELL_HOOK_END)
    if start >= 0 and end >= start:
        end += len(SHELL_HOOK_END)
        updated = existing[:start].rstrip() + "\n\n" + block + existing[end:]
    else:
        updated = existing.rstrip() + "\n\n" + block + "\n"

    rc_file.parent.mkdir(parents=True, exist_ok=True)
    mode = rc_file.stat().st_mode if rc_file.exists() else None
    tmp = rc_file.with_name(f".{rc_file.name}.{uuid.uuid4().hex}.tmp")
    tmp.write_text(updated, encoding="utf-8")
    if mode is not None:
        tmp.chmod(mode)
    os.replace(tmp, rc_file)
    print(f"Installed long-task pending-status hook in {rc_file}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Explicit callback tool for waking Codex after a long task.")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="mode", required=True)

    done_parser = sub.add_parser("done", help="Wake Codex after an externally managed task finishes")
    add_common_flags(done_parser)

    run_parser = sub.add_parser("run", help="Run a command and wake Codex when it exits")
    add_common_flags(run_parser)
    run_parser.add_argument("wrapped_command", nargs=argparse.REMAINDER)

    daemon_parser = sub.add_parser("daemon", help="Process queued wakeup requests outside Codex tool sandboxes")
    daemon_parser.add_argument("--queue-dir", help="Wakeup queue directory")
    daemon_parser.add_argument("--interval", type=float, default=2.0, help="Polling interval in seconds")
    daemon_parser.add_argument("--once", action="store_true", help="Exit after the queue is empty")
    daemon_parser.add_argument("--max-items", type=int, help="Exit after processing this many queued requests")
    daemon_parser.add_argument("--retries", type=int, default=DEFAULT_RETRIES, help="Retries after a callback is not acknowledged")
    daemon_parser.add_argument("--retry-delay", type=float, default=DEFAULT_RETRY_DELAY, help="Initial retry delay in seconds")
    daemon_parser.add_argument("--retry-backoff", type=float, default=DEFAULT_RETRY_BACKOFF, help="Retry delay multiplier")
    daemon_parser.add_argument("--resume-timeout", type=float, default=DEFAULT_RESUME_TIMEOUT, help="Maximum seconds for one Codex resume before retrying")

    systemd_parser = sub.add_parser("install-systemd", help="Install a user-level systemd service for the wakeup daemon")
    systemd_parser.add_argument("--name", default="codex-long-task-wakeup", help="Systemd service name")
    systemd_parser.add_argument("--queue-dir", help="Wakeup queue directory")
    systemd_parser.add_argument("--interval", type=float, default=2.0, help="Daemon polling interval in seconds")
    systemd_parser.add_argument("--retries", type=int, default=DEFAULT_RETRIES, help="Retries after a callback is not acknowledged")
    systemd_parser.add_argument("--retry-delay", type=float, default=DEFAULT_RETRY_DELAY, help="Initial retry delay in seconds")
    systemd_parser.add_argument("--retry-backoff", type=float, default=DEFAULT_RETRY_BACKOFF, help="Retry delay multiplier")
    systemd_parser.add_argument("--resume-timeout", type=float, default=DEFAULT_RESUME_TIMEOUT, help="Maximum seconds for one Codex resume before retrying")
    systemd_parser.add_argument("--restart-sec", type=float, default=5.0, help="Restart delay in seconds")
    systemd_parser.add_argument("--exec-start", help="Path to codex-long-task-wakeup executable")
    systemd_parser.add_argument("--codex-bin", help="Path to codex executable used by the daemon")
    systemd_parser.add_argument("--path", help="PATH environment for the daemon service")
    add_proxy_environment_flags(systemd_parser)
    systemd_parser.add_argument("--force", action="store_true", help="Overwrite an existing service file")
    systemd_parser.add_argument("--enable", action="store_true", help="Run systemctl --user enable after writing the service")
    systemd_parser.add_argument("--now", action="store_true", help="Start or restart the service after writing it")
    systemd_parser.add_argument("--print", action="store_true", help="Print the service file instead of writing it")

    install_parser = sub.add_parser("install-skill", help="Install the bundled Codex skill into CODEX_HOME")
    install_parser.add_argument("--path", help="Skills directory to install into (defaults to ${CODEX_HOME:-~/.codex}/skills)")
    install_parser.add_argument("--force", action="store_true", help="Overwrite an existing long-task-callback skill")

    setup_parser = sub.add_parser("setup", help="Install the bundled skill and user-level wakeup daemon")
    setup_parser.add_argument("--skill-path", help="Skills directory to install into (defaults to ${CODEX_HOME:-~/.codex}/skills)")
    setup_parser.add_argument("--name", default="codex-long-task-wakeup", help="Systemd service name")
    setup_parser.add_argument("--queue-dir", help="Wakeup queue directory")
    setup_parser.add_argument("--interval", type=float, default=2.0, help="Daemon polling interval in seconds")
    setup_parser.add_argument("--retries", type=int, default=DEFAULT_RETRIES, help="Retries after a callback is not acknowledged")
    setup_parser.add_argument("--retry-delay", type=float, default=DEFAULT_RETRY_DELAY, help="Initial retry delay in seconds")
    setup_parser.add_argument("--retry-backoff", type=float, default=DEFAULT_RETRY_BACKOFF, help="Retry delay multiplier")
    setup_parser.add_argument("--resume-timeout", type=float, default=DEFAULT_RESUME_TIMEOUT, help="Maximum seconds for one Codex resume before retrying")
    setup_parser.add_argument("--restart-sec", type=float, default=5.0, help="Systemd restart delay in seconds")
    setup_parser.add_argument("--exec-start", help="Path to codex-long-task-wakeup executable")
    setup_parser.add_argument("--codex-bin", help="Path to codex executable used by the daemon")
    setup_parser.add_argument("--path", help="PATH environment for the daemon service")
    add_proxy_environment_flags(setup_parser)
    setup_parser.add_argument("--force", action="store_true", help="Overwrite an existing skill and service file")
    setup_parser.add_argument("--enable", action="store_true", help="Run systemctl --user enable after writing the service")
    setup_parser.add_argument("--now", action="store_true", help="Start or restart the service after writing it")

    ack_parser = sub.add_parser("ack", help="Mark a daemon callback as successfully received")
    ack_parser.add_argument("--queue-dir", help="Wakeup queue directory")
    ack_parser.add_argument("--id", required=True, help="Callback request id to acknowledge")
    ack_parser.add_argument("--message", help="Optional acknowledgement note")

    goal_parser = sub.add_parser("goal", help="Manage persistent long-running goal acknowledgements")
    goal_sub = goal_parser.add_subparsers(dest="goal_mode", required=True)
    goal_start_parser = goal_sub.add_parser("start", help="Create an active goal")
    goal_start_parser.add_argument("--id", help="Optional stable goal id")
    goal_start_parser.add_argument("--queue-dir", help="Queue root that owns the goal")
    goal_start_parser.add_argument("--session", required=True, help="Bound Codex session")
    goal_start_parser.add_argument("--cwd", default=os.getcwd())
    goal_start_parser.add_argument("--task", required=True)
    goal_start_parser.add_argument("--idle-seconds", type=float, default=DEFAULT_GOAL_IDLE_SECONDS)
    goal_ack_parser = goal_sub.add_parser("ack", help="Acknowledge completion or blocked conditions")
    goal_ack_parser.add_argument("--queue-dir", help="Queue root that owns the goal")
    goal_ack_parser.add_argument("--id", required=True)
    goal_ack_parser.add_argument("--state", choices=["completed", "blocked_conditions"], required=True)
    goal_ack_parser.add_argument("--message")
    goal_ack_parser.add_argument("--condition")
    goal_ack_parser.add_argument("--email-to")
    goal_ack_parser.add_argument("--email-after", type=float, default=DEFAULT_BLOCKED_EMAIL_SECONDS)
    goal_resume_parser = goal_sub.add_parser("resume", help="Explicitly resume a blocked goal")
    goal_resume_parser.add_argument("--queue-dir", help="Queue root that owns the goal")
    goal_resume_parser.add_argument("--id", required=True)

    cancel_parser = sub.add_parser("cancel", help="Cancel queued callbacks before they retry or complete")
    cancel_parser.add_argument("--queue-dir", help="Wakeup queue directory")
    cancel_target = cancel_parser.add_mutually_exclusive_group(required=True)
    cancel_target.add_argument("--id", help="Callback request id to cancel")
    cancel_target.add_argument("--all", action="store_true", help="Cancel all active, pending, and running callbacks")
    cancel_parser.add_argument("--message", help="Optional cancellation note")

    status_parser = sub.add_parser("status", help="List queued callbacks that still need handling")
    status_parser.add_argument("--queue-dir", help="Wakeup queue directory")
    status_parser.add_argument(
        "--state",
        action="append",
        choices=[ACTIVE_STATE, "pending", "running", "failed"],
        help="Queue state to include; repeat as needed (default: pending and running)",
    )
    status_parser.add_argument("--limit", type=int, default=5, help="Maximum items to print")
    status_parser.add_argument("--quiet-empty", action="store_true", help="Print nothing when no items exist")
    status_parser.add_argument("--shell-hook", action="store_true", help="Use compact terminal-startup output")

    shell_hook_parser = sub.add_parser("install-shell-hook", help="Install an idempotent pending-status check in Bash startup")
    shell_hook_parser.add_argument("--rc-file", help="Bash rc file (default: ~/.bashrc)")
    shell_hook_parser.add_argument("--command", help="Executable path embedded in the hook")

    args = parser.parse_args()
    if args.mode == "done":
        return done(args)
    if args.mode == "run":
        if args.wrapped_command and args.wrapped_command[0] == "--":
            args.wrapped_command = args.wrapped_command[1:]
        return run(args)
    if args.mode == "daemon":
        return daemon(args)
    if args.mode == "install-systemd":
        return install_systemd(args)
    if args.mode == "install-skill":
        return install_skill(args)
    if args.mode == "setup":
        return setup(args)
    if args.mode == "ack":
        return ack(args)
    if args.mode == "goal":
        if args.goal_mode == "start":
            return goal_start(args)
        if args.goal_mode == "ack":
            return goal_ack(args)
        return goal_resume(args)
    if args.mode == "cancel":
        return cancel(args)
    if args.mode == "status":
        return status(args)
    if args.mode == "install-shell-hook":
        return install_shell_hook(args)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
