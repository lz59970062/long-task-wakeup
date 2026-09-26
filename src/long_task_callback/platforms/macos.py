"""macOS identities and independent, one-shot launchd task owners.

Task plists live beside private task records, never in Library/LaunchAgents:
login or reboot must not load and replay business commands. Only the coordinator
is a persistent LaunchAgent. Manager errors are unknown, not proof of absence.
"""
from __future__ import annotations

import ctypes
import hashlib
import os
from pathlib import Path
import plistlib
import re
import subprocess
import sys
import uuid

from .base import LaunchError, OwnerState

CONTROL_TIMEOUT_SECONDS = 10.0
LAUNCHCTL = "/bin/launchctl"
OWNER_ENV = "LTC_LAUNCHD_OWNER"
_LABEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,199}\Z")


def _libsystem():
    if sys.platform != "darwin":
        raise OSError("macOS system interfaces are unavailable")
    return ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)


def current_boot_id() -> str | None:
    try:
        library = _libsystem()
        query = library.sysctlbyname
        query.argtypes = [ctypes.c_char_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_size_t),
                          ctypes.c_void_p, ctypes.c_size_t]
        query.restype = ctypes.c_int
        value = ctypes.create_string_buffer(128)
        size = ctypes.c_size_t(len(value))
        if query(b"kern.bootsessionuuid", value, ctypes.byref(size), None, 0) != 0:
            return None
        return str(uuid.UUID(value.value.decode("ascii")))
    except (OSError, AttributeError, ValueError, UnicodeError):
        return None


class _Timespec(ctypes.Structure):
    _fields_ = [("seconds", ctypes.c_long), ("nanoseconds", ctypes.c_long)]


def current_machine_id() -> str:
    """Hardware host UUID; a mutable hostname cannot authorize recovery."""
    try:
        query = _libsystem().gethostuuid
        query.argtypes = [ctypes.c_void_p, ctypes.POINTER(_Timespec)]
        query.restype = ctypes.c_int
        value = ctypes.create_string_buffer(16)
        if query(value, ctypes.byref(_Timespec(1, 0))) == 0 and any(value.raw):
            return str(uuid.UUID(bytes=value.raw))
    except (OSError, AttributeError):
        pass
    raise RuntimeError("could not obtain macOS host identity")


class _ProcBsdInfo(ctypes.Structure):
    # Public Darwin proc_bsdinfo layout (sys/proc_info.h), on arm64 and x86_64.
    _fields_ = [(name, ctypes.c_uint32) for name in (
        "flags", "status", "xstatus", "pid", "ppid", "uid", "gid", "ruid",
        "rgid", "svuid", "svgid", "reserved",
    )] + [("comm", ctypes.c_char * 16), ("name", ctypes.c_char * 32)] + [
        (name, ctypes.c_uint32) for name in ("nfiles", "pgid", "jobc", "tdev", "tpgid")
    ] + [("nice", ctypes.c_int32), ("start_seconds", ctypes.c_uint64),
         ("start_microseconds", ctypes.c_uint64)]


def process_identity(pid: int) -> dict[str, object] | None:
    """Use microsecond process start time, boot, host and UID to reject PID reuse."""
    if sys.platform != "darwin" or type(pid) is not int or pid <= 0:
        return None
    try:
        library = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
        query = library.proc_pidinfo
        query.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_uint64, ctypes.c_void_p, ctypes.c_int]
        query.restype = ctypes.c_int
        info = _ProcBsdInfo()
        if query(pid, 3, 0, ctypes.byref(info), ctypes.sizeof(info)) != ctypes.sizeof(info):
            return None
        if info.pid != pid or info.status in (0, 5) or not info.start_seconds:
            return None  # SZOMB = 5; a zombie cannot serve the queue.
        boot = current_boot_id()
        if boot is None:
            return None
        return {"boot_id": boot, "machine_id": current_machine_id(), "uid": info.uid,
                "start_seconds": info.start_seconds, "start_microseconds": info.start_microseconds}
    except (OSError, AttributeError, RuntimeError):
        return None


def peer_uid(descriptor: int) -> int:
    """Authenticate a local UNIX socket peer using Darwin's getpeereid."""
    query = _libsystem().getpeereid
    query.argtypes = [ctypes.c_int, ctypes.POINTER(ctypes.c_uint), ctypes.POINTER(ctypes.c_uint)]
    query.restype = ctypes.c_int
    uid, gid = ctypes.c_uint(), ctypes.c_uint()
    if query(descriptor, ctypes.byref(uid), ctypes.byref(gid)) != 0:
        raise OSError(ctypes.get_errno(), "could not identify local socket peer")
    return uid.value


def validate_label(label: str) -> str:
    if not isinstance(label, str) or not _LABEL.fullmatch(label):
        raise ValueError("invalid launchd service label")
    return label


def gui_domain() -> str:
    return f"gui/{os.getuid()}"


def launchctl(arguments: list[str]) -> subprocess.CompletedProcess[str]:
    if sys.platform != "darwin":
        raise OSError("launchd requires macOS")
    return subprocess.run(
        [LAUNCHCTL, *arguments], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True, check=False, timeout=CONTROL_TIMEOUT_SECONDS,
        env=dict(os.environ, LC_ALL="C", LANG="C"),
    )


def manager_available() -> bool:
    if sys.platform != "darwin":
        return False
    try:
        return launchctl(["print", gui_domain()]).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def job_status(label: str) -> tuple[OwnerState, int | None, bool | None]:
    """Return state, running PID and registration, conservatively.

    launchctl print is diagnostic text, so accept only recognized observations.
    An unrecognized format, inaccessible domain or failed query is UNKNOWN.
    A loaded job with no runs may still be awaiting its first launch.
    """
    if sys.platform != "darwin":
        return OwnerState.UNKNOWN, None, None
    try:
        validate_label(label)
        result = launchctl(["print", f"{gui_domain()}/{label}"])
        if result.returncode != 0:
            missing = re.search(r'Could not find service "' + re.escape(label) + r'" in domain ', result.stderr or "")
            if result.returncode == 113 and missing and manager_available():
                return OwnerState.ABSENT, None, False
            return OwnerState.UNKNOWN, None, None
        output = result.stdout or ""
        # Top-level fields are indented once; nested subprocess/resource fields
        # must not be mistaken for the service's own state or PID.
        pid_match = re.search(r"^\tpid = ([1-9][0-9]*)\s*$", output, re.MULTILINE)
        if pid_match:
            return OwnerState.ALIVE, int(pid_match.group(1)), True
        state = re.search(r"^\tstate = ([^\r\n]+)$", output, re.MULTILINE)
        runs = re.search(r"^\truns = ([0-9]+)\s*$", output, re.MULTILINE)
        if state and state.group(1).strip() in {"running", "spawn scheduled", "spawning", "throttled", "exiting"}:
            return OwnerState.ALIVE, None, True
        if state and state.group(1).strip() in {"not running", "exited"} and runs and int(runs.group(1)) > 0:
            return OwnerState.ABSENT, None, True
        return OwnerState.UNKNOWN, None, True
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return OwnerState.UNKNOWN, None, None


def plist_text(configuration: dict[str, object]) -> str:
    return plistlib.dumps(configuration, fmt=plistlib.FMT_XML, sort_keys=True).decode("utf-8")


class LaunchdBackend:
    name = "launchd"

    def available(self) -> bool:
        return manager_available()

    def owner_name(self, task_id: str, attempt: int, queue_root: Path) -> str:
        if not isinstance(task_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", task_id):
            raise ValueError("invalid task id")
        if type(attempt) is not int or attempt < 1:
            raise ValueError("launch attempt must be a positive integer")
        queue_key = hashlib.sha256(os.fsencode(queue_root.expanduser().resolve())).hexdigest()[:16]
        task_key = task_id if len(task_id) <= 80 else hashlib.sha256(task_id.encode("ascii")).hexdigest()
        return validate_label(f"ltc.{queue_key}.{task_key}.a{attempt}")

    def launch(self, owner: str, argv: list[str], cwd: Path, log_path: Path) -> None:
        from .. import storage  # Avoid a storage/platform package import cycle.

        try:
            validate_label(owner)
            if sys.platform != "darwin":
                raise ValueError("launchd workers require macOS")
            if not argv or any(not isinstance(value, str) or "\0" in value for value in argv):
                raise ValueError("worker argv must contain non-NUL strings")
            if not Path(argv[0]).is_absolute():
                raise ValueError("worker executable must use an absolute path")
            output = log_path.expanduser().resolve()
            configuration = {
                "Label": owner, "ProgramArguments": argv,
                "WorkingDirectory": str(cwd.expanduser().resolve()),
                "RunAtLoad": True, "KeepAlive": False, "AbandonProcessGroup": False,
                "Umask": 0o077, "StandardInPath": "/dev/null",
                "StandardOutPath": str(output), "StandardErrorPath": str(output),
                "EnvironmentVariables": {OWNER_ENV: owner, "PYTHONUNBUFFERED": "1"},
            }
            path = output.parent / f"{owner}.plist"
            storage.write_private_text(path, plist_text(configuration))
        except (OSError, ValueError, TypeError) as error:
            raise LaunchError("could not prepare launchd worker configuration", uncertain=False) from error
        try:
            result = launchctl(["bootstrap", gui_domain(), str(path)])
        except subprocess.TimeoutExpired as error:
            raise LaunchError("launchd submission timed out; its outcome is unknown", uncertain=True) from error
        except OSError as error:
            raise LaunchError("could not execute launchctl", uncertain=False) from error
        if result.returncode != 0:
            raise LaunchError(f"launchctl bootstrap exited with status {result.returncode}; reconcile before retrying",
                              uncertain=True)

    def probe(self, owner: str) -> OwnerState:
        return job_status(owner)[0]

    def admits_worker(self, owner: str) -> bool:
        return (sys.platform == "darwin" and os.getppid() == 1
                and os.environ.get(OWNER_ENV) == owner
                and os.environ.get("XPC_SERVICE_NAME") == owner
                and job_status(owner)[:2] == (OwnerState.ALIVE, os.getpid()))

    def collect(self, owner: str) -> bool:
        """Remove an exited terminal task's registration; never stop a worker."""
        state, _pid, registered = job_status(owner)
        if state != OwnerState.ABSENT:
            return False
        if registered is False:
            return True
        try:
            return launchctl(["bootout", f"{gui_domain()}/{owner}"]).returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            return False
