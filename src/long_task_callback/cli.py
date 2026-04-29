from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time


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


def resume_codex(args: argparse.Namespace, prompt: str) -> int:
    if args.dry_run:
        print(prompt)
        return 0

    cmd = ["codex", "exec", "resume", "--all"]
    if args.session:
        cmd.append(args.session)
    elif args.last:
        cmd.append("--last")
    else:
        raise SystemExit("Pass --session <id> for the target Codex session, or --last as an explicit fallback.")
    cmd.append("-")

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
    parser.add_argument("--dry-run", action="store_true", help="Print the wakeup prompt instead of resuming Codex")
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Propagate callback failure. By default callback failure never changes task success or exit code.",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Explicit callback tool for waking Codex after a long task.")
    sub = parser.add_subparsers(dest="mode", required=True)

    done_parser = sub.add_parser("done", help="Wake Codex after an externally managed task finishes")
    add_common_flags(done_parser)

    run_parser = sub.add_parser("run", help="Run a command and wake Codex when it exits")
    add_common_flags(run_parser)
    run_parser.add_argument("wrapped_command", nargs=argparse.REMAINDER)

    args = parser.parse_args()
    if args.mode == "done":
        return done(args)
    if args.mode == "run":
        if args.wrapped_command and args.wrapped_command[0] == "--":
            args.wrapped_command = args.wrapped_command[1:]
        return run(args)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
