"""Per-user Task Scheduler coordinator, separate from every business runner.

The scheduled supervisor replaces a drained daemon on reload; Windows execv is
not an in-place process replacement. Configuration and environment stay in a
private file, never in the registered task's arguments.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time

from .platforms import windows, windows_io, windows_process
from .runtime import worker_command


def service_owner(name: str, profile: Path) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,120}", name):
        raise ValueError("service name must contain only letters, numbers, '.', '_' or '-'")
    key = hashlib.sha256(os.path.normcase(str(profile.resolve())).encode("utf-8")).hexdigest()[:16]
    return f"ltc-coordinator-{key}-{name}"


def config_path(name: str) -> Path:
    from . import cli
    service_owner(name, cli.codex_home())  # Validate before making a path.
    return cli.daemon_state_dir() / f"windows-{name}.json"


def configuration(args: argparse.Namespace) -> dict[str, object]:
    from . import cli
    root = cli.queue_dir(args).resolve()
    command = cli.daemon_command(args)
    if "--queue-dir" in command:
        command[command.index("--queue-dir") + 1] = str(root)
    else:
        command.extend(["--queue-dir", str(root)])
    environment = cli.daemon_environment(args, include_proxy_values=False)
    environment.update(CODEX_HOME=str(cli.codex_home().resolve()),
                       CLAUDE_CONFIG_DIR=str(cli.claude_home().resolve()),
                       PYTHONIOENCODING="utf-8")
    for name in (cli.TARGET_LOCK_DIR_ENV, cli.APP_SERVER_BRIDGE_FILE_ENV):
        if name in os.environ:
            environment[name] = os.environ[name]
    return {
        "version": 1, "run": True,
        "owner": service_owner(args.name, cli.codex_home()),
        "command": command, "queue_dir": str(root),
        "environment": environment, "cwd": str(cli.daemon_state_dir().resolve()),
        "log_path": str(cli.daemon_state_dir().resolve() / "daemon.log"),
        "restart_sec": max(1.0, float(args.restart_sec)),
    }


def install(args: argparse.Namespace) -> int:
    from . import cli
    try:
        config = configuration(args)
        target = config_path(args.name).resolve()
        command = worker_command("_windows-coordinator", "--config-file", str(target))
        definition = windows.task_configuration(command, Path(str(config["cwd"])), login=args.enable)
        if args.print:
            # Environment may contain private proxy/profile settings. A preview
            # shows the actual registered invocation, not the private payload.
            print(json.dumps({"owner": config["owner"], "configuration": definition}, ensure_ascii=False, indent=2))
            return 0
        if sys.platform != "win32":
            raise ValueError("Windows coordinator requires native Windows")
        if target.exists() and not args.force:
            print(f"Coordinator configuration exists at {target}; use --force to update.", file=sys.stderr)
            return 1
        if target.exists():
            previous = json.loads(target.read_text(encoding="utf-8"))
            if (previous.get("queue_dir") != config["queue_dir"]
                    and windows_io.probe_existing_lock(target.with_suffix(".supervisor.lock"))):
                raise ValueError("stop and drain this coordinator before changing its queue")
        runtime = cli.read_daemon_runtime()
        if (runtime and runtime.get("queue_dir") != config["queue_dir"]
                and cli.daemon_supports_hot_reload(runtime.get("pid"))):
            raise ValueError("this Agent profile already has another queue coordinator; use a separate profile")
        state = windows.scheduler_call("status", {"owner": config["owner"]})
        # Pin the queue/profile and write configuration before registration.
        cli.configure_proxy_environment(args)
        windows_io.ensure_private_directory(target.parent)
        cli.write_request(target, config)
        windows.scheduler_call("register", {"owner": config["owner"], "configuration": definition,
                                             "replace": bool(state.get("registered"))})
        print(f"Installed Windows coordinator {config['owner']}; private configuration: {target}")
        if not args.now:
            return 0
        root = Path(str(config["queue_dir"]))
        admission = cli.acquire_owner_lock(root, "daemon-singleton", blocking=False)
        if admission is None:
            runtime = cli.read_daemon_runtime()
            pid = runtime.get("pid") if runtime else None
            if type(pid) is int and cli.send_standalone_reload(pid, expected_queue=root):
                # IgnoreNew preserves a live supervisor. If only its daemon
                # survived, a replacement supervisor waits for that daemon's
                # identity-verified drain instead of losing the restart.
                windows.scheduler_call("run", {"owner": config["owner"]})
                print("Requested coordinator replacement after active callback deliveries drain.")
                return 0
            print("ltc: existing coordinator identity is unverified; left running without replacement", file=sys.stderr)
            return 1
        cli.release_owner_lock(admission, remove=False)
        # IgnoreNew and the supervisor/daemon locks arbitrate concurrent setup.
        windows.scheduler_call("run", {"owner": config["owner"]})
        print(f"Started coordinator; log: {config['log_path']}")
        return 0
    except (OSError, ValueError, windows.SchedulerError, subprocess.TimeoutExpired) as error:
        print(f"ltc: Windows coordinator configuration failed ({type(error).__name__}: {error})", file=sys.stderr)
        return 2


def run_coordinator(path: Path) -> int:
    """Supervise only the coordinator; never own/replay business commands."""
    if os.name != "nt":
        return 125
    path = path.resolve()
    lock = windows_io.acquire_path_lock(path.with_suffix(".supervisor.lock"), blocking=False)
    if lock is None:
        return 1
    try:
        while True:
            config = json.loads(path.read_text(encoding="utf-8"))
            if config.get("version") != 1:
                raise ValueError("unsupported coordinator configuration")
            if not config.get("run"):
                return 0
            queue_lock = Path(config["queue_dir"]) / "locks" / "daemon-singleton.lock"
            if windows_io.probe_existing_lock(queue_lock):
                time.sleep(0.1)
                continue
            environment = os.environ.copy()
            environment.update(config["environment"])
            environment.pop("CODEX_THREAD_ID", None)
            environment.pop("CLAUDE_CODE_SESSION_ID", None)
            environment.pop("CLAUDECODE", None)
            command = windows_process.prepare_command(config["command"], env=environment)
            with Path(config["log_path"]).open("a", encoding="utf-8") as log:
                process = subprocess.Popen(command, cwd=config["cwd"], env=environment,
                                           stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                           close_fds=True, **windows_process.background_popen_kwargs())
                result = process.wait()
            if result == 1:  # Another owner won the queue lock.
                return result
            if not json.loads(path.read_text(encoding="utf-8")).get("run"):
                return 0
            if result != 75:
                time.sleep(max(1.0, float(config["restart_sec"])))
    finally:
        lock[0].close()


def uninstall(args: argparse.Namespace) -> int:
    """Remove login registration, allowing live callback owners to drain."""
    from . import cli
    try:
        path = config_path(args.name)
        config = json.loads(path.read_text(encoding="utf-8"))
        root = Path(str(config["queue_dir"]))
        runtime = cli.read_daemon_runtime()
        active = windows_io.probe_existing_lock(cli.owner_lock_path(root, "daemon-singleton"))
        if active and (not runtime or not cli.daemon_supports_hot_reload(runtime.get("pid"), expected_queue=root)):
            raise ValueError("coordinator identity is unverified; inspect before removal")
        config["run"] = False
        cli.write_request(path, config)
        if active:
            cli.write_request(root / "daemon-stop.json", {"pid": runtime["pid"],
                              "process_identity": runtime["process_identity"]})
        # Deleting a registration does not stop an already running instance.
        windows.scheduler_call("delete", {"owner": config["owner"]})
        print("Removed Windows coordinator registration; active delivery drains before coordinator exit.")
        return 0
    except (OSError, ValueError, windows.SchedulerError) as error:
        print(f"ltc: could not remove Windows coordinator: {error}", file=sys.stderr)
        return 2
