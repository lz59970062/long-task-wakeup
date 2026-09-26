"""Linux workers owned by separate transient systemd user services.

Each service has its own control group, independent of the callback daemon's
service. ``Type=exec`` waits for worker exec, not task completion; ``Restart=no``
prevents systemd from replaying work after a failure. Durable task/result files
remain the source of truth after a unit is collected or the machine reboots.

Only the worker invocation is sent to systemd. The worker restores its saved
environment and reads its command from the private task record. This avoids
putting prompts or credentials into D-Bus properties and process listings.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import re
import shutil
import signal
import socket
import subprocess
import sys

from .base import LaunchError, OwnerState


CONTROL_TIMEOUT_SECONDS = 10.0
_TASK_ID = re.compile(r"[A-Za-z0-9_-]+\Z")
_OWNER = re.compile(r"ltc-[A-Za-z0-9_-]+\.service\Z")
_LIVE_STATES = frozenset({"active", "activating", "reloading", "deactivating", "maintenance", "refreshing"})


def current_boot_id() -> str | None:
    """Return Linux's boot identity; None means unknown, never 'same boot'."""
    if not sys.platform.startswith("linux"):
        return None
    try:
        value = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return value or None


def current_machine_id() -> str:
    """Read the Linux host identity, preserving the legacy hostname fallback."""
    if not sys.platform.startswith("linux"):
        raise RuntimeError("machine identity requires a Linux platform adapter")
    try:
        value = Path("/etc/machine-id").read_text(encoding="utf-8").strip()
    except OSError:
        value = socket.gethostname()
    return value or socket.gethostname()


def process_identity(pid: int) -> dict[str, object] | None:
    """Identify a live process across PID reuse, boots and PID namespaces.

    Containers can share the host boot and an image's machine-id. The PID
    namespace and /proc start ticks are therefore both required. Missing
    identity evidence disables reload; it must never authorize a signal.
    """
    if not sys.platform.startswith("linux") or type(pid) is not int or pid <= 0:
        return None
    boot_id = current_boot_id()
    if not boot_id:
        return None
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        # The parenthesized comm field may itself contain spaces or ')'.
        fields = stat.rsplit(")", 1)[1].split()
        if fields[0] in {"Z", "X", "x"}:
            return None
        start_ticks = int(fields[19])  # starttime is field 22; state is field 3.
        pid_namespace = os.readlink(f"/proc/{pid}/ns/pid")
        machine_id = current_machine_id()
    except (OSError, ValueError, IndexError):
        return None
    if not pid_namespace.startswith("pid:[") or start_ticks < 0:
        return None
    return {
        "boot_id": boot_id,
        "machine_id": machine_id,
        "pid_namespace": pid_namespace,
        "start_ticks": start_ticks,
    }


def signal_if_identity_matches(pid: int, identity: dict[str, object], signum: int) -> bool:
    """Signal the verified process through a pidfd, never a reusable PID.

    On kernels/Python builds without pidfd support, leave the coordinator
    running and report False rather than fall back to a check/kill race.
    """
    if not sys.platform.startswith("linux") or type(pid) is not int or pid <= 0:
        return False
    if not hasattr(os, "pidfd_open") or not hasattr(signal, "pidfd_send_signal"):
        return False
    try:
        descriptor = os.pidfd_open(pid)
    except OSError:
        return False
    try:
        if not identity or process_identity(pid) != identity:
            return False
        signal.pidfd_send_signal(descriptor, signum)
        return True
    except OSError:
        return False
    finally:
        os.close(descriptor)


def _validate_owner(owner: str) -> None:
    if not _OWNER.fullmatch(owner) or len(owner) > 255:
        raise ValueError("invalid LTC systemd unit name")


def _absolute_path(path: Path) -> str:
    value = str(Path(path).expanduser().resolve())
    if any(character in value for character in ("\0", "\r", "\n")):
        raise ValueError("systemd paths must not contain NUL or line breaks")
    return value


class SystemdUserBackend:
    """A user-manager backend for native Linux (systemd 240 or newer).

    User services normally share the user's login lifetime. Surviving logout
    requires a separately configured persistent user manager; this backend
    does not enable linger or request machine-wide privileges.
    """

    name = "systemd-user"

    def available(self) -> bool:
        if not sys.platform.startswith("linux"):
            return False
        systemctl = shutil.which("systemctl")
        if systemctl is None or shutil.which("systemd-run") is None:
            return False
        try:
            result = subprocess.run(
                [systemctl, "--user", "--no-pager", "show", "--property=Version", "--value"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
                timeout=CONTROL_TIMEOUT_SECONDS,
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
        # Type=exec and append-mode log output are both available from v240.
        version = re.match(r"\s*(\d+)", result.stdout or "")
        return result.returncode == 0 and version is not None and int(version.group(1)) >= 240

    def owner_name(self, task_id: str, attempt: int, queue_root: Path) -> str:
        if not isinstance(task_id, str) or not _TASK_ID.fullmatch(task_id):
            raise ValueError("task id must contain only letters, numbers, '-' or '_'")
        if isinstance(attempt, bool) or not isinstance(attempt, int) or attempt < 1:
            raise ValueError("launch attempt must be a positive integer")
        queue_key = hashlib.sha256(os.fsencode(_absolute_path(queue_root))).hexdigest()[:16]
        task_key = task_id
        if len(task_key) > 80:
            digest = hashlib.sha256(task_id.encode("ascii")).hexdigest()[:16]
            task_key = f"{task_id[:40]}-{digest}"
        owner = f"ltc-{queue_key}-{task_key}-a{attempt}.service"
        _validate_owner(owner)
        return owner

    def launch(self, owner: str, argv: list[str], cwd: Path, log_path: Path) -> None:
        try:
            _validate_owner(owner)
            if not argv or any(not isinstance(value, str) or "\0" in value for value in argv):
                raise ValueError("worker argv must contain non-NUL strings")
            if not Path(argv[0]).is_absolute():
                raise ValueError("worker executable must use an absolute path")
            working_directory = _absolute_path(cwd)
            output_path = _absolute_path(log_path)
        except (OSError, ValueError) as error:
            raise LaunchError(str(error), uncertain=False) from error
        if not sys.platform.startswith("linux"):
            raise LaunchError("systemd user workers require Linux", uncertain=False)
        executable = shutil.which("systemd-run")
        if executable is None:
            raise LaunchError("systemd-run was not found", uncertain=False)

        # systemd-run v245 passes argv through D-Bus, so spaces and '%' remain
        # literal. The manager still expands '$' in arguments by default.
        # Doubling '$' preserves literal paths without the newer (v254)
        # --expand-environment=no flag. No shell is involved.
        worker_argv = [argv[0], *(argument.replace("$", "$$") for argument in argv[1:])]
        command = [
            executable,
            "--user",
            "--no-ask-password",
            "--quiet",
            "--collect",
            f"--unit={owner}",
            "--property=Type=exec",
            "--property=Restart=no",
            "--property=KillMode=control-group",
            "--property=UMask=0077",
            "--property=StandardInput=null",
            f"--property=StandardOutput=append:{output_path}",
            f"--property=StandardError=append:{output_path}",
            f"--working-directory={working_directory}",
            "--",
            *worker_argv,
        ]
        try:
            result = subprocess.run(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
                timeout=CONTROL_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired as error:
            raise LaunchError("systemd worker submission timed out; its outcome is unknown", uncertain=True) from error
        except OSError as error:
            # Popen did not exec the submission utility, so it could not send
            # this launch request. Never include the submitted argv in errors.
            raise LaunchError(f"could not start systemd-run: {error.strerror or type(error).__name__}", uncertain=False) from error
        if result.returncode != 0:
            # The manager might have accepted a start before the client lost
            # its reply, or an existing owner might already be running. A
            # missing/collected unit alone cannot prove the command never ran.
            raise LaunchError(
                f"systemd-run exited with status {result.returncode}; reconcile worker state before retrying",
                uncertain=True,
            )

    def probe(self, owner: str) -> OwnerState:
        try:
            _validate_owner(owner)
        except ValueError:
            return OwnerState.UNKNOWN
        if not sys.platform.startswith("linux"):
            return OwnerState.UNKNOWN
        executable = shutil.which("systemctl")
        if executable is None:
            return OwnerState.UNKNOWN
        try:
            result = subprocess.run(
                [
                    executable,
                    "--user",
                    "--no-pager",
                    "show",
                    "--property=LoadState",
                    "--property=ActiveState",
                    "--property=Job",
                    owner,
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
                timeout=CONTROL_TIMEOUT_SECONDS,
            )
        except (OSError, subprocess.TimeoutExpired):
            return OwnerState.UNKNOWN
        if result.returncode != 0:
            return OwnerState.UNKNOWN
        properties = dict(line.split("=", 1) for line in (result.stdout or "").splitlines() if "=" in line)
        active_state = properties.get("ActiveState")
        load_state = properties.get("LoadState")
        if not active_state or not load_state:
            return OwnerState.UNKNOWN
        # systemd 245 renders an idle Job object as an empty property, not 0.
        pending_job = (properties.get("Job") or "0").split(" ", 1)[0]
        if not pending_job.isdigit():
            return OwnerState.UNKNOWN
        if int(pending_job) != 0:
            return OwnerState.ALIVE
        if load_state == "loaded" and active_state in _LIVE_STATES:
            return OwnerState.ALIVE
        if load_state in {"loaded", "not-found"} and active_state in {"inactive", "failed"}:
            return OwnerState.ABSENT
        return OwnerState.UNKNOWN
