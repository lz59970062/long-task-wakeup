"""Native Windows ACL/publication/lease checks using disposable short processes."""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from long_task_callback import storage
from long_task_callback.platforms import windows_io


@unittest.skipUnless(os.name == "nt", "native Windows file primitives")
class WindowsIOTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="ltc 中文 空格 ")
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)

    def acl(self, path: Path) -> str:
        advapi = ctypes.WinDLL("advapi32", use_last_error=True)
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        advapi.GetNamedSecurityInfoW.argtypes = [wintypes.LPWSTR, ctypes.c_int, wintypes.DWORD,
            ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_void_p)]
        advapi.GetNamedSecurityInfoW.restype = wintypes.DWORD
        advapi.ConvertSecurityDescriptorToStringSecurityDescriptorW.argtypes = [ctypes.c_void_p,
            wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(wintypes.LPWSTR), ctypes.c_void_p]
        advapi.ConvertSecurityDescriptorToStringSecurityDescriptorW.restype = wintypes.BOOL
        kernel.LocalFree.argtypes = [ctypes.c_void_p]
        kernel.LocalFree.restype = ctypes.c_void_p
        descriptor, text = ctypes.c_void_p(), wintypes.LPWSTR()
        error = advapi.GetNamedSecurityInfoW(str(path), 1, 4, None, None, None, None,
                                            ctypes.byref(descriptor))
        if error:
            raise ctypes.WinError(error)
        try:
            if not advapi.ConvertSecurityDescriptorToStringSecurityDescriptorW(
                    descriptor, 1, 4, ctypes.byref(text), None):
                raise ctypes.WinError(ctypes.get_last_error())
            return text.value
        finally:
            if text:
                kernel.LocalFree(ctypes.cast(text, ctypes.c_void_p))
            kernel.LocalFree(descriptor)

    def assert_private_acl(self, path: Path) -> None:
        sddl = self.acl(path)
        self.assertTrue(sddl.startswith("D:P"), sddl)
        self.assertEqual(sddl.count("("), 2, sddl)
        self.assertIn(";;;SY)", sddl)
        self.assertIn(f";;;{windows_io.current_user_sid()})", sddl)

    def start_child(self, code: str, *args: str, **kwargs) -> subprocess.Popen:
        environment = dict(os.environ)
        environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
        child = subprocess.Popen([sys.executable, "-u", "-c", code, *args],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, env=environment, **kwargs)
        self.addCleanup(self.stop_child, child)
        return child

    @staticmethod
    def stop_child(child: subprocess.Popen) -> None:
        if child.poll() is None:
            child.kill()
        child.communicate(timeout=10)

    def test_new_directories_and_files_are_private_before_content_is_written(self) -> None:
        path = self.directory / "新目录 空格" / "嵌套" / "environment.json"
        windows_io.ensure_private_directory(path.parent)
        self.assert_private_acl(path.parent)
        self.assert_private_acl(path.parent.parent)
        with windows_io.open_private_text(path) as handle:
            self.assert_private_acl(path)
            self.assertFalse(os.get_inheritable(handle.fileno()))
            handle.write('秘密 = "你好"\n')
            handle.flush()
            os.fsync(handle.fileno())
        self.assertEqual(path.read_text(encoding="utf-8"), '秘密 = "你好"\n')

    def test_existing_acl_is_replaced_with_only_user_and_system(self) -> None:
        path = self.directory / "existing.txt"
        path.write_text("old", encoding="utf-8")
        windows_io.secure_file(path)
        self.assert_private_acl(path)

    def test_private_creation_does_not_replace_existing_file(self) -> None:
        path = self.directory / "already.txt"
        path.write_text("old", encoding="utf-8")
        with self.assertRaises(FileExistsError):
            windows_io.open_private_text(path)
        self.assertEqual(path.read_text(encoding="utf-8"), "old")

    def test_atomic_private_publication_and_unicode_path(self) -> None:
        path = self.directory / "提示词 带空格" / "提示.json"
        storage.write_request(path, {"prompt": "你好 世界", "value": 1})
        storage.write_request(path, {"prompt": "新内容", "value": 2})
        self.assertIn('"prompt": "新内容"', path.read_text(encoding="utf-8"))
        self.assert_private_acl(path)
        self.assertEqual(list(path.parent.glob("*.tmp")), [])

    def test_failed_replace_preserves_old_value_and_removes_temporary(self) -> None:
        path = self.directory / "result.json"
        storage.write_private_text(path, "old")
        # A reader without FILE_SHARE_DELETE causes an actual Windows replace failure.
        with path.open("rb"):
            with self.assertRaises(OSError):
                storage.write_private_text(path, "new")
        self.assertEqual(path.read_text(encoding="utf-8"), "old")
        self.assertEqual(list(path.parent.glob("*.tmp")), [])

    def test_sync_hook_failure_can_follow_successful_publication(self) -> None:
        path = self.directory / "result.json"
        with self.assertRaisesRegex(OSError, "hook"):
            storage.write_private_text(path, "new", sync_directory=mock.Mock(side_effect=OSError("hook")))
        self.assertEqual(path.read_text(encoding="utf-8"), "new")

    def test_directory_hook_checks_path_without_claiming_directory_flush(self) -> None:
        windows_io.fsync_directory(self.directory)
        with self.assertRaises(FileNotFoundError):
            windows_io.fsync_directory(self.directory / "missing")

    def test_probe_missing_lock_never_creates_directory_or_file(self) -> None:
        path = self.directory / "does not exist" / "owner.lock"
        self.assertFalse(windows_io.probe_existing_lock(path))
        self.assertFalse(path.parent.exists())

    def test_same_process_contenders_are_exclusive_and_close_releases(self) -> None:
        path = self.directory / "租约 空格.lock"
        lock = windows_io.acquire_path_lock(path, blocking=False)
        self.assertIsNotNone(lock)
        try:
            self.assertTrue(windows_io.probe_existing_lock(path))
            self.assertIsNone(windows_io.acquire_path_lock(path, blocking=False))
        finally:
            lock[0].close()
        self.assertFalse(windows_io.probe_existing_lock(path))
        self.assertTrue(path.exists(), "The lock namespace must remain stable")
        self.assert_private_acl(path)

    def test_process_crash_releases_lock(self) -> None:
        path = self.directory / "child.lock"
        code = """
import sys
from pathlib import Path
from long_task_callback.platforms.windows_io import acquire_path_lock
lock = acquire_path_lock(Path(sys.argv[1]), blocking=False)
assert lock is not None
print('held', flush=True)
sys.stdin.read(1)
"""
        child = self.start_child(code, str(path))
        self.assertEqual(child.stdout.readline().strip(), "held")
        self.assertIsNone(windows_io.acquire_path_lock(path, blocking=False))
        child.kill()
        child.communicate(timeout=10)
        recovered = windows_io.acquire_path_lock(path, blocking=False)
        self.assertIsNotNone(recovered)
        recovered[0].close()

    def test_inherited_handle_keeps_lock_after_parent_copy_closes(self) -> None:
        import msvcrt
        path = self.directory / "inherited.lock"
        lock = windows_io.acquire_path_lock(path, blocking=False)
        self.assertIsNotNone(lock)
        native = msvcrt.get_osfhandle(lock[0].fileno())
        startup = subprocess.STARTUPINFO()
        startup.lpAttributeList = {"handle_list": [native]}
        code = """
import msvcrt, os, sys
descriptor = msvcrt.open_osfhandle(int(sys.argv[1]), os.O_RDWR)
print('inherited', flush=True)
sys.stdin.read(1)
os.close(descriptor)
"""
        try:
            os.set_handle_inheritable(native, True)
            child = self.start_child(code, str(native), startupinfo=startup, close_fds=True)
        finally:
            os.set_handle_inheritable(native, False)
            lock[0].close()
        self.assertEqual(child.stdout.readline().strip(), "inherited")
        self.assertTrue(windows_io.probe_existing_lock(path))
        self.assertIsNone(windows_io.acquire_path_lock(path, blocking=False))
        child.kill()
        child.communicate(timeout=10)
        self.assertFalse(windows_io.probe_existing_lock(path))


if __name__ == "__main__":
    unittest.main()
