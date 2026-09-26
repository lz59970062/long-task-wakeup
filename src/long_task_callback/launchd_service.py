"""Installation of the macOS per-user callback coordinator.

Business commands are owned by separate jobs in platforms/macos.py. Updating a
coordinator never boots out a live process or interrupts its callback delivery.
"""
from __future__ import annotations

import argparse
import math
import os
from pathlib import Path
import subprocess
import sys

from .platforms import OwnerState
from .platforms import macos


def agent_directory() -> Path:
    return Path.home() / "Library" / "LaunchAgents"


def service_text(args: argparse.Namespace) -> str:
    from . import cli

    label = macos.validate_label(args.name)
    command = cli.daemon_command(args)
    command[0] = str(Path(command[0]).expanduser().absolute())
    # Pin even the default queue: launchd does not inherit the submitting shell.
    if "--queue-dir" in command:
        command[command.index("--queue-dir") + 1] = str(cli.queue_dir(args).resolve())
    else:
        command.extend(["--queue-dir", str(cli.queue_dir(args).resolve())])
    environment = cli.daemon_environment(args, include_proxy_values=False)
    environment.update(CODEX_HOME=str(cli.codex_home().resolve()),
                       CLAUDE_CONFIG_DIR=str(cli.claude_home().resolve()))
    for name in (cli.TARGET_LOCK_DIR_ENV, cli.APP_SERVER_SOCKET_ENV, cli.ALLOW_APP_SERVER_SOCKET_OVERRIDE_ENV):
        if name in os.environ:
            environment[name] = os.environ[name]
    log = str(cli.daemon_state_dir().resolve() / "daemon.log")
    return macos.plist_text({
        "Label": label, "ProgramArguments": command, "WorkingDirectory": str(Path.home()),
        "EnvironmentVariables": environment, "RunAtLoad": True, "KeepAlive": True,
        "ThrottleInterval": max(1, math.ceil(args.restart_sec)), "Umask": 0o077,
        "StandardInPath": "/dev/null", "StandardOutPath": log, "StandardErrorPath": log,
    })


def install(args: argparse.Namespace) -> int:
    from . import cli

    try:
        text = service_text(args)
        if args.print:
            print(text, end="")
            return 0
        if sys.platform != "darwin":
            raise ValueError("launchd coordinator requires macOS")
        target = agent_directory() / f"{args.name}.plist"
        if target.exists() and not args.force:
            print(f"LaunchAgent already exists at {target}. Re-run with --force to overwrite.", file=sys.stderr)
            return 1
        cli.configure_proxy_environment(args)
        state_dir = cli.daemon_state_dir()
        state_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(state_dir, 0o700)
        cli.write_private_text(target, text)
        print(f"Installed launchd LaunchAgent to {target}")
        if not (args.enable or args.now):
            return 0
        service = f"{macos.gui_domain()}/{args.name}"
        result = macos.launchctl(["enable", service])
        if result.returncode != 0:
            print("ltc: could not enable the LaunchAgent; log in to the macOS GUI session and retry", file=sys.stderr)
            return 1
        if not args.now:
            return 0

        state, pid, registered = macos.job_status(args.name)
        if state == OwnerState.ALIVE:
            if pid is not None and cli.send_standalone_reload(pid, expected_queue=cli.queue_dir(args)):
                print("Requested coordinator reload after callback deliveries drain. LaunchAgent settings apply at next load.")
                return 0
            print("ltc: running LaunchAgent could not be verified for safe reload; it was left untouched", file=sys.stderr)
            return 1
        if state == OwnerState.UNKNOWN:
            print("ltc: LaunchAgent state is unknown; refusing to replace or start another coordinator", file=sys.stderr)
            return 1

        queue = cli.queue_dir(args)
        admission = cli.acquire_owner_lock(queue, "daemon-singleton", blocking=False)
        if admission is None:
            runtime = cli.read_daemon_runtime()
            pid = runtime.get("pid") if runtime else None
            if type(pid) is int and cli.send_standalone_reload(pid, expected_queue=queue):
                print("Reloading the existing coordinator. LaunchAgent activation is deferred until that coordinator exits.")
                return 0
            print("ltc: another coordinator owns the queue; LaunchAgent activation was deferred", file=sys.stderr)
            return 1
        cli.release_owner_lock(admission, remove=False)
        if registered:
            result = macos.launchctl(["bootout", service])
            if result.returncode != 0:
                print("ltc: could not unload the inactive LaunchAgent; activation deferred", file=sys.stderr)
                return 1
        result = macos.launchctl(["bootstrap", macos.gui_domain(), str(target)])
        if result.returncode != 0:
            print("ltc: LaunchAgent bootstrap failed; inspect launchctl status and daemon.log before retrying", file=sys.stderr)
            return 1
        print(f"Started {service}; log: {state_dir / 'daemon.log'}")
        return 0
    except (OSError, ValueError, subprocess.TimeoutExpired) as error:
        print(f"ltc: could not configure launchd coordinator ({type(error).__name__}); inspect its saved configuration", file=sys.stderr)
        return 2
