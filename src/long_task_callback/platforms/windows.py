"""Native Windows identities and independent Task Scheduler task owners.

Only fixed worker arguments and private-file references enter task definitions.
Business tasks have no triggers or restart policy. A failed manager observation
is UNKNOWN, so recovery cannot replay a command after a lost submission reply.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import uuid

from .base import LaunchError, OwnerState

CONTROL_TIMEOUT_SECONDS = 15.0
_OWNER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,199}\Z")
_OPERATIONS = frozenset({"available", "status", "definition", "register", "launch", "run", "stop", "collect", "delete", "enable"})


def _kernel32():
    if sys.platform != "win32":
        raise OSError("Windows process interfaces are unavailable")
    library = ctypes.WinDLL("kernel32", use_last_error=True)
    library.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    library.OpenProcess.restype = wintypes.HANDLE
    library.CloseHandle.argtypes = [wintypes.HANDLE]
    library.CloseHandle.restype = wintypes.BOOL
    library.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    library.WaitForSingleObject.restype = wintypes.DWORD
    library.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
    library.GetProcessTimes.restype = wintypes.BOOL
    return library


def current_machine_id() -> str:
    """Windows installation identity, independent of mutable computer names."""
    if sys.platform != "win32":
        raise RuntimeError("Windows host identity requires Windows")
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Cryptography",
                            0, winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as key:
            value, _kind = winreg.QueryValueEx(key, "MachineGuid")
        return str(uuid.UUID(value))
    except (OSError, ValueError, TypeError) as error:
        raise RuntimeError("could not obtain Windows host identity") from error


def current_boot_id() -> str | None:
    """Read Windows' documented per-successful-boot counter, not wall time.

    The counter is always paired with the machine ID in persisted records.
    Missing/access-denied data stays unknown; switching to a clock-derived
    fallback could falsely classify clock correction as a machine restart.
    Microsoft documents this key in:
    https://learn.microsoft.com/windows-hardware/design/device-experiences/oem-hvci-enablement
    """
    if sys.platform != "win32":
        return None
    try:
        import winreg
        path = r"SYSTEM\CurrentControlSet\Control\Session Manager\Memory Management\PrefetchParameters"
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, path, 0,
                            winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as key:
            value, kind = winreg.QueryValueEx(key, "BootId")
        if kind != winreg.REG_DWORD or type(value) is not int or value < 0:
            return None
        return f"windows-boot-{value}"
    except (OSError, AttributeError, ValueError, ImportError):
        return None


def _process_sid(handle) -> str:
    kernel = _kernel32()
    security = ctypes.WinDLL("advapi32", use_last_error=True)
    security.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
    security.OpenProcessToken.restype = wintypes.BOOL
    security.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_uint32, ctypes.c_void_p,
                                            wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    security.GetTokenInformation.restype = wintypes.BOOL
    security.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.LPWSTR)]
    security.ConvertSidToStringSidW.restype = wintypes.BOOL
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    token = wintypes.HANDLE()
    if not security.OpenProcessToken(handle, 0x0008, ctypes.byref(token)):  # TOKEN_QUERY
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        size = wintypes.DWORD()
        security.GetTokenInformation(token, 1, None, 0, ctypes.byref(size))  # TokenUser
        if not size.value:
            raise ctypes.WinError(ctypes.get_last_error())
        buffer = ctypes.create_string_buffer(size.value)
        if not security.GetTokenInformation(token, 1, buffer, size, ctypes.byref(size)):
            raise ctypes.WinError(ctypes.get_last_error())
        sid = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_void_p))[0]
        text = wintypes.LPWSTR()
        if not security.ConvertSidToStringSidW(sid, ctypes.byref(text)):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            return text.value
        finally:
            kernel.LocalFree(ctypes.cast(text, ctypes.c_void_p))
    finally:
        kernel.CloseHandle(token)


def process_identity(pid: int) -> dict[str, object] | None:
    """Read creation time and owner from one live process handle (PID-reuse safe)."""
    if sys.platform != "win32" or type(pid) is not int or pid <= 0:
        return None
    try:
        kernel = _kernel32()
        handle = kernel.OpenProcess(0x1000 | 0x100000, False, pid)
        if not handle:
            return None
        try:
            if kernel.WaitForSingleObject(handle, 0) != 258:  # WAIT_TIMEOUT = still alive
                return None
            created, exited, system, user = (wintypes.FILETIME() for _ in range(4))
            if not kernel.GetProcessTimes(handle, ctypes.byref(created), ctypes.byref(exited),
                                          ctypes.byref(system), ctypes.byref(user)):
                return None
            boot = current_boot_id()
            if not boot:
                return None
            identity = {"boot_id": boot, "machine_id": current_machine_id(),
                        "sid": _process_sid(handle),
                        "creation_time": (created.dwHighDateTime << 32) | created.dwLowDateTime}
            return identity if kernel.WaitForSingleObject(handle, 0) == 258 else None
        finally:
            kernel.CloseHandle(handle)
    except (OSError, RuntimeError, AttributeError):
        return None


def pid_is_running(pid: int) -> bool:
    """Inspect a native handle; os.kill(pid, 0) can terminate Windows processes."""
    if sys.platform != "win32" or type(pid) is not int or pid <= 0:
        return False
    try:
        kernel = _kernel32()
        handle = kernel.OpenProcess(0x100000, False, pid)  # SYNCHRONIZE
        if not handle:
            return ctypes.get_last_error() == 5  # Access denied is not proof of absence.
        try:
            return kernel.WaitForSingleObject(handle, 0) != 0
        finally:
            kernel.CloseHandle(handle)
    except (OSError, AttributeError):
        return False


def _process_image(pid: int) -> str | None:
    try:
        kernel = _kernel32()
        kernel.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                                                     wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
        kernel.QueryFullProcessImageNameW.restype = wintypes.BOOL
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            return None
        try:
            size = wintypes.DWORD(32768)
            value = ctypes.create_unicode_buffer(size.value)
            if not kernel.QueryFullProcessImageNameW(handle, 0, value, ctypes.byref(size)):
                return None
            return os.path.normcase(os.path.realpath(value.value))
        finally:
            kernel.CloseHandle(handle)
    except (OSError, AttributeError):
        return None


def validate_owner(owner: str) -> str:
    if not isinstance(owner, str) or not _OWNER.fullmatch(owner):
        raise ValueError("invalid Windows task owner")
    return owner


class SchedulerError(RuntimeError):
    """Manager failure, deliberately excluding localized or sensitive output."""


def task_configuration(argv: list[str], cwd: Path, *, login: bool = False,
                       enabled: bool = True) -> dict[str, object]:
    if not argv or any(not isinstance(value, str) or "\0" in value for value in argv):
        raise ValueError("worker argv must contain non-NUL strings")
    if not Path(argv[0]).is_absolute():
        raise ValueError("worker executable must use an absolute path")
    return {"executable": argv[0], "arguments": subprocess.list2cmdline(argv[1:]),
            "cwd": str(cwd.expanduser().resolve()), "login": bool(login), "enabled": bool(enabled)}


def scheduler_call(operation: str, payload: dict[str, object] | None = None) -> dict[str, object]:
    """Control COM with structured UTF-8 stdin; never parse schtasks text.

    The script emits one JSON object, including failures. Process launch errors
    and timeouts retain their normal exceptions so callers can distinguish a
    failure before submitting anything from a lost manager response.
    """
    if sys.platform != "win32":
        raise OSError("Task Scheduler requires Windows")
    if operation not in _OPERATIONS:
        raise ValueError("invalid scheduler operation")
    request = dict(payload or {}, operation=operation)
    if operation != "available":
        validate_owner(request.get("owner"))
    executable = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    result = subprocess.run(
        [str(executable), "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
         "-File", str(Path(__file__).with_name("windows_scheduler.ps1"))],
        input=json.dumps(request, ensure_ascii=False).encode("utf-8"), stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, check=False, timeout=CONTROL_TIMEOUT_SECONDS,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    try:
        response = json.loads(result.stdout.decode("utf-8-sig"))
    except (ValueError, UnicodeError) as error:
        raise SchedulerError("Task Scheduler returned an unreadable response") from error
    if not isinstance(response, dict) or result.returncode or response.get("ok") is not True:
        code = response.get("hresult") if isinstance(response, dict) else None
        suffix = f" (HRESULT {code})" if type(code) is int else ""
        raise SchedulerError("Task Scheduler control failed" + suffix)
    return response


def manager_available() -> bool:
    try:
        return scheduler_call("available").get("available") is True
    except (OSError, SchedulerError, subprocess.TimeoutExpired):
        return False


def job_status(owner: str) -> tuple[OwnerState, int | None, bool | None]:
    """Only an observed missing or previously-run idle task proves absence."""
    try:
        data = scheduler_call("status", {"owner": owner})
        if data.get("registered") is False:
            return OwnerState.ABSENT, None, False
        if data.get("registered") is not True:
            return OwnerState.UNKNOWN, None, None
        state, instances = data.get("state"), data.get("instances")
        if type(state) is not int or not isinstance(instances, list):
            return OwnerState.UNKNOWN, None, True
        if state in (2, 4) or instances:  # TASK_STATE_QUEUED / RUNNING
            pid = instances[0].get("pid") if len(instances) == 1 and isinstance(instances[0], dict) else None
            return OwnerState.ALIVE, pid if type(pid) is int and pid > 0 else None, True
        if state in (1, 3) and data.get("has_run") is True:
            return OwnerState.ABSENT, None, True
        return OwnerState.UNKNOWN, None, True
    except (OSError, ValueError, SchedulerError, subprocess.TimeoutExpired):
        return OwnerState.UNKNOWN, None, None


class WindowsBackend:
    name = "windows-task"

    def available(self) -> bool:
        return manager_available()

    def owner_name(self, task_id: str, attempt: int, queue_root: Path) -> str:
        if not isinstance(task_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", task_id):
            raise ValueError("invalid task id")
        if type(attempt) is not int or attempt < 1:
            raise ValueError("launch attempt must be a positive integer")
        path = os.path.normcase(str(queue_root.expanduser().resolve()))
        queue_key = hashlib.sha256(os.fsencode(path)).hexdigest()[:16]
        task_key = task_id if len(task_id) <= 80 else hashlib.sha256(task_id.encode("ascii")).hexdigest()
        return validate_owner(f"ltc.{queue_key}.{task_key}.a{attempt}")

    def launch(self, owner: str, argv: list[str], cwd: Path, log_path: Path) -> None:
        try:
            validate_owner(owner)
            if sys.platform != "win32":
                raise ValueError("Windows workers require Windows")
            configuration = task_configuration(argv, cwd)
        except (OSError, ValueError, TypeError) as error:
            raise LaunchError("could not prepare Windows worker configuration", uncertain=False) from error
        try:
            scheduler_call("launch", {"owner": owner, "configuration": configuration})
        except OSError as error:
            raise LaunchError("could not execute Task Scheduler controller", uncertain=False) from error
        except (SchedulerError, subprocess.TimeoutExpired) as error:
            raise LaunchError("Task Scheduler submission outcome is unknown; reconcile before retrying",
                              uncertain=True) from error

    def probe(self, owner: str) -> OwnerState:
        return job_status(owner)[0]

    def admits_worker(self, owner: str) -> bool:
        if sys.platform != "win32":
            return False
        pid = os.getpid()
        identity = process_identity(pid)
        state, engine_pid, _registered = job_status(owner)
        if identity is None or state != OwnerState.ALIVE or engine_pid is None:
            return False
        if engine_pid == pid:
            return process_identity(pid) == identity
        # Windows venv python.exe is a native redirector. The scheduler owns
        # that executable while Python runs in its direct child. Admit only
        # that exact interpreter chain, never an arbitrary descendant of a
        # shared task engine or a reusable parent PID.
        if sys.prefix == sys.base_prefix or engine_pid != os.getppid():
            return False
        expected_parent = os.path.normcase(os.path.realpath(sys.executable))
        expected_child = os.path.normcase(os.path.realpath(getattr(sys, "_base_executable", "")))
        if expected_parent == expected_child:
            return False
        parent_identity = process_identity(engine_pid)
        if (not parent_identity or any(parent_identity.get(key) != identity.get(key)
                                       for key in ("boot_id", "machine_id", "sid"))
                or not isinstance(parent_identity.get("creation_time"), int)
                or not isinstance(identity.get("creation_time"), int)
                or parent_identity["creation_time"] > identity["creation_time"]):
            return False
        try:
            definition = scheduler_call("definition", {"owner": owner})
            executable = definition.get("executable")
            if not isinstance(executable, str) or os.path.normcase(os.path.realpath(executable)) != expected_parent:
                return False
        except (OSError, SchedulerError, subprocess.TimeoutExpired, ValueError):
            return False
        return (_process_image(engine_pid) == expected_parent and _process_image(pid) == expected_child
                and job_status(owner)[:2] == (OwnerState.ALIVE, engine_pid)
                and process_identity(engine_pid) == parent_identity
                and process_identity(pid) == identity)

    def collect(self, owner: str) -> bool:
        """Recheck in the manager immediately before deleting an exited owner."""
        state, _pid, registered = job_status(owner)
        if state != OwnerState.ABSENT:
            return False
        if registered is False:
            return True
        try:
            return scheduler_call("collect", {"owner": owner}).get("collected") is True
        except (OSError, ValueError, SchedulerError, subprocess.TimeoutExpired):
            return False
