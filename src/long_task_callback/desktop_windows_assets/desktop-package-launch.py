"""Experimental Desktop launcher for an explicitly supplied MSIX package context.

Invoke this helper with Windows' Invoke-CommandInDesktopPackage. That command
uses a debugging token, so success does not establish normal activation parity.
Only --request is accepted; environment overrides are never put on the command
line or written to the result. Check mode never runs the Desktop main thread.
"""

import argparse
import ctypes
from ctypes import wintypes
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys


ENVIRONMENT_KEYS = frozenset({
    "CODEX_HOME",
    "CODEX_CLI_PATH",
    "CODEX_APP_SERVER_FORCE_CLI",
    "CODEX_APP_SERVER_WS_URL",
    "CODEX_LONG_TASK_WAKEUP_DESKTOP_REAL_CODEX",
    "CODEX_LONG_TASK_WAKEUP_DESKTOP_BRIDGE_FILE",
    "CODEX_LONG_TASK_WAKEUP_DESKTOP_APP_SERVER",
})
REQUEST_KEYS = frozenset({
    "expected_package_full_name", "desktop_executable", "environment",
    "result_file", "check_only",
})
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ctypes.c_size_t),
        ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", wintypes.LONG),
        ("dwFlags", wintypes.DWORD), ("szExeFile", wintypes.WCHAR * 260),
    ]


def current_package_full_name():
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetCurrentPackageFullName.argtypes = [ctypes.POINTER(wintypes.DWORD), wintypes.LPWSTR]
    kernel.GetCurrentPackageFullName.restype = wintypes.LONG
    length = wintypes.DWORD()
    status = kernel.GetCurrentPackageFullName(ctypes.byref(length), None)
    if status != 122 or not 1 < length.value <= 65536:
        raise RuntimeError("The helper has no verifiable Windows package identity (status %d)" % status)
    buffer = ctypes.create_unicode_buffer(length.value)
    status = kernel.GetCurrentPackageFullName(ctypes.byref(length), buffer)
    if status:
        raise RuntimeError("Windows package identity lookup failed (status %d)" % status)
    return buffer.value


def _normalized_image(path):
    value = os.path.normcase(os.path.normpath(str(path)))
    return value[4:] if value.startswith("\\\\?\\") else value


def existing_desktop_processes(executable):
    """Read exact executable paths; never stop an existing process.

    A same-named process whose identity cannot be read is a conservative error.
    This snapshot cannot prevent another independent launcher from racing us;
    the Desktop's own single-instance behavior remains authoritative.
    """
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    kernel.Process32FirstW.restype = wintypes.BOOL
    kernel.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    kernel.Process32NextW.restype = wintypes.BOOL
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)
    ]
    kernel.QueryFullProcessImageNameW.restype = wintypes.BOOL
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    snapshot = kernel.CreateToolhelp32Snapshot(2, 0)  # TH32CS_SNAPPROCESS
    if snapshot == INVALID_HANDLE_VALUE:
        raise ctypes.WinError(ctypes.get_last_error())
    found = []
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(entry)
        valid = kernel.Process32FirstW(snapshot, ctypes.byref(entry))
        count = 0
        while valid:
            count += 1
            if count > 65536:
                raise RuntimeError("Windows process snapshot exceeded its safety bound")
            if entry.szExeFile.casefold() == executable.name.casefold():
                # Native handles pin process identity through the path query.
                handle = kernel.OpenProcess(0x00100000 | 0x1000, False, entry.th32ProcessID)
                if not handle:
                    code = ctypes.get_last_error()
                    if code != 87:  # A process that already exited may be absent.
                        raise RuntimeError("Cannot verify a same-named Desktop process (Win32 %d)" % code)
                else:
                    try:
                        waited = kernel.WaitForSingleObject(handle, 0)
                        if waited == 258:  # WAIT_TIMEOUT: still running
                            size = wintypes.DWORD(32768)
                            image = ctypes.create_unicode_buffer(size.value)
                            if not kernel.QueryFullProcessImageNameW(handle, 0, image, ctypes.byref(size)):
                                raise ctypes.WinError(ctypes.get_last_error())
                            if _normalized_image(image.value) == _normalized_image(executable):
                                found.append(int(entry.th32ProcessID))
                        elif waited != 0:
                            raise RuntimeError("Cannot verify whether a same-named Desktop process is alive")
                    finally:
                        kernel.CloseHandle(handle)
            valid = kernel.Process32NextW(snapshot, ctypes.byref(entry))
        code = ctypes.get_last_error()
        if code != 18:  # ERROR_NO_MORE_FILES
            raise ctypes.WinError(code)
    finally:
        kernel.CloseHandle(snapshot)
    return found


def _no_duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate request field")
        result[key] = value
    return result


def read_request(request_file):
    if not request_file.is_absolute() or request_file.suffix.lower() != ".json":
        raise ValueError("The request must be an absolute JSON file path")
    request_file = request_file.resolve(strict=True)
    with request_file.open("rb") as stream:
        raw = stream.read(65537)
    if len(raw) > 65536:
        raise ValueError("The request exceeds 64 KiB")
    request = json.loads(raw.decode("utf-8-sig"), object_pairs_hook=_no_duplicate_keys)
    if not isinstance(request, dict):
        raise ValueError("The request must be an object")
    result_name = request.get("result_file")
    if not isinstance(result_name, str) or "\0" in result_name:
        raise ValueError("The result path must be an absolute path named result.json")
    result_file = Path(result_name)
    if (not result_file.is_absolute() or result_file.name != "result.json"
            or result_file.resolve().parent != request_file.parent
            or result_file.resolve() == request_file):
        raise ValueError("The result must be result.json in the request directory")
    # Do not overwrite a prior result or follow an existing result symlink.
    if result_file.exists() or result_file.is_symlink():
        raise ValueError("The result already exists; use a fresh request directory")
    return request, result_file


def validate_request(request):
    if request.keys() != REQUEST_KEYS:
        raise ValueError("The request must contain exactly the five documented fields")
    expected = request["expected_package_full_name"]
    if not isinstance(expected, str) or not expected or "\0" in expected:
        raise ValueError("Expected package identity must be a nonempty string")
    actual = current_package_full_name()
    if actual != expected:
        raise RuntimeError("Windows package identity does not match the expected package")
    executable = request["desktop_executable"]
    if not isinstance(executable, str) or "\0" in executable:
        raise ValueError("Desktop executable must be an absolute .exe path")
    executable = Path(executable)
    if not executable.is_absolute() or executable.suffix.lower() != ".exe" or not executable.is_file():
        raise ValueError("Desktop executable must be an existing absolute .exe path")
    if type(request["check_only"]) is not bool:
        raise ValueError("check_only must be a JSON boolean")
    overrides = request["environment"]
    if not isinstance(overrides, dict) or overrides.keys() != ENVIRONMENT_KEYS:
        raise ValueError("Environment must contain exactly the seven documented overrides")
    for value in overrides.values():
        if value is not None and (not isinstance(value, str) or "\0" in value or len(value) > 32767):
            raise ValueError("Environment override values must be bounded strings or null")
    return actual, executable, overrides


def launch(request):
    package, executable, overrides = validate_request(request)
    # These settings affect this short-lived helper and its child only. Never
    # edit user/machine environment variables, registry, or shell settings.
    saved = {key: os.environ.get(key) for key in overrides}
    try:
        for key, value in overrides.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        source = Path(__file__).resolve().with_name("probe-desktop-launch.py")
        specification = importlib.util.spec_from_file_location("desktop_native_probe", source)
        helper = importlib.util.module_from_spec(specification)
        specification.loader.exec_module(helper)
        native_probe, code = helper.probe(str(executable))
        result = {"ok": False, "package_full_name": package, "native_launch_probe": native_probe}
        if code or not native_probe["cleanup_complete"] or not native_probe["can_create_suspended"]:
            result.update(error_type="NativeLaunchProbeError", message="Desktop suspended creation or cleanup failed")
            return result
        if (native_probe.get("child_package_query_status") != 0
                or native_probe.get("child_package_full_name") != package):
            result.update(error_type="PackageIdentityError",
                          message="The suspended Desktop child did not retain the expected Windows package identity")
            return result
        if not request["check_only"]:
            if existing_desktop_processes(executable):
                raise RuntimeError("The exact Desktop executable is already running; no new instance was started")
            process = subprocess.Popen([str(executable)], cwd=str(executable.parent),
                                       env=os.environ.copy(), close_fds=True)
            result["desktop_pid"] = process.pid
        result["ok"] = True
        return result
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request", required=True, help="Absolute path to a private request JSON file")
    args = parser.parse_args(argv)
    result_file = None
    try:
        if os.name != "nt":
            raise RuntimeError("This helper runs only on Windows")
        request, result_file = read_request(Path(args.request))
        result = launch(request)
    except Exception as error:
        result = {"ok": False, "error_type": type(error).__name__, "message": str(error)}
    if result_file is None:
        # Do not guess a destination from an invalid request or print secrets.
        print("Invalid package launch request: " + result["message"], file=sys.stderr)
        return 1
    try:
        # One closed write, no rename/replace: package filesystem virtualization
        # can deny replacement even when creating files here is permitted.
        with result_file.open("x", encoding="utf-8") as stream:
            json.dump(result, stream, ensure_ascii=True)
            stream.write("\n")
    except OSError as error:
        print("Cannot write package launch result (Win32 %s)" % getattr(error, "winerror", None), file=sys.stderr)
        return 1
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
