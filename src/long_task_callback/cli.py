from __future__ import annotations

import argparse
import importlib.resources as resources
import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path


def build_prompt(args: argparse.Namespace, duration: float | None = None) -> str:
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
    return "\n".join(lines)


def truthy_env(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def queue_dir(args: argparse.Namespace | None = None) -> Path:
    explicit = getattr(args, "queue_dir", None) if args is not None else None
    path = explicit or os.environ.get("CODEX_LONG_TASK_WAKEUP_QUEUE_DIR")
    if path:
        return Path(path).expanduser()
    return codex_home() / "long-task-wakeup" / "queue"


def codex_command() -> str:
    return os.environ.get("CODEX_LONG_TASK_WAKEUP_CODEX_BIN", "codex")


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
    }


def enqueue_request(args: argparse.Namespace, prompt: str) -> int:
    root = queue_dir(args)
    pending = root / "pending"
    pending.mkdir(parents=True, exist_ok=True)

    request = make_request(args, prompt)
    request_id = str(request["id"])
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
    for name in ("pending", "running", "done", "failed"):
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


def process_one(root: Path) -> bool:
    ensure_daemon_dirs(root)
    pending = sorted((root / "pending").glob("*.json"))
    if not pending:
        return False

    path = pending[0]
    running = root / "running" / path.name
    try:
        os.replace(path, running)
    except FileNotFoundError:
        return True

    try:
        request = load_request(running)
        result = subprocess.run(
            resume_command(request),
            input=str(request["prompt"]),
            text=True,
            cwd=str(request["cwd"]),
            check=False,
        )
        destination_dir = root / ("done" if result.returncode == 0 else "failed")
        if result.returncode != 0:
            print(
                f"codex-long-task-wakeup: warning: daemon callback {running.name} exited with {result.returncode}",
                file=sys.stderr,
            )
    except Exception as exc:
        destination_dir = root / "failed"
        print(f"codex-long-task-wakeup: warning: daemon failed to process {running.name}: {exc}", file=sys.stderr)

    destination = destination_dir / running.name
    if destination.exists():
        destination = destination_dir / f"{running.stem}.{int(time.time())}.json"
    os.replace(running, destination)
    return True


def daemon(args: argparse.Namespace) -> int:
    root = queue_dir(args)
    ensure_daemon_dirs(root)
    print(f"codex-long-task-wakeup: daemon watching {root}", file=sys.stderr)

    processed = 0
    while True:
        did_work = process_one(root)
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


def main() -> int:
    parser = argparse.ArgumentParser(description="Explicit callback tool for waking Codex after a long task.")
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

    install_parser = sub.add_parser("install-skill", help="Install the bundled Codex skill into CODEX_HOME")
    install_parser.add_argument("--path", help="Skills directory to install into (defaults to ${CODEX_HOME:-~/.codex}/skills)")
    install_parser.add_argument("--force", action="store_true", help="Overwrite an existing long-task-callback skill")

    args = parser.parse_args()
    if args.mode == "done":
        return done(args)
    if args.mode == "run":
        if args.wrapped_command and args.wrapped_command[0] == "--":
            args.wrapped_command = args.wrapped_command[1:]
        return run(args)
    if args.mode == "daemon":
        return daemon(args)
    if args.mode == "install-skill":
        return install_skill(args)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
