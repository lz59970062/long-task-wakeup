"""Environment, configuration and lifecycle evidence for Agent-owned recovery.

These checks never install software, start a service, contact a model, or submit
work. Automatic reports are advisory: task admission and its exit status remain
owned by the caller. A writable queue and a live coordinator do not establish
successful authentication, the daemon's environment, or remote session delivery.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import time

from . import __version__
from .agents import get_agent
from .platforms import posix
from .platforms import OwnerState
from .platforms.screen import ScreenBackend
from .runtime import worker_command

SCHEMA = "ltc.health.v1"
PRODUCERS = {"run", "agent", "done"}
UNVERIFIED = ["authentication", "session_delivery", "daemon_environment"]
HANDOFF_GRACE_SECONDS = 10.0
INSTRUCTION = (
    "The invoking AI owns LTC recovery: inspect the environment, configuration and runtime issues, "
    "perform the applicable repair, then run recheck_command. A null repair_command means inspect "
    "lifecycle evidence or platform support first, not reinstall. Do not resubmit persisted tasks "
    "or queued callbacks merely because this check failed; inspect their state/results before choosing further work. "
    "For unknown admission, inspect existing records before retrying. For done, repair callback "
    "delivery only; never rerun the completed external work. Do not repeat a failed "
    "repair loop. Ask the user only for missing credentials or authorization, not routine configuration."
)


def queue_writable(root: Path) -> bool:
    """Probe the queue or nearest existing ancestor without creating directories.

    The private probe is removed immediately. Existing operational directories
    are checked too: write access to the queue root alone does not imply access
    to its pending requests, acknowledgements, locks or callback details.
    """
    directory = root
    try:
        while not directory.exists():
            if directory.is_symlink() or directory.parent == directory:
                return False
            directory = directory.parent
        targets = [directory]
        if root.is_dir():
            targets.extend(root / name for name in ("pending", "acks", "locks", "details")
                           if (root / name).exists() or (root / name).is_symlink())
        for target in targets:
            if not target.is_dir():
                return False
            with os.scandir(target) as entries:
                next(entries, None)
            descriptor, temporary = tempfile.mkstemp(prefix=".ltc-health-", dir=target)
            try:
                os.write(descriptor, b"ltc readiness probe\n")
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
                Path(temporary).unlink()
            posix.fsync_directory(target)
        return True
    except OSError:
        return False


def coordinator_issue(root: Path) -> dict[str, str] | None:
    """Observe the existing singleton inode without creating a new lock file."""
    from . import cli

    path = cli.owner_lock_path(root, "daemon-singleton")
    try:
        if cli.fcntl is None:
            raise OSError("platform lock support unavailable")
        with path.open("r", encoding="utf-8") as handle:
            try:
                cli.fcntl.flock(handle.fileno(), cli.fcntl.LOCK_EX | cli.fcntl.LOCK_NB)
            except BlockingIOError:
                pass  # A coordinator may own the queue; verify its identity.
            else:
                cli.fcntl.flock(handle.fileno(), cli.fcntl.LOCK_UN)
                return {"code": "daemon_not_running", "action": "Start one coordinator for this exact queue, then recheck."}
    except FileNotFoundError:
        return {"code": "daemon_not_running", "action": "Start one coordinator for this exact queue, then recheck."}
    except OSError:
        return {"code": "daemon_unverified", "action": "Inspect queue lock access and the existing coordinator; do not start a duplicate."}

    runtime = cli.read_daemon_runtime()
    if (runtime is None or not cli.daemon_supports_hot_reload(runtime.get("pid"), expected_queue=root)
            or runtime.get("queue_dir") != str(root.resolve())):
        return {"code": "daemon_unverified", "action": "Inspect the lock-owning coordinator and its queue/runtime identity; leave unrelated processes untouched."}
    if runtime.get("version") != __version__:
        return {"code": "daemon_version_mismatch", "action": "Safely update the verified coordinator after active deliveries drain, then recheck."}
    return None


def environment_profile() -> dict[str, object]:
    """Describe this runtime; supported backends determine useful checks."""
    from . import cli

    operating_system = {"linux": "linux", "darwin": "macos", "win32": "windows"}.get(sys.platform, "other")
    return {"os": operating_system, "container": cli.running_in_container() if sys.platform == "linux" else False,
            "supported": sys.platform in ("linux", "darwin")}


def runtime_issues(root: Path) -> list[dict[str, object]]:
    """Observe persisted work through its recorded backend, never relaunch it.

    Empty queues and not-yet-launched submissions need no execution owner.
    Results outrank process observations; re-read after an absent/unknown owner
    to tolerate a worker finishing while its manager is queried. Group faults so
    a manager outage does not flood the calling Agent with one block per task.
    """
    from . import cli

    grouped: dict[tuple[str, str], dict[str, object]] = {}
    now = time.time()
    machine = cli.current_machine_id()
    boot = cli.current_boot_id()

    def add(code: str, task_id: str, backend: str, action: str) -> None:
        item = grouped.setdefault((code, backend), {
            "code": code, "kind": "runtime", "component": "task_lifecycle", "backend": backend,
            "count": 0, "task_ids": [], "action": action,
        })
        item["count"] += 1
        if len(item["task_ids"]) < 5:
            item["task_ids"].append(task_id)

    def result_for(task: dict[str, object]) -> dict[str, object] | None:
        path = cli.managed_result_path(root, str(task["id"]))
        try:
            result = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        if not isinstance(result, dict) or result.get("id") != task["id"] or type(result.get("exit_code")) is not int:
            raise ValueError("invalid result identity")
        return result

    def handoff(task: dict[str, object], result: dict[str, object] | None, record_path: Path) -> None:
        task_id = str(task["id"])
        if cli.managed_callback_exists(root, task_id):
            return
        finished = (result or {}).get("completed_at")
        if not isinstance(finished, (int, float)):
            finished = next((task.get(key) for key in ("completed_at", "interrupted_at", "launch_failed_at")
                             if isinstance(task.get(key), (int, float))), record_path.stat().st_mtime)
        if now - float(finished) < HANDOFF_GRACE_SECONDS:
            return
        # The worker/coordinator may publish the callback during inspection.
        if not cli.managed_callback_exists(root, task_id):
            add("result_handoff_missing", task_id, str(task.get("execution_backend", "screen")),
                "Inspect the saved result and restore callback handoff for this ID through the coordinator; do not rerun the business command.")

    try:
        tasks_root = cli.managed_tasks_root(root)
        if not tasks_root.exists():
            return []
        paths = sorted(directory / "task.json" for directory in tasks_root.iterdir()
                       if directory.is_dir() and (directory / "task.json").exists())
    except OSError:
        return [{"code": "task_records_unavailable", "kind": "runtime", "component": "task_lifecycle",
                 "action": "Restore read access to the task records before assessing their owners."}]
    for path in paths:
        task_id, backend = path.parent.name, "unknown"
        try:
            task = cli.load_managed_task(path)
            backend = str(task.get("execution_backend", "screen"))
            state = task.get("state")
            if state not in {"submitted", "launching", "running", "completed", "interrupted", "launch_failed", "foreign_host"}:
                continue
            if Path(str(task["queue_dir"])).expanduser().resolve() != root.resolve():
                add("task_queue_mismatch", task_id, backend, "Inspect the recorded queue binding; do not move or relaunch the task automatically.")
                continue
            if state == "submitted":
                continue
            if state == "foreign_host":
                add("task_foreign_host", task_id, backend, "Restore the original host/container context for this unresolved task; do not restart its command on this host.")
                continue
            # Completed historical work needs no live screen session or unit.
            if state in {"completed", "interrupted", "launch_failed"} and cli.managed_callback_exists(root, task_id):
                continue
            try:
                result = result_for(task)
            except (OSError, ValueError):
                add("task_result_invalid", task_id, backend, "Inspect the result record and artifacts; a missing or malformed observation does not authorize a new execution.")
                continue
            if result is not None or state in {"completed", "interrupted", "launch_failed"}:
                handoff(task, result, path)
                continue
            if task.get("machine_id") != machine:
                add("task_foreign_host", task_id, backend, "Inspect the task in its original host/container; do not probe or restart an owner using another host's identity.")
                continue
            recorded_boot = (task.get("boot_id") if state == "running"
                             else task.get("launch_boot_id") or task.get("submission_boot_id"))
            if boot and recorded_boot and recorded_boot != boot:
                add("task_boot_changed", task_id, backend, "The recorded execution predates this boot; inspect checkpoints and restore recovery reporting, not automatic command replay.")
                continue
            launched = task.get("launch_requested_at", task.get("screen_submitted_at"))
            if state == "launching" and isinstance(launched, (int, float)) and now - launched < cli.MANAGED_WORKER_HANDSHAKE_SECONDS:
                continue
            try:
                owner = cli.managed_owner_state(task)
            except (OSError, RuntimeError, ValueError):
                owner = OwnerState.UNKNOWN
            if owner == OwnerState.ALIVE:
                continue
            latest = cli.load_managed_task(path)
            try:
                result = result_for(latest)
            except (OSError, ValueError):
                add("task_result_invalid", task_id, backend, "Inspect the changed result record before deciding task status or retrying.")
                continue
            if result is not None or latest.get("state") in {"completed", "interrupted", "launch_failed"}:
                handoff(latest, result, path)
                continue
            identity_keys = ("state", "launch_attempt_count", "execution_owner", "screen_session")
            if any(latest.get(key) != task.get(key) for key in identity_keys):
                continue
            if owner == OwnerState.ABSENT:
                add("task_owner_missing", task_id, backend,
                    "The recorded task owner is absent without a saved result; inspect logs/checkpoints and let the coordinator reconcile it. Do not blindly restart work.")
            else:
                add("task_owner_unverified", task_id, backend,
                    "The task owner cannot be verified; inspect its backend connection/permissions. Unknown does not mean stopped and must not trigger a duplicate execution.")
        except FileNotFoundError:
            continue  # Concurrent cleanup is not evidence that a task died.
        except (OSError, ValueError, KeyError, TypeError):
            add("task_record_invalid", task_id, backend, "Inspect the task record and its read permissions before repairing its lifecycle; keep existing artifacts.")
    return list(grouped.values())


def _operation(args: argparse.Namespace) -> str:
    return str(getattr(args, "operation", None) or getattr(args, "mode", "doctor"))


def _work(args: argparse.Namespace) -> dict[str, object]:
    default = "not_submitted" if getattr(args, "mode", None) in PRODUCERS else "not_applicable"
    value = getattr(args, "_health_work", None)
    if not isinstance(value, dict):
        return {"state": default}
    work = {"state": value.get("state", default)}
    if isinstance(value.get("id"), str):
        work["id"] = value["id"]
    return work


def inspect(args: argparse.Namespace) -> dict[str, object]:
    """Check supported environment, configuration and observed lifecycle state."""
    from . import cli

    operation = _operation(args)
    root = cli.queue_dir(args).expanduser().absolute()
    issues: list[dict[str, object]] = []
    work = _work(args)
    environment = environment_profile()
    if not environment["supported"]:
        recheck = worker_command("doctor", "--queue-dir", str(root), "--operation", operation)
        for option in ("backend", "agent", "session", "agent_worker"):
            value = getattr(args, option, None)
            if value:
                recheck.extend(["--" + option.replace("_", "-"), str(value)])
        if getattr(args, "last", False):
            recheck.append("--last")
        return {
            "schema": SCHEMA, "status": "needs_configuration", "scope": "local_prerequisites",
            "environment": environment, "checks": {"support": "unsupported", "configuration": "not_checked", "runtime": "not_checked"},
            "unverified": list(UNVERIFIED), "operation": operation, "queue_dir": str(root), "work": work,
            "issues": [{"code": "platform_unsupported", "kind": "support", "component": "platform",
                        "action": "This preview has no native adapter for this OS. Use a supported Linux or macOS deployment; installing another OS's services here will not fix it."}],
            "repair_command": None, "recheck_command": recheck, "instruction": INSTRUCTION,
        }
    if work["state"] == "unknown":
        issues.append({"code": "submission_unverified", "kind": "runtime", "component": "submission",
                       "action": "Inspect the existing record for this work ID before retrying; admission may already have persisted."})
    choice = getattr(args, "backend", None) or "auto"
    if operation in {"run", "agent"}:
        try:
            if getattr(args, "_health_backend_error", None):
                raise ValueError("backend selection already failed")
            selected = getattr(args, "_health_backend", None)
            if selected is None:
                selected = cli.select_execution_backend(args)
            choice = "systemd" if selected == "systemd-user" else str(selected)
            if selected == "screen" and not ScreenBackend().available():
                raise ValueError("selected screen executable is not usable")
        except (OSError, ValueError, RuntimeError):
            issues.append({"code": "backend_unavailable", "action": (
                "Provide the requested task backend: launchd in a logged-in macOS GUI session, "
                "systemd on Linux, or explicitly select screen when the native manager is unavailable.")})
    if not queue_writable(root):
        issues.append({"code": "queue_unwritable", "action": "Repair access/storage for this queue and its existing directories without deleting task records."})
    if operation in {"run", "agent"} and not queue_writable(cli.managed_tasks_root(root)):
        issues.append({"code": "tasks_unwritable", "action": "Repair access/storage for this queue's managed task directory; the default tasks directory is beside the queue."})
    issue = coordinator_issue(root)
    if issue is not None:
        issues.append(dict(issue, kind="runtime", component="coordinator"))
    issues.extend(runtime_issues(root))
    issues.extend(delivery_issues(root))

    callback_agent = cli.resolve_agent(args)
    adapter = get_agent(callback_agent)
    executables: dict[str, str] = {}
    callback_executable = shutil.which(adapter.executable(os.environ))
    if callback_executable is None:
        issues.append({"code": "callback_agent_unavailable", "action": "Make the selected callback Agent CLI available in the coordinator's environment."})
    else:
        executables[callback_agent] = callback_executable
    child_agent = getattr(args, "agent_worker", None)
    if operation == "agent" and not child_agent:
        issues.append({"code": "child_agent_unspecified", "action": "Pass --agent-worker with the actual intended child Agent to check delegated-task readiness."})
    if operation == "agent" and child_agent:
        child_executable = shutil.which(get_agent(child_agent).executable(os.environ))
        if child_executable is None:
            issues.append({"code": "child_agent_unavailable", "action": "Install or configure the selected child Agent CLI before executing this delegated task."})
        else:
            executables[child_agent] = child_executable

    # resolve_target is intentionally quiet; bind_target prints normal CLI output.
    target_args = argparse.Namespace(**vars(args))
    target_args.session = getattr(args, "session", None)
    target_args.last = bool(getattr(args, "last", False))
    target = None
    try:
        target, _ = cli.resolve_target(target_args)
    except SystemExit:
        issues.append({"code": "session_unbound", "action": "Recover the actual originating Agent session ID and pass --session; never invent a target or substitute --last."})

    skill_home = cli.codex_home() if callback_agent == "codex" else cli.claude_home() if callback_agent == "claude" else None
    if skill_home is not None and not (skill_home / "skills" / "long-task-callback" / "SKILL.md").is_file():
        issues.append({"code": "skill_missing", "action": "Install the bundled LTC skill for the selected Agent so it can inspect, acknowledge and continue callbacks."})

    repair = worker_command("setup", "--queue-dir", str(root), "--backend", choice,
                            "--service", "auto", "--skill-target", callback_agent,
                            "--keep-skill", "--force", "--now")
    if operation == "done":
        repair.append("--callback-only")
    for agent, executable in executables.items():
        if agent in {"codex", "claude"}:
            repair.extend([f"--{agent}-bin", executable])
    recheck = worker_command("doctor", "--queue-dir", str(root), "--backend", choice,
                             "--agent", callback_agent, "--operation", operation)
    if target is not None:
        if target.get("kind") == "session":
            recheck.extend(["--session", target["value"]])
        elif target.get("kind") == "last":
            recheck.append("--last")
    if operation == "agent" and child_agent:
        recheck.extend(["--agent-worker", child_agent])
    for issue in issues:
        issue.setdefault("kind", "configuration")
    checks = {"support": "supported",
              "configuration": "needs_attention" if any(issue["kind"] == "configuration" for issue in issues) else "ready",
              "runtime": "needs_attention" if any(issue["kind"] == "runtime" for issue in issues) else "ready"}
    # Setup repairs bootstrap/configuration or a missing coordinator. A lost
    # task owner or failed delivery instead needs inspection and reconciliation,
    # not a generic service reinstall that cannot restore the business process.
    bootstrap_codes = {"daemon_not_running", "daemon_version_mismatch", "skill_missing"}
    if issues and not any(issue["kind"] == "configuration" or issue["code"] in bootstrap_codes for issue in issues):
        repair = None
    return {
        "schema": SCHEMA,
        "status": "needs_configuration" if issues else "ready",
        "scope": "local_prerequisites",
        "environment": environment,
        "checks": checks,
        "unverified": list(UNVERIFIED),
        "operation": operation,
        "queue_dir": str(root),
        "issues": issues,
        "work": work,
        "repair_command": repair,
        "recheck_command": recheck,
        "instruction": INSTRUCTION,
    }


def _failed(args: argparse.Namespace, error: BaseException) -> dict[str, object]:
    # Exception messages may contain user commands, prompts, or environment data.
    return {
        "schema": SCHEMA, "status": "needs_configuration", "scope": "local_prerequisites",
        "unverified": list(UNVERIFIED), "operation": _operation(args), "work": _work(args),
        "issues": [{"code": "diagnostics_failed", "error_type": type(error).__name__,
                    "action": "Inspect local readiness with ltc doctor; preserve existing work and avoid repeated retries."}],
        "instruction": INSTRUCTION,
    }


def doctor(args: argparse.Namespace) -> int:
    try:
        report = inspect(args)
    except (Exception, SystemExit) as error:
        report = _failed(args, error)
    print(json.dumps(report, ensure_ascii=False, separators=(",", ":")))
    return 0 if report["status"] == "ready" else 1


def emit_if_needed(args: argparse.Namespace) -> None:
    """Append one bounded advisory without altering the producer's exit status."""
    try:
        try:
            report = inspect(args)
        except (Exception, SystemExit) as error:
            report = _failed(args, error)
        if report["status"] != "ready":
            print("[ltc-status]\n" + json.dumps(report, ensure_ascii=False, separators=(",", ":"))
                  + "\n[/ltc-status]", file=sys.stderr)
    except Exception:
        # A closed stderr or an unexpected report-serialization failure cannot
        # turn a successful durable submission into a retryable command failure.
        return


def delivery_issues(root: Path) -> list[dict[str, object]]:
    """Summarize unresolved failed handoffs without retrying or releasing leases.

    ACK/cancellation wins even if it arrives while a failed record is being read.
    Output is grouped by issue and Agent; only five example IDs are included in
    each group. Commands, prompts and recorded exception text are never emitted.
    """
    from . import cli

    actions = {
        "callback_delivery_failed": (
            "Inspect delivery evidence and the bound original session; repair the handoff "
            "without rerunning business work, deleting leases, or blindly resuming."
        ),
        "callback_outcome_unknown": (
            "Inspect the bound original session and ACK state before recovery; do not blindly "
            "resume, rerun business work, or delete the retained lease."
        ),
        "callback_record_unverified": (
            "Inspect unreadable callback records and their bound original sessions; preserve "
            "records/leases and do not blindly resume or rerun business work."
        ),
        "callback_queue_unverified": (
            "Restore read access to failed callback records, then inspect their bound original "
            "sessions; do not delete leases, blindly resume, or rerun business work."
        ),
    }
    groups: dict[tuple[str, str], dict[str, object]] = {}

    def add(code: str, agent: str = "unknown", callback_id: str | None = None) -> None:
        group = groups.setdefault((code, agent), {
            "code": code, "agent": agent, "kind": "runtime", "component": "callback_delivery",
            "count": 0, "callback_ids": [], "action": actions[code],
        })
        group["count"] += 1
        if callback_id is not None and len(group["callback_ids"]) < 5:
            group["callback_ids"].append(callback_id)

    def settled(callback_id: str) -> bool:
        return cli.ack_path(root, callback_id).exists() or cli.is_canceled(root, callback_id)

    try:
        with os.scandir(root / "failed") as entries:
            for entry in entries:
                if not entry.name.endswith(".json"):
                    continue
                path = Path(entry.path)
                callback_id = path.stem
                request = None
                try:
                    if settled(callback_id):
                        continue
                    request = cli.load_request(path)
                except FileNotFoundError:
                    continue  # The coordinator may have moved the record.
                except Exception:
                    pass  # Preserve the record and report only a stable code.
                try:
                    if settled(callback_id) or not path.exists():
                        continue
                except OSError:
                    request = None
                if request is None:
                    add("callback_record_unverified", callback_id=callback_id)
                    continue
                agent = cli.request_agent(request)
                code = ("callback_outcome_unknown" if request.get("retain_target_lease") is True
                        else "callback_delivery_failed")
                add(code, agent, callback_id)
    except FileNotFoundError:
        pass  # A queue with no failed directory has no failed handoffs to inspect.
    except OSError:
        add("callback_queue_unverified")
    return list(groups.values())
