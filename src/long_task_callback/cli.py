from __future__ import annotations

import argparse
import importlib.resources as resources
import json
import os
import shlex
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

from . import __version__

DEFAULT_APPROVALS_REVIEWER = "auto_review"
DEFAULT_APPROVAL_POLICY = "on-request"
DEFAULT_SANDBOX_MODE = "workspace-write"
DEFAULT_RETRIES = 3
DEFAULT_RETRY_DELAY = 30.0
DEFAULT_RETRY_BACKOFF = 2.0


def build_prompt(
    args: argparse.Namespace,
    duration: float | None = None,
    acknowledgement: str | None = None,
) -> str:
    lines = [
        "[long-task-callback]",
        "A long-running task explicitly called back into Codex.",
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


def codex_command() -> str:
    return os.environ.get("CODEX_LONG_TASK_WAKEUP_CODEX_BIN", "codex")


def systemd_user_dir() -> Path:
    config_home = os.environ.get("XDG_CONFIG_HOME")
    if config_home:
        return Path(config_home).expanduser() / "systemd" / "user"
    return Path("~/.config/systemd/user").expanduser()


def systemd_quote(value: str) -> str:
    if value and all(char not in value for char in " \t\n\"'\\"):
        return value
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def service_name(name: str) -> str:
    return name if name.endswith(".service") else f"{name}.service"


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
            "Restart=always",
            f"RestartSec={args.restart_sec}",
            "Environment=PYTHONUNBUFFERED=1",
            f"Environment={systemd_quote(f'PATH={path}')}",
            f"Environment={systemd_quote(f'CODEX_LONG_TASK_WAKEUP_CODEX_BIN={codex_bin}')}",
            "",
            "[Install]",
            "WantedBy=default.target",
            "",
        ]
    )


def daemon_command(args: argparse.Namespace) -> list[str]:
    command = [
        args.exec_start or console_script_path(),
        "daemon",
        "--interval",
        str(args.interval),
        "--retries",
        str(args.retries),
        "--retry-delay",
        str(args.retry_delay),
        "--retry-backoff",
        str(args.retry_backoff),
    ]
    if args.queue_dir:
        command.extend(["--queue-dir", str(Path(args.queue_dir).expanduser())])
    return command


def make_request(args: argparse.Namespace, prompt: str) -> dict[str, object]:
    if args.session:
        target = {"kind": "session", "value": args.session}
    elif args.last:
        target = {"kind": "last"}
    else:
        raise SystemExit("Pass --session <id> for the target Codex session, or --last as an explicit fallback.")
    return {
        "version": 1,
        "id": uuid.uuid4().hex,
        "created_at": time.time(),
        "cwd": args.cwd,
        "target": target,
        "prompt": prompt,
        "approvals_reviewer": getattr(args, "approvals_reviewer", None) or DEFAULT_APPROVALS_REVIEWER,
        "approval_policy": getattr(args, "approval_policy", None) or DEFAULT_APPROVAL_POLICY,
        "sandbox_mode": getattr(args, "sandbox_mode", None) or DEFAULT_SANDBOX_MODE,
    }


def enqueue_request(args: argparse.Namespace, prompt: str) -> int:
    root = queue_dir(args)
    pending = root / "pending"
    pending.mkdir(parents=True, exist_ok=True)

    request = make_request(args, prompt)
    request_id = str(request["id"])
    request["queue_dir"] = str(root)
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
    request["prompt"] = f"{prompt}\n\n{build_acknowledgement_text(acknowledgement)}"
    tmp = pending / f".{request_id}.json.tmp"
    target = pending / f"{request_id}.json"
    try:
        tmp.write_text(json.dumps(request, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(tmp, target)
    except OSError as exc:
        try:
            tmp.unlink()
        except OSError:
            pass
        print(f"codex-long-task-wakeup: warning: failed to enqueue callback: {exc}", file=sys.stderr)
        return 1

    print(f"codex-long-task-wakeup: queued callback {request_id} in {root}", file=sys.stderr)
    return 0


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
    if args.dry_run:
        print(prompt)
        return 0
    if should_enqueue(args):
        return enqueue_request(args, prompt)

    request = make_request(args, prompt)
    cmd = resume_command(request)

    try:
        result = subprocess.run(
            cmd,
            input=prompt,
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
    for name in ("pending", "running", "done", "failed", "acks"):
        (root / name).mkdir(parents=True, exist_ok=True)


def load_request(path: Path) -> dict[str, object]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("request must be a JSON object")
    if data.get("version") != 1:
        raise ValueError("unsupported request version")
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


def select_pending(root: Path, now: float) -> Path | None:
    for path in sorted((root / "pending").glob("*.json")):
        if next_attempt_at(path) <= now:
            return path
    return None


def retry_delay(args: argparse.Namespace, attempt: int) -> float:
    delay = max(0.0, float(getattr(args, "retry_delay", DEFAULT_RETRY_DELAY)))
    backoff = max(1.0, float(getattr(args, "retry_backoff", DEFAULT_RETRY_BACKOFF)))
    return delay * (backoff ** max(0, attempt - 1))


def write_request(path: Path, request: dict[str, object]) -> None:
    tmp = path.with_name(f".{path.stem}.{uuid.uuid4().hex}.tmp")
    tmp.write_text(json.dumps(request, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def move_request(source: Path, destination_dir: Path) -> None:
    destination = destination_dir / source.name
    if destination.exists():
        destination = destination_dir / f"{source.stem}.{int(time.time())}.json"
    os.replace(source, destination)


def process_one(root: Path, args: argparse.Namespace) -> bool:
    ensure_daemon_dirs(root)
    path = select_pending(root, time.time())
    if path is None:
        return False

    running = root / "running" / path.name
    try:
        os.replace(path, running)
    except FileNotFoundError:
        return True

    try:
        request = load_request(running)
        request_id = str(request.get("id", running.stem))
        attempts = int(request.get("attempts", 0)) + 1
        request["attempts"] = attempts
        request.pop("next_attempt_at", None)
        write_request(running, request)
        try:
            ack_path(root, request_id).unlink()
        except FileNotFoundError:
            pass
        result = subprocess.run(
            resume_command(request),
            input=str(request["prompt"]),
            text=True,
            cwd=str(request["cwd"]),
            check=False,
        )
        acked = ack_path(root, request_id).exists()
        destination_dir = root / ("done" if acked else "failed")
        if result.returncode != 0:
            print(
                f"codex-long-task-wakeup: warning: daemon callback {running.name} exited with {result.returncode}",
                file=sys.stderr,
            )
        if not acked:
            request["last_error"] = f"missing acknowledgement marker after exit {result.returncode}"
            max_retries = max(0, int(getattr(args, "retries", DEFAULT_RETRIES)))
            if attempts <= max_retries:
                delay = retry_delay(args, attempts)
                request["next_attempt_at"] = time.time() + delay
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

    move_request(running, destination_dir)
    return True


def daemon(args: argparse.Namespace) -> int:
    root = queue_dir(args)
    ensure_daemon_dirs(root)
    print(f"codex-long-task-wakeup: daemon watching {root}", file=sys.stderr)

    processed = 0
    while True:
        did_work = process_one(root, args)
        if did_work:
            processed += 1
            if args.max_items is not None and processed >= args.max_items:
                return 0
            continue
        if args.once:
            return 0
        time.sleep(args.interval)


def done(args: argparse.Namespace) -> int:
    callback_code = resume_codex(args, build_prompt(args))
    return callback_code if args.strict else 0


def run(args: argparse.Namespace) -> int:
    if not args.wrapped_command:
        raise SystemExit("run mode requires a command after --")

    started = time.time()
    exit_code = 1
    try:
        completed = subprocess.run(
            args.wrapped_command,
            cwd=args.cwd,
            shell=False,
            check=False,
        )
        exit_code = completed.returncode
        return exit_code
    finally:
        args.exit_code = exit_code
        args.command = args.command or " ".join(args.wrapped_command)
        duration = time.time() - started
        prompt = build_prompt(args, duration)
        callback_code = resume_codex(args, prompt)
        if args.strict and exit_code == 0 and callback_code != 0:
            raise SystemExit(callback_code)


def add_common_flags(parser: argparse.ArgumentParser) -> None:
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--session", help="Codex session id to resume")
    target.add_argument("--last", action="store_true", help="Resume the most recent Codex session")
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


def daemon_state_dir() -> Path:
    return codex_home() / "long-task-wakeup"


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
        print(f"codex-long-task-wakeup: standalone daemon already running with pid {existing_pid}")
        print(f"codex-long-task-wakeup: log file: {log_path}")
        return 0

    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    env["CODEX_LONG_TASK_WAKEUP_CODEX_BIN"] = codex_bin_path(args)
    if args.path:
        env["PATH"] = args.path

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


def install_systemd(args: argparse.Namespace) -> int:
    name = service_name(args.name)
    text = systemd_service_text(args)
    if args.print:
        print(text, end="")
        return 0

    target = systemd_user_dir() / name
    if target.exists() and not args.force:
        print(f"Service already exists at {target}. Re-run with --force to overwrite.", file=sys.stderr)
        return 1

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    print(f"Installed systemd user service to {target}")

    status = 0
    if shutil.which("systemctl"):
        status = run_systemctl(["daemon-reload"])
        if args.enable:
            status = run_systemctl(["enable", name]) or status
        if args.now:
            action = "restart" if args.enable else "start"
            status = run_systemctl([action, name]) or status
    else:
        print("codex-long-task-wakeup: warning: systemctl not found; service file was written but not loaded", file=sys.stderr)
        if args.enable:
            print("codex-long-task-wakeup: warning: --enable has no effect without systemd", file=sys.stderr)
        if args.now:
            print("codex-long-task-wakeup: starting standalone daemon fallback", file=sys.stderr)
            status = start_standalone_daemon(args)

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
        restart_sec=args.restart_sec,
        exec_start=args.exec_start,
        codex_bin=args.codex_bin,
        path=args.path,
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
    tmp = marker.with_name(f".{marker.stem}.{uuid.uuid4().hex}.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, marker)
    print(f"codex-long-task-wakeup: acknowledged callback {args.id} in {root}", file=sys.stderr)
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

    systemd_parser = sub.add_parser("install-systemd", help="Install a user-level systemd service for the wakeup daemon")
    systemd_parser.add_argument("--name", default="codex-long-task-wakeup", help="Systemd service name")
    systemd_parser.add_argument("--queue-dir", help="Wakeup queue directory")
    systemd_parser.add_argument("--interval", type=float, default=2.0, help="Daemon polling interval in seconds")
    systemd_parser.add_argument("--retries", type=int, default=DEFAULT_RETRIES, help="Retries after a callback is not acknowledged")
    systemd_parser.add_argument("--retry-delay", type=float, default=DEFAULT_RETRY_DELAY, help="Initial retry delay in seconds")
    systemd_parser.add_argument("--retry-backoff", type=float, default=DEFAULT_RETRY_BACKOFF, help="Retry delay multiplier")
    systemd_parser.add_argument("--restart-sec", type=float, default=5.0, help="Restart delay in seconds")
    systemd_parser.add_argument("--exec-start", help="Path to codex-long-task-wakeup executable")
    systemd_parser.add_argument("--codex-bin", help="Path to codex executable used by the daemon")
    systemd_parser.add_argument("--path", help="PATH environment for the daemon service")
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
    setup_parser.add_argument("--restart-sec", type=float, default=5.0, help="Systemd restart delay in seconds")
    setup_parser.add_argument("--exec-start", help="Path to codex-long-task-wakeup executable")
    setup_parser.add_argument("--codex-bin", help="Path to codex executable used by the daemon")
    setup_parser.add_argument("--path", help="PATH environment for the daemon service")
    setup_parser.add_argument("--force", action="store_true", help="Overwrite an existing skill and service file")
    setup_parser.add_argument("--enable", action="store_true", help="Run systemctl --user enable after writing the service")
    setup_parser.add_argument("--now", action="store_true", help="Start or restart the service after writing it")

    ack_parser = sub.add_parser("ack", help="Mark a daemon callback as successfully received")
    ack_parser.add_argument("--queue-dir", help="Wakeup queue directory")
    ack_parser.add_argument("--id", required=True, help="Callback request id to acknowledge")
    ack_parser.add_argument("--message", help="Optional acknowledgement note")

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
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
