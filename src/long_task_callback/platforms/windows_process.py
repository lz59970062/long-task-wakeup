"""Windows process containment, shell-free launchers and delivery lock transfer.

The runner owns its Job handle for its entire process lifetime.  In particular,
the coordinator must never own that handle and Python finalizers must not close
it: closing the last handle also terminates the runner itself.
"""
from __future__ import annotations

import copy
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import threading
import time
from typing import Mapping, MutableMapping, Sequence


LOCK_HANDLES_ENV = "CODEX_LONG_TASK_DELIVERY_LOCK_HANDLES"
_WORKER_JOB_HANDLE: int | None = None
_JOB_MUTEX = threading.Lock()
_SPAWN_MUTEX = threading.Lock()


def _kernel32():
    if os.name != "nt":
        raise RuntimeError("Windows process operations require Windows")
    api = ctypes.WinDLL("kernel32", use_last_error=True)
    signatures = {
        "GetCurrentProcess": ([], wintypes.HANDLE),
        "CreateJobObjectW": ([ctypes.c_void_p, wintypes.LPCWSTR], wintypes.HANDLE),
        "SetInformationJobObject": ([wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD], wintypes.BOOL),
        "AssignProcessToJobObject": ([wintypes.HANDLE, wintypes.HANDLE], wintypes.BOOL),
        "SetHandleInformation": ([wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD], wintypes.BOOL),
        "DuplicateHandle": ([wintypes.HANDLE, wintypes.HANDLE, wintypes.HANDLE, ctypes.POINTER(wintypes.HANDLE), wintypes.DWORD, wintypes.BOOL, wintypes.DWORD], wintypes.BOOL),
        "CloseHandle": ([wintypes.HANDLE], wintypes.BOOL),
        "QueryInformationJobObject": ([wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)], wintypes.BOOL),
        "OpenProcess": ([wintypes.DWORD, wintypes.BOOL, wintypes.DWORD], wintypes.HANDLE),
        "IsProcessInJob": ([wintypes.HANDLE, wintypes.HANDLE, ctypes.POINTER(wintypes.BOOL)], wintypes.BOOL),
        "TerminateProcess": ([wintypes.HANDLE, wintypes.UINT], wintypes.BOOL),
        "WaitForSingleObject": ([wintypes.HANDLE, wintypes.DWORD], wintypes.DWORD),
    }
    for name, (arguments, result) in signatures.items():
        function = getattr(api, name)
        function.argtypes, function.restype = arguments, result
    return api


class _BasicLimits(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_int64),
        ("PerJobUserTimeLimit", ctypes.c_int64),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class _IoCounters(ctypes.Structure):
    _fields_ = [(name, ctypes.c_uint64) for name in (
        "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
        "ReadTransferCount", "WriteTransferCount", "OtherTransferCount",
    )]


class _ExtendedLimits(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _BasicLimits),
        ("IoInfo", _IoCounters),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


def enter_worker_job() -> int:
    """Contain this worker and all future children; fail before doing any work.

    Windows 8+ nested jobs allow a Task Scheduler or sandbox outer Job. If its
    policy prevents assignment, the native error is fatal rather than allowing
    an uncontained workload. The unnamed handle is non-inheritable and is left
    open until the OS closes it at process exit, including abnormal exit.
    """
    global _WORKER_JOB_HANDLE
    with _JOB_MUTEX:
        if _WORKER_JOB_HANDLE is not None:
            return _WORKER_JOB_HANDLE
        api = _kernel32()
        handle = api.CreateJobObjectW(None, None)
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        limits = _ExtendedLimits()
        limits.BasicLimitInformation.LimitFlags = 0x00002000  # KILL_ON_JOB_CLOSE
        try:
            if not api.SetHandleInformation(handle, 1, 0):
                raise ctypes.WinError(ctypes.get_last_error())
            if not api.SetInformationJobObject(handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
                raise ctypes.WinError(ctypes.get_last_error())
            if not api.AssignProcessToJobObject(handle, api.GetCurrentProcess()):
                raise ctypes.WinError(ctypes.get_last_error())
        except BaseException:
            api.CloseHandle(handle)
            raise
        _WORKER_JOB_HANDLE = int(handle)
        return _WORKER_JOB_HANDLE


def _job_process_ids(api, job_handle: int) -> set[int]:
    capacity = 32
    while capacity <= 65536:
        data = ctypes.create_string_buffer(8 + ctypes.sizeof(ctypes.c_size_t) * capacity)
        length = wintypes.DWORD()
        okay = api.QueryInformationJobObject(job_handle, 3, data, len(data), ctypes.byref(length))
        if not okay:
            error = ctypes.get_last_error()
            if error == 234:  # ERROR_MORE_DATA: membership grew while querying.
                capacity *= 2
                continue
            raise ctypes.WinError(error)
        assigned = wintypes.DWORD.from_buffer(data, 0).value
        count = wintypes.DWORD.from_buffer(data, 4).value
        if count > capacity or count > assigned or length.value < 8 + count * ctypes.sizeof(ctypes.c_size_t):
            raise RuntimeError("Invalid worker Job process list")
        if assigned > count:
            capacity = max(capacity * 2, assigned)
            continue
        members = {int(ctypes.c_size_t.from_buffer(data, 8 + index * ctypes.sizeof(ctypes.c_size_t)).value)
                   for index in range(count)}
        if len(members) != count or any(pid <= 0 or pid > 0xFFFFFFFF for pid in members):
            raise RuntimeError("Invalid worker Job process identity list")
        return members
    raise RuntimeError("Worker Job process list exceeds its cleanup limit")


def terminate_worker_children(job_handle: int, *, timeout: float = 5.0) -> None:
    """Drain this process's own worker Job before reporting graceful shutdown.

    KILL_ON_JOB_CLOSE is still needed for crashes, but its termination of child
    processes is asynchronous after the parent exits. Graceful bridge teardown
    instead pins each child with an OS process handle, verifies membership in
    the exact Job, terminates that handle, and waits for all members except
    ourselves to exit. Never close the Job (it also contains this process).
    This is deliberately not an arbitrary PID/tree termination primitive.
    """
    import math

    if type(job_handle) is not int or job_handle != _WORKER_JOB_HANDLE:
        raise ValueError("Cleanup requires this process's own worker Job handle")
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("Worker Job cleanup timeout must be finite and positive")
    api = _kernel32()
    own_membership = wintypes.BOOL()
    if not api.IsProcessInJob(api.GetCurrentProcess(), job_handle, ctypes.byref(own_membership)):
        raise ctypes.WinError(ctypes.get_last_error())
    if not own_membership.value:
        raise RuntimeError("Worker does not belong to the supplied Job")
    deadline = time.monotonic() + timeout
    while True:
        members = _job_process_ids(api, job_handle)
        if os.getpid() not in members:
            raise RuntimeError("Worker Job no longer reports its owner")
        members.discard(os.getpid())
        if not members:
            return
        if time.monotonic() >= deadline:
            raise TimeoutError("Worker Job children did not finish cleanup")
        handles = []
        try:
            for pid in members:
                if time.monotonic() >= deadline:
                    raise TimeoutError("Worker Job children did not finish cleanup")
                handle = api.OpenProcess(0x0001 | 0x00100000 | 0x1000, False, pid)
                if not handle:
                    error = ctypes.get_last_error()
                    if error == 87:  # The process exited after the snapshot.
                        continue
                    raise OSError(f"OpenProcess failed during worker Job cleanup (Windows error {error})")
                handles.append(handle)
                belongs = wintypes.BOOL()
                if not api.IsProcessInJob(handle, job_handle, ctypes.byref(belongs)):
                    raise OSError(f"IsProcessInJob failed during worker Job cleanup (Windows error {ctypes.get_last_error()})")
                if not belongs.value:
                    # A recycled PID can now name an unrelated process. Its
                    # handle is only closed, never terminated or waited on.
                    handles.pop()
                    api.CloseHandle(handle)
                    continue
                if not api.TerminateProcess(handle, 1):
                    error = ctypes.get_last_error()
                    # Windows reports ACCESS_DENIED when termination has
                    # already begun, before the process handle is signaled.
                    # Still wait on this exact verified member handle below;
                    # refusal is never treated as evidence of disappearance.
                    if error != 5 and api.WaitForSingleObject(handle, 0) != 0:
                        raise OSError(f"TerminateProcess failed during worker Job cleanup (Windows error {error})")
            for handle in handles:
                remaining = max(0, int((deadline - time.monotonic()) * 1000))
                status = api.WaitForSingleObject(handle, remaining)
                if status == 258:
                    raise TimeoutError("Worker Job child did not finish cleanup")
                if status != 0:
                    raise ctypes.WinError(ctypes.get_last_error())
        finally:
            for handle in handles:
                api.CloseHandle(handle)
        # A child can have forked between the first snapshot and termination.
        # Membership is authoritative and includes nested Jobs; repeat until
        # only this bridge remains before releasing logs or singleton state.


def background_popen_kwargs() -> dict[str, object]:
    """Launch console executables without a visible console window."""
    if os.name != "nt":
        return {}
    return {"creationflags": 0x08000000 | 0x00000200}  # NO_WINDOW | NEW_PROCESS_GROUP


def format_command(argv: Sequence[str]) -> str:
    """Render literal PowerShell arguments, including paths and single quotes."""
    return "& " + " ".join("'" + str(value).replace("'", "''") + "'" for value in argv)


def _environment_value(env: Mapping[str, str], name: str) -> str:
    return next((value for key, value in env.items() if key.upper() == name.upper()), "")


def _resolve_executable(command: str, env: Mapping[str, str]) -> Path:
    path = Path(command).expanduser()
    has_directory = path.is_absolute() or "\\" in command or "/" in command
    if has_directory:
        candidates = [path] if path.suffix else [Path(str(path) + ext) for ext in (".exe", ".com", ".cmd", ".bat")]
        for candidate in candidates:
            if candidate.is_file():
                return candidate.resolve()
    else:
        found = shutil.which(command, path=_environment_value(env, "PATH"))
        if found:
            return Path(found).resolve()
    raise FileNotFoundError(f"Windows executable not found: {command}")


_NPM_PREAMBLE = re.compile(
    r"@echo off\ngoto start\n:find_dp0\nset dp0=%~dp0\nexit /b\n:start\nsetlocal\ncall :find_dp0\n",
    re.IGNORECASE,
)
_NPM_NODE_TAIL = re.compile(
    r'if exist "%dp0%\\node\.exe" \(\n'
    r'set "_prog=%dp0%\\node\.exe"\n'
    r'\) else \(\nset "_prog=node"\n'
    r'set pathext=%pathext:;\.js;=;%\n\)\n'
    r'endlocal & goto #_undefined_# 2>nul \|\| title %comspec% & "%_prog%"\s+'
    r'"%dp0%\\(?P<script>node_modules\\[^"\r\n]+\.(?:js|cjs|mjs))"\s+%\*',
    re.IGNORECASE,
)
_NPM_NATIVE_TAIL = re.compile(r'"%dp0%\\(?P<script>node_modules\\[^"\r\n]+\.exe)"\s+%\*', re.IGNORECASE)


def _npm_command(path: Path, content: str, env: Mapping[str, str]) -> list[str] | None:
    # Validate the complete npm template. Do not execute or silently discard
    # arbitrary batch commands that happen to mention a node_modules path.
    normalized = "\n".join(line.strip() for line in content.splitlines() if line.strip())
    preamble = _NPM_PREAMBLE.match(normalized)
    if preamble is None:
        return None
    tail = normalized[preamble.end():]
    match = _NPM_NATIVE_TAIL.fullmatch(tail) or _NPM_NODE_TAIL.fullmatch(tail)
    if match is None:
        return None
    script = (path.parent / match.group("script").replace("\\", os.sep)).resolve()
    try:
        script.relative_to((path.parent / "node_modules").resolve())
    except ValueError:
        raise ValueError(f"npm shim target escapes node_modules: {path}") from None
    if not script.is_file():
        raise FileNotFoundError(f"npm shim target is missing: {path}")
    if script.suffix.lower() == ".exe":
        return [str(script)]
    local_node = path.parent / "node.exe"
    node = local_node if local_node.is_file() else _resolve_executable("node.exe", env)
    return [str(node), str(script)]


def _forwarding_command(path: Path, content: str, env: MutableMapping[str, str]) -> list[str] | None:
    lines = [line.strip() for line in content.splitlines() if line.strip()]
    if lines and lines[0].lower() == "@echo off":
        lines.pop(0)
    if lines and lines[-1].lower() == "exit /b %errorlevel%":
        lines.pop()
    if not lines:
        return None
    assignments: list[tuple[str, str]] = []
    for line in lines[:-1]:
        match = re.fullmatch(r'set\s+(?:"([^"=\s]+)=([^"\r\n]*)"|([^=\s]+)=([^\r\n]*))', line, re.IGNORECASE)
        if match is None:
            return None
        key, value = (match.group(1), match.group(2)) if match.group(1) else (match.group(3), match.group(4))
        if "!" in value or (not match.group(1) and any(char in value for char in '&|<>^()')):
            return None
        assignments.append((key, value))
    match = re.fullmatch(r'(.+?)\s+%\*', lines[-1])
    if match is None:
        return None
    prefix = match.group(1)
    arguments: list[str] = []
    position = 0
    for token in re.finditer(r'"([^"%!\r\n]*)"|([^\s"&|<>^()%!]+)', prefix):
        if prefix[position:token.start()].strip():
            return None
        if position and token.start() == position:
            return None  # Concatenated quoted fragments have cmd-specific rules.
        arguments.append(token.group(1) if token.group(1) is not None else token.group(2))
        position = token.end()
    if not arguments or not arguments[0] or prefix[position:].strip():
        return None
    for key, value in assignments:
        value = re.sub(r"%([^%]+)%", lambda item: _environment_value(env, item.group(1)), value)
        existing = next((name for name in env if name.upper() == key.upper()), key)
        env[existing] = value
    return arguments


def prepare_command(argv: Sequence[str], env: MutableMapping[str, str] | None = None) -> list[str]:
    """Resolve native executables and recognized npm/forwarding batch shims.

    Arbitrary batch scripts are rejected: CreateProcess's implicit cmd handling
    does not preserve literal untrusted arguments. A supplied environment is
    updated with the supported forwarding wrapper's SET statements, and must
    also be passed to Popen. With no environment, such wrappers use a private
    Python launcher so their environment values never appear on a command line.
    """
    if not argv:
        raise ValueError("a command is required")
    command = [str(part) for part in argv]
    if any("\x00" in part for part in command):
        raise ValueError("command arguments cannot contain NUL")
    if os.name != "nt":
        return command
    child_env = dict(os.environ) if env is None else env
    original_arguments = command[1:]
    initial = _resolve_executable(command[0], child_env)
    executable = initial
    seen: set[Path] = set()
    for _ in range(10):
        suffix = executable.suffix.lower()
        if suffix in {".exe", ".com"}:
            return [str(executable), *command[1:]]
        if suffix not in {".cmd", ".bat"}:
            raise ValueError(f"Unsupported Windows executable type: {executable}")
        if executable in seen:
            raise ValueError(f"Recursive Windows command shim: {initial}")
        seen.add(executable)
        content = executable.read_text(encoding="utf-8-sig")
        npm = _npm_command(executable, content, child_env)
        if npm is not None:
            return [*npm, *command[1:]]
        before = dict(child_env)
        forwarded = _forwarding_command(executable, content, child_env)
        if forwarded is None:
            raise ValueError(f"Unsupported batch shim; configure its native executable instead: {executable}")
        if env is None and child_env != before:
            return [sys.executable, str(Path(__file__).resolve()), "--shim", str(initial), *original_arguments]
        command = [*forwarded, *command[1:]]
        executable = _resolve_executable(command[0], child_env)
    raise ValueError(f"Windows command shim chain is too long: {initial}")


def spawn_delivery_worker(
    command: Sequence[str], lock_fds: Sequence[int], env: Mapping[str, str] | None = None, **kwargs,
) -> subprocess.Popen:
    """Transfer independent copies of locks without changing parent inheritance.

    The caller closes its lock files after this returns successfully. The child
    continues owning the same exclusive CreateFile objects until its fds close.
    """
    import msvcrt

    if kwargs.get("shell") or kwargs.get("close_fds", True) is not True or kwargs.get("pass_fds"):
        raise ValueError("delivery workers require shell=False, close_fds=True and explicit handles")
    child_env = dict(os.environ if env is None else env)
    prepared = prepare_command(command, env=child_env)
    api = _kernel32()
    handles: list[int] = []
    with _SPAWN_MUTEX:
        try:
            current = api.GetCurrentProcess()
            for fd in lock_fds:
                duplicate = wintypes.HANDLE()
                if not api.DuplicateHandle(current, msvcrt.get_osfhandle(fd), current, ctypes.byref(duplicate), 0, True, 2):
                    raise ctypes.WinError(ctypes.get_last_error())
                handles.append(int(duplicate.value))
            child_env.pop("CODEX_LONG_TASK_DELIVERY_LOCK_FDS", None)
            child_env.pop("CODEX_LONG_TASK_DELIVERY_LOCK_FD", None)
            child_env[LOCK_HANDLES_ENV] = json.dumps(handles)
            startup = copy.copy(kwargs.pop("startupinfo", None)) or subprocess.STARTUPINFO()
            startup.lpAttributeList = dict(startup.lpAttributeList or {})
            startup.lpAttributeList["handle_list"] = list(startup.lpAttributeList.get("handle_list", [])) + handles
            kwargs["creationflags"] = int(kwargs.get("creationflags", 0)) | int(background_popen_kwargs()["creationflags"])
            kwargs["close_fds"] = True
            return subprocess.Popen(prepared, startupinfo=startup, env=child_env, **kwargs)
        finally:
            for handle in handles:
                api.CloseHandle(handle)


def inherited_lock_fds() -> list[int]:
    """Consume inherited lock handles into non-inheritable CRT descriptors."""
    import msvcrt

    encoded = os.environ.pop(LOCK_HANDLES_ENV, None)
    handles = json.loads(encoded) if encoded is not None else None
    if not isinstance(handles, list) or not handles or len(handles) > 64 or any(type(handle) is not int or handle <= 0 for handle in handles) or len(set(handles)) != len(handles):
        raise ValueError("delivery worker requires a valid inherited lock handle list")
    api = _kernel32()
    descriptors: list[int] = []
    try:
        for handle in handles:
            if not api.SetHandleInformation(handle, 1, 0):
                raise ctypes.WinError(ctypes.get_last_error())
            descriptor = msvcrt.open_osfhandle(handle, os.O_RDWR | os.O_BINARY)
            descriptors.append(descriptor)
        return descriptors
    except BaseException:
        for descriptor in descriptors:
            os.close(descriptor)
        for handle in handles[len(descriptors):]:
            api.CloseHandle(handle)
        raise


def _main() -> int:
    if len(sys.argv) < 3 or sys.argv[1] != "--shim":
        raise ValueError("this private launcher requires --shim and a shim path")
    environment = dict(os.environ)
    command = prepare_command(sys.argv[2:], env=environment)
    # Explicit standard handles preserve a redirected parent's streams when
    # CREATE_NO_WINDOW starts the final executable without an attached console.
    return subprocess.call(command, env=environment, shell=False, stdin=sys.stdin,
                           stdout=sys.stdout, stderr=sys.stderr, **background_popen_kwargs())


if __name__ == "__main__":
    raise SystemExit(_main())
