"""Diagnose CreateProcess permissions without running any executable code.

The only child this probe can create starts suspended. It is never resumed and
is terminated through its returned process handle before the probe exits.
"""

import argparse
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import subprocess
import sys


CREATE_SUSPENDED = 0x00000004
CREATE_UNICODE_ENVIRONMENT = 0x00000400
CREATE_NO_WINDOW = 0x08000000
STARTF_USESHOWWINDOW = 0x00000001
WAIT_OBJECT_0 = 0
WAIT_TIMEOUT = 258
WAIT_FAILED = 0xFFFFFFFF


class STARTUPINFOW(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD),
        ("lpReserved", wintypes.LPWSTR),
        ("lpDesktop", wintypes.LPWSTR),
        ("lpTitle", wintypes.LPWSTR),
        ("dwX", wintypes.DWORD),
        ("dwY", wintypes.DWORD),
        ("dwXSize", wintypes.DWORD),
        ("dwYSize", wintypes.DWORD),
        ("dwXCountChars", wintypes.DWORD),
        ("dwYCountChars", wintypes.DWORD),
        ("dwFillAttribute", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("wShowWindow", wintypes.WORD),
        ("cbReserved2", wintypes.WORD),
        ("lpReserved2", ctypes.POINTER(wintypes.BYTE)),
        ("hStdInput", wintypes.HANDLE),
        ("hStdOutput", wintypes.HANDLE),
        ("hStdError", wintypes.HANDLE),
    ]


class PROCESS_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("hProcess", wintypes.HANDLE),
        ("hThread", wintypes.HANDLE),
        ("dwProcessId", wintypes.DWORD),
        ("dwThreadId", wintypes.DWORD),
    ]


def _kernel32():
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetCurrentProcess.argtypes = []
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.IsProcessInJob.argtypes = [
        wintypes.HANDLE, wintypes.HANDLE, ctypes.POINTER(wintypes.BOOL)
    ]
    kernel.IsProcessInJob.restype = wintypes.BOOL
    kernel.CreateProcessW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.LPWSTR,
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.BOOL,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.LPCWSTR,
        ctypes.POINTER(STARTUPINFOW),
        ctypes.POINTER(PROCESS_INFORMATION),
    ]
    kernel.CreateProcessW.restype = wintypes.BOOL
    kernel.GetPackageFullName.argtypes = [
        wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD), wintypes.LPWSTR
    ]
    kernel.GetPackageFullName.restype = wintypes.LONG
    kernel.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel.TerminateProcess.restype = wintypes.BOOL
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    return kernel


def _error_text(code):
    return ctypes.FormatError(code).strip()


def _child_package_identity(kernel, handle):
    """Read the exact suspended child's package identity without resuming it."""
    length = wintypes.DWORD()
    status = int(kernel.GetPackageFullName(handle, ctypes.byref(length), None))
    if status != 122 or not 1 < length.value <= 65536:  # ERROR_INSUFFICIENT_BUFFER
        return None, status
    name = ctypes.create_unicode_buffer(length.value)
    status = int(kernel.GetPackageFullName(handle, ctypes.byref(length), name))
    if status:
        return None, status
    return name.value or None, status


def _cleanup(kernel, child):
    """Terminate and close only the exact handles returned by CreateProcessW."""
    errors = []
    terminated = False
    try:
        terminate_error = 0
        if not kernel.TerminateProcess(child.hProcess, 1):
            terminate_error = ctypes.get_last_error()
        wait_result = kernel.WaitForSingleObject(child.hProcess, 5000)
        if wait_result == WAIT_OBJECT_0:
            # A separate process may already have terminated this suspended
            # child. A signaled process handle also establishes safe cleanup.
            terminated = True
        elif wait_result == WAIT_TIMEOUT:
            errors.append("the suspended child did not exit within 5 seconds")
        elif wait_result == WAIT_FAILED:
            code = ctypes.get_last_error()
            errors.append("waiting for termination failed: %s (%d)" % (_error_text(code), code))
        else:
            errors.append("unexpected process wait result: %d" % wait_result)
        if terminate_error and not terminated:
            errors.append("TerminateProcess failed: %s (%d)" % (_error_text(terminate_error), terminate_error))
    finally:
        for label, handle in (("thread", child.hThread), ("process", child.hProcess)):
            if handle and not kernel.CloseHandle(handle):
                code = ctypes.get_last_error()
                errors.append("closing the %s handle failed: %s (%d)" % (label, _error_text(code), code))
    if errors:
        raise RuntimeError("cleanup of suspended probe PID %d failed: %s" % (child.dwProcessId, "; ".join(errors)))
    return terminated


def probe(executable):
    result = {
        "executable": executable,
        "can_create_suspended": False,
        "native_error_code": 0,
        "native_error_message": "",
        "current_process_in_job": None,
        "child_package_full_name": None,
        "child_package_query_status": None,
        "probe_resumed": False,
        "cleanup_complete": True,
    }
    try:
        kernel = _kernel32()
        in_job = wintypes.BOOL()
        if not kernel.IsProcessInJob(kernel.GetCurrentProcess(), None, ctypes.byref(in_job)):
            code = ctypes.get_last_error()
            raise OSError(code, "IsProcessInJob failed: " + _error_text(code))
        result["current_process_in_job"] = bool(in_job.value)
        startup = STARTUPINFOW()
        startup.cb = ctypes.sizeof(startup)
        startup.dwFlags = STARTF_USESHOWWINDOW
        startup.wShowWindow = 0
        child = PROCESS_INFORMATION()
        command_line = ctypes.create_unicode_buffer(subprocess.list2cmdline([executable]))
        try:
            created = kernel.CreateProcessW(
                executable,
                command_line,
                None,
                None,
                False,
                CREATE_SUSPENDED | CREATE_UNICODE_ENVIRONMENT | CREATE_NO_WINDOW,
                None,  # Inherit the caller's environment without reading or printing it.
                str(Path(executable).parent),
                ctypes.byref(startup),
                ctypes.byref(child),
            )
            if not created:
                code = ctypes.get_last_error()
                result["native_error_code"] = code
                result["native_error_message"] = _error_text(code)
            else:
                result["can_create_suspended"] = True
                name, status = _child_package_identity(kernel, child.hProcess)
                result["child_package_full_name"] = name
                result["child_package_query_status"] = status
        finally:
            # Inspect the out-parameter even when Python raises immediately
            # after the native call has supplied the new child's handles.
            if child.hProcess:
                result["cleanup_complete"] = False
                result["cleanup_complete"] = _cleanup(kernel, child)
        return result, 0
    except (Exception, KeyboardInterrupt) as error:
        print("Suspended launch probe failed: %s" % (str(error) or type(error).__name__), file=sys.stderr)
        return result, 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("executable", help="Absolute path to one Windows .exe file")
    args = parser.parse_args()
    if os.name != "nt":
        parser.error("this diagnostic runs only on Windows")
    if sys.version_info < (3, 9):
        parser.error("Python 3.9 or newer is required")
    executable = Path(args.executable)
    if not executable.is_absolute() or executable.suffix.lower() != ".exe":
        parser.error("executable must be an absolute .exe path")
    result, returncode = probe(str(executable))
    print(json.dumps(result, ensure_ascii=True))
    return returncode


if __name__ == "__main__":
    sys.exit(main())
