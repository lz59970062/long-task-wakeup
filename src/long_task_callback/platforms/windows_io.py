"""Private files and transferable, crash-released leases on Windows 10+.

The protected DACL grants access only to the current process user and SYSTEM.
Files and new directories receive it *at creation*, before any secret is written.
An ACL-capable filesystem is required. Windows sharing exclusion belongs to the
open file object: duplicated/inherited handles keep a lease after its original
owner exits. The lock path must never be unlinked while contenders may exist.

Publication flushes file data and requests MoveFileExW WRITE_THROUGH, without
COPY_ALLOWED (cross-volume publication therefore fails). Windows exposes no
supported POSIX-equivalent directory fsync. In particular, WRITE_THROUGH's
documented copy/delete guarantee is not a general rename-journal power-loss
guarantee. Process-crash recovery is supported; surviving sudden power failure
of directory creation, rename or deletion is not promised.

References:
https://learn.microsoft.com/windows/win32/api/fileapi/nf-fileapi-createfilew
https://learn.microsoft.com/windows/win32/api/winbase/nf-winbase-movefileexw
https://learn.microsoft.com/windows/win32/api/fileapi/nf-fileapi-flushfilebuffers
"""

from __future__ import annotations

from contextlib import contextmanager
import ctypes
from ctypes import wintypes
from functools import lru_cache
import os
from pathlib import Path
import stat
import time
from typing import Iterator, TextIO


GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
WRITE_DAC = 0x00040000
CREATE_NEW = 1
OPEN_EXISTING = 3
OPEN_ALWAYS = 4
FILE_ATTRIBUTE_NORMAL = 0x80
FILE_FLAG_WRITE_THROUGH = 0x80000000
ERROR_SHARING_VIOLATION = 32
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
DACL_SECURITY_INFORMATION = 0x00000004
PROTECTED_DACL_SECURITY_INFORMATION = 0x80000000
SE_FILE_OBJECT = 1


class _SecurityAttributes(ctypes.Structure):
    _fields_ = [
        ("nLength", wintypes.DWORD),
        ("lpSecurityDescriptor", ctypes.c_void_p),
        ("bInheritHandle", wintypes.BOOL),
    ]


def _bind(library, name, arguments, result):
    function = getattr(library, name)
    function.argtypes = arguments
    function.restype = result
    return function


if os.name == "nt":
    import msvcrt

    _kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    _advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    _bind(_kernel, "GetCurrentProcess", [], wintypes.HANDLE)
    _bind(_kernel, "CloseHandle", [wintypes.HANDLE], wintypes.BOOL)
    _bind(_kernel, "LocalFree", [ctypes.c_void_p], ctypes.c_void_p)
    _bind(_kernel, "CreateFileW", [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
          ctypes.POINTER(_SecurityAttributes), wintypes.DWORD, wintypes.DWORD,
          wintypes.HANDLE], wintypes.HANDLE)
    _bind(_kernel, "CreateDirectoryW", [wintypes.LPCWSTR, ctypes.POINTER(_SecurityAttributes)], wintypes.BOOL)
    _bind(_kernel, "MoveFileExW", [wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD], wintypes.BOOL)
    _bind(_kernel, "GetVolumePathNameW", [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD], wintypes.BOOL)
    _bind(_kernel, "GetVolumeInformationW", [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD,
          ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(wintypes.DWORD),
          ctypes.POINTER(wintypes.DWORD), wintypes.LPWSTR, wintypes.DWORD], wintypes.BOOL)
    _bind(_advapi, "OpenProcessToken", [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)], wintypes.BOOL)
    _bind(_advapi, "GetTokenInformation", [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
          wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)], wintypes.BOOL)
    _bind(_advapi, "ConvertSidToStringSidW", [ctypes.c_void_p, ctypes.POINTER(wintypes.LPWSTR)], wintypes.BOOL)
    _bind(_advapi, "ConvertStringSecurityDescriptorToSecurityDescriptorW",
          [wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wintypes.DWORD)], wintypes.BOOL)
    _bind(_advapi, "GetSecurityDescriptorDacl", [ctypes.c_void_p, ctypes.POINTER(wintypes.BOOL),
          ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wintypes.BOOL)], wintypes.BOOL)
    _bind(_advapi, "SetNamedSecurityInfoW", [wintypes.LPWSTR, ctypes.c_int, wintypes.DWORD,
          ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p], wintypes.DWORD)
    _bind(_advapi, "SetSecurityInfo", [wintypes.HANDLE, ctypes.c_int, wintypes.DWORD,
          ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p], wintypes.DWORD)


def require_windows(operation: str) -> None:
    if os.name != "nt":
        raise RuntimeError(f"{operation} requires Windows")


def _winerror(code: int | None = None) -> OSError:
    return ctypes.WinError(ctypes.get_last_error() if code is None else code)


def _path(path: Path) -> str:
    """Use extended absolute paths so Unicode/spaces and long paths stay literal."""
    value = os.path.abspath(os.fspath(path))
    if value.startswith("\\\\?\\"):
        return value
    if value.startswith("\\\\"):
        return "\\\\?\\UNC\\" + value[2:]
    return "\\\\?\\" + value


@lru_cache(maxsize=1)
def current_user_sid() -> str:
    require_windows("private Windows files")
    token = wintypes.HANDLE()
    if not _advapi.OpenProcessToken(_kernel.GetCurrentProcess(), 0x0008, ctypes.byref(token)):
        raise _winerror()
    try:
        length = wintypes.DWORD()
        _advapi.GetTokenInformation(token, 1, None, 0, ctypes.byref(length))
        data = ctypes.create_string_buffer(length.value)
        if not _advapi.GetTokenInformation(token, 1, data, length, ctypes.byref(length)):
            raise _winerror()
        sid = ctypes.cast(data, ctypes.POINTER(ctypes.c_void_p))[0]
        text = wintypes.LPWSTR()
        if not _advapi.ConvertSidToStringSidW(sid, ctypes.byref(text)):
            raise _winerror()
        try:
            return text.value
        finally:
            _kernel.LocalFree(ctypes.cast(text, ctypes.c_void_p))
    finally:
        _kernel.CloseHandle(token)


@contextmanager
def _private_security() -> Iterator[tuple[_SecurityAttributes, ctypes.c_void_p]]:
    descriptor = ctypes.c_void_p()
    sddl = f"D:P(A;OICI;FA;;;SY)(A;OICI;FA;;;{current_user_sid()})"
    if not _advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW(sddl, 1, ctypes.byref(descriptor), None):
        raise _winerror()
    try:
        present, defaulted = wintypes.BOOL(), wintypes.BOOL()
        dacl = ctypes.c_void_p()
        if not _advapi.GetSecurityDescriptorDacl(descriptor, ctypes.byref(present), ctypes.byref(dacl), ctypes.byref(defaulted)):
            raise _winerror()
        yield _SecurityAttributes(ctypes.sizeof(_SecurityAttributes), descriptor, False), dacl
    finally:
        _kernel.LocalFree(descriptor)


def _require_acl_filesystem(path: Path) -> None:
    volume = ctypes.create_unicode_buffer(32768)
    if not _kernel.GetVolumePathNameW(_path(path), volume, len(volume)):
        raise _winerror()
    flags = wintypes.DWORD()
    if not _kernel.GetVolumeInformationW(volume, None, 0, None, None, ctypes.byref(flags), None, 0):
        raise _winerror()
    if not flags.value & 0x00000008:  # FILE_PERSISTENT_ACLS
        raise OSError("LTC private state requires a filesystem with persistent Windows ACLs")


def secure_file(path: Path) -> None:
    """Protect an existing file/directory from inherited, broader permissions."""
    require_windows("private Windows files")
    path = Path(path)
    _require_acl_filesystem(path)
    with _private_security() as (_, dacl):
        error = _advapi.SetNamedSecurityInfoW(_path(path), SE_FILE_OBJECT,
                    DACL_SECURITY_INFORMATION | PROTECTED_DACL_SECURITY_INFORMATION,
                    None, None, dacl, None)
        if error:
            raise _winerror(error)


def ensure_private_directory(path: Path) -> None:
    """Create every missing component privately; secure the requested directory."""
    require_windows("private Windows directories")
    path = Path(path).absolute()
    try:
        info = path.lstat()
    except FileNotFoundError:
        if not path.parent.is_dir():
            ensure_private_directory(path.parent)
        _require_acl_filesystem(path.parent)
        with _private_security() as (attributes, _):
            if _kernel.CreateDirectoryW(_path(path), ctypes.byref(attributes)):
                return
            error = ctypes.get_last_error()
            if error != 183:  # ERROR_ALREADY_EXISTS: inspect concurrent creation.
                raise _winerror(error)
        info = path.lstat()
    if not stat.S_ISDIR(info.st_mode):
        raise NotADirectoryError(str(path))
    if getattr(info, "st_file_attributes", 0) & 0x400:  # FILE_ATTRIBUTE_REPARSE_POINT
        raise OSError("Private state directory cannot be a Windows reparse point")
    secure_file(path)


def _file_from_handle(handle: int, *, writable_only: bool = False) -> TextIO:
    flags = (os.O_WRONLY if writable_only else os.O_RDWR) | os.O_BINARY | os.O_NOINHERIT
    try:
        descriptor = msvcrt.open_osfhandle(handle, flags)
    except BaseException:
        _kernel.CloseHandle(handle)
        raise
    try:
        os.set_inheritable(descriptor, False)
        return os.fdopen(descriptor, "w" if writable_only else "r+", encoding="utf-8", newline="\n")
    except BaseException:
        os.close(descriptor)
        raise


def open_private_text(path: Path) -> TextIO:
    """Exclusively create a UTF-8 file with private ACL and write-through data."""
    require_windows("private Windows files")
    path = Path(path)
    ensure_private_directory(path.parent)
    with _private_security() as (attributes, _):
        handle = _kernel.CreateFileW(_path(path), GENERIC_WRITE, 0, ctypes.byref(attributes),
                    CREATE_NEW, FILE_ATTRIBUTE_NORMAL | FILE_FLAG_WRITE_THROUGH, None)
        error = ctypes.get_last_error() if handle == INVALID_HANDLE_VALUE else 0
    if handle == INVALID_HANDLE_VALUE:
        raise _winerror(error)
    return _file_from_handle(handle, writable_only=True)


def replace_file(source: Path, destination: Path) -> None:
    """Replace within a volume without copy fallback; request write-through.

    Open readers that deny FILE_SHARE_DELETE can cause a sharing violation.
    Failure is reported to the caller and does not remove the old destination.
    """
    require_windows("Windows atomic publication")
    if not _kernel.MoveFileExW(_path(source), _path(destination), 0x1 | 0x8):
        raise _winerror()


def fsync_directory(path: Path) -> None:
    """Compatibility hook: validate the directory, WITHOUT a durability claim.

    Windows does not offer a supported directory FlushFileBuffers equivalent.
    File data is flushed by storage before a write-through replacement. Namespace
    persistence across sudden power loss (including deletes) remains unguaranteed.
    """
    require_windows("Windows publication")
    info = Path(path).stat()
    if not stat.S_ISDIR(info.st_mode):
        raise NotADirectoryError(str(path))


def acquire_path_lock(path: Path, *, blocking: bool) -> tuple[TextIO, Path] | None:
    """Hold share=0 exclusion until *all* copies of this file handle close.

    The default handle is not inheritable. A delivery launcher must duplicate
    it into its explicit STARTUPINFO handle_list; closing the parent's copy
    then leaves exclusion owned by the worker. Never unlink a lock file.
    """
    require_windows("Windows callback ownership")
    path = Path(path)
    ensure_private_directory(path.parent)
    with _private_security() as (attributes, dacl):
        while True:
            handle = _kernel.CreateFileW(_path(path), GENERIC_READ | GENERIC_WRITE | WRITE_DAC,
                         0, ctypes.byref(attributes), OPEN_ALWAYS, FILE_ATTRIBUTE_NORMAL, None)
            if handle != INVALID_HANDLE_VALUE:
                break
            error = ctypes.get_last_error()
            if error != ERROR_SHARING_VIOLATION:
                raise _winerror(error)
            if not blocking:
                return None
            time.sleep(0.05)
        error = _advapi.SetSecurityInfo(handle, SE_FILE_OBJECT,
                    DACL_SECURITY_INFORMATION | PROTECTED_DACL_SECURITY_INFORMATION,
                    None, None, dacl, None)
        if error:
            _kernel.CloseHandle(handle)
            raise _winerror(error)
    return _file_from_handle(handle), path


def probe_existing_lock(path: Path) -> bool:
    """Observe contention without creating a missing file or changing its ACL.

    Missing and unlocked return False; a sharing violation returns True. Other
    errors propagate, so access failures cannot become evidence of a dead owner.
    """
    require_windows("Windows callback ownership")
    handle = _kernel.CreateFileW(_path(path), GENERIC_READ | GENERIC_WRITE, 0, None,
                                 OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, None)
    if handle == INVALID_HANDLE_VALUE:
        error = ctypes.get_last_error()
        if error in (2, 3):  # ERROR_FILE_NOT_FOUND / ERROR_PATH_NOT_FOUND
            return False
        if error == ERROR_SHARING_VIOLATION:
            return True
        raise _winerror(error)
    _kernel.CloseHandle(handle)
    return False
