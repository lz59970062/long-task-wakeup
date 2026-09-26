"""Guard the experimental package launcher without starting any GUI process."""
from __future__ import annotations

from contextlib import contextmanager
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock


SOURCE = Path(__file__).resolve().parents[1] / "examples" / "windows" / "desktop-package-launch.py"
_spec = importlib.util.spec_from_file_location("ltc_desktop_package_launch_tests", SOURCE)
launcher = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(launcher)


@contextmanager
def native_probe(callback):
    """Replace the imported probe before it can call any native process API."""
    module = SimpleNamespace(probe=callback)
    spec = SimpleNamespace(loader=SimpleNamespace(exec_module=lambda value: None))
    with mock.patch.object(launcher.importlib.util, "spec_from_file_location", return_value=spec), \
            mock.patch.object(launcher.importlib.util, "module_from_spec", return_value=module):
        yield


class DesktopPackageLauncherTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="ltc-package-launch-")
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name).resolve()
        self.executable = self.directory / "Desktop.exe"
        self.executable.write_bytes(b"MZ")
        self.request_file = self.directory / "request.json"
        self.result_file = self.directory / "result.json"
        self.overrides = {key: "test-only-value" for key in launcher.ENVIRONMENT_KEYS}
        self.overrides["CODEX_APP_SERVER_WS_URL"] = None

    def request(self, *, check_only=True):
        return {
            "expected_package_full_name": "Test.Package_1.0.0.0_x64__publisher",
            "desktop_executable": str(self.executable),
            "environment": dict(self.overrides),
            "check_only": check_only,
            "result_file": str(self.result_file),
        }

    def matching_package(self):
        return mock.patch.object(launcher, "current_package_full_name",
                                 return_value=self.request()["expected_package_full_name"])

    @staticmethod
    def successful_probe(_executable):
        return {"cleanup_complete": True, "can_create_suspended": True, "probe_resumed": False,
                "child_package_full_name": "Test.Package_1.0.0.0_x64__publisher",
                "child_package_query_status": 0}, 0

    def test_untrusted_request_cannot_escape_result_directory_or_clobber_files(self):
        request = self.request()
        self.request_file.write_text(json.dumps(request), encoding="utf-8")
        self.assertEqual(launcher.read_request(self.request_file)[1], self.result_file)
        self.result_file.write_text("existing-result", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "already exists"):
            launcher.read_request(self.request_file)
        self.assertEqual(self.result_file.read_text(), "existing-result")
        for destination in (self.directory.parent / "result.json", self.directory / "other.json",
                            Path("result.json"), self.request_file):
            with self.subTest(destination=destination):
                request["result_file"] = str(destination)
                self.request_file.write_text(json.dumps(request), encoding="utf-8")
                with self.assertRaises(ValueError):
                    launcher.read_request(self.request_file)

    def test_duplicate_keys_and_unapproved_environment_are_rejected(self):
        self.request_file.write_text('{"check_only":true,"check_only":false}', encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            launcher.read_request(self.request_file)
        with self.matching_package():
            changes = (
                lambda request: request.update(check_only=1),
                lambda request: request.update(extra="field"),
                lambda request: request["environment"].update(UNAPPROVED="private-value"),
                lambda request: request["environment"].update(CODEX_HOME="bad\0value"),
                lambda request: request["environment"].pop("CODEX_HOME"),
            )
            for change in changes:
                request = self.request()
                change(request)
                with self.assertRaises(ValueError):
                    launcher.validate_request(request)

    def test_wrong_package_cannot_probe_or_launch_or_change_environment(self):
        before = dict(os.environ)
        probe = mock.Mock(side_effect=self.successful_probe)
        with mock.patch.object(launcher, "current_package_full_name", return_value="Other.Package"), \
                mock.patch.object(launcher.subprocess, "Popen") as start, native_probe(probe):
            with self.assertRaisesRegex(RuntimeError, "does not match"):
                launcher.launch(self.request(check_only=False))
            probe.assert_not_called()
            start.assert_not_called()
        self.assertEqual(dict(os.environ), before)

    def test_check_only_applies_child_settings_and_never_launches_gui(self):
        before = dict(os.environ)

        def inspect_probe(executable):
            self.assertEqual(executable, str(self.executable))
            for key, value in self.overrides.items():
                self.assertEqual(os.environ.get(key), value)
            return self.successful_probe(executable)

        with self.matching_package(), mock.patch.object(launcher.subprocess, "Popen") as start, \
                mock.patch.object(launcher, "existing_desktop_processes") as process_scan, \
                native_probe(inspect_probe):
            result = launcher.launch(self.request())
            start.assert_not_called()
            process_scan.assert_not_called()
        self.assertTrue(result["ok"])
        self.assertNotIn("desktop_pid", result)
        self.assertNotIn("environment", result)
        self.assertEqual(dict(os.environ), before)

    def test_probe_exception_restores_removed_and_replaced_environment(self):
        with mock.patch.dict(os.environ, {"CODEX_APP_SERVER_WS_URL": "original-endpoint",
                                          "CODEX_HOME": "original-profile"}):
            before = dict(os.environ)

            def fail_probe(_executable):
                self.assertNotIn("CODEX_APP_SERVER_WS_URL", os.environ)
                self.assertEqual(os.environ["CODEX_HOME"], "test-only-value")
                raise OSError("test probe failure")

            with self.matching_package(), mock.patch.object(launcher.subprocess, "Popen") as start, \
                    native_probe(fail_probe):
                with self.assertRaisesRegex(OSError, "test probe failure"):
                    launcher.launch(self.request(check_only=False))
                start.assert_not_called()
            self.assertEqual(dict(os.environ), before)

    def test_failed_native_cleanup_or_creation_prevents_actual_start(self):
        for can_create, cleanup, code in ((False, True, 0), (True, False, 1)):
            with self.subTest(can_create=can_create, cleanup=cleanup):
                def probe(_executable):
                    return {"can_create_suspended": can_create, "cleanup_complete": cleanup}, code

                with self.matching_package(), mock.patch.object(launcher.subprocess, "Popen") as start, \
                        native_probe(probe):
                    result = launcher.launch(self.request(check_only=False))
                    self.assertFalse(result["ok"])
                    self.assertEqual(result["error_type"], "NativeLaunchProbeError")
                    start.assert_not_called()

    def test_existing_desktop_refusal_restores_environment_and_never_launches(self):
        before = dict(os.environ)
        with self.matching_package(), \
                mock.patch.object(launcher, "existing_desktop_processes", return_value=[123]) as scan, \
                mock.patch.object(launcher.subprocess, "Popen") as start, native_probe(self.successful_probe):
            with self.assertRaisesRegex(RuntimeError, "already running"):
                launcher.launch(self.request(check_only=False))
            scan.assert_called_once_with(self.executable)
            start.assert_not_called()
        self.assertEqual(dict(os.environ), before)

    def test_child_package_identity_must_be_verified_before_actual_start(self):
        for child_package, status in (("Other.Package", 0), (None, 15700),
                                      ("Test.Package_1.0.0.0_x64__publisher", 5), (None, None)):
            with self.subTest(child_package=child_package, status=status):
                def probe(executable):
                    result, code = self.successful_probe(executable)
                    result.update(child_package_full_name=child_package, child_package_query_status=status)
                    return result, code

                with self.matching_package(), mock.patch.object(launcher.subprocess, "Popen") as start, \
                        mock.patch.object(launcher, "existing_desktop_processes") as scan, native_probe(probe):
                    result = launcher.launch(self.request(check_only=False))
                    self.assertFalse(result["ok"])
                    self.assertEqual(result["error_type"], "PackageIdentityError")
                    self.assertIn("expected Windows package identity", result["message"])
                    self.assertTrue(result["native_launch_probe"]["cleanup_complete"])
                    start.assert_not_called()
                    scan.assert_not_called()

    def test_launch_uses_only_exact_executable_and_explicit_child_environment(self):
        with self.matching_package(), \
                mock.patch.object(launcher, "existing_desktop_processes", return_value=[]), \
                mock.patch.object(launcher.subprocess, "Popen", return_value=SimpleNamespace(pid=123)) as start, \
                native_probe(self.successful_probe):
            result = launcher.launch(self.request(check_only=False))
        self.assertEqual(result["desktop_pid"], 123)
        self.assertEqual(start.call_args.args, ([str(self.executable)],))
        self.assertEqual(start.call_args.kwargs["cwd"], str(self.directory))
        self.assertTrue(start.call_args.kwargs["close_fds"])
        self.assertNotIn("shell", start.call_args.kwargs)
        self.assertNotIn("CODEX_APP_SERVER_WS_URL", start.call_args.kwargs["env"])

    @unittest.skipUnless(os.name == "nt", "main intentionally rejects non-Windows hosts")
    def test_package_identity_error_is_reported_once_without_clobbering_result(self):
        self.request_file.write_text(json.dumps(self.request()), encoding="utf-8")
        with mock.patch.object(launcher, "current_package_full_name", return_value="Other.Package"), \
                mock.patch.object(launcher.subprocess, "Popen") as start:
            self.assertEqual(launcher.main(["--request", str(self.request_file)]), 1)
            start.assert_not_called()
        original = self.result_file.read_bytes()
        result = json.loads(original)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_type"], "RuntimeError")
        self.assertIn("does not match", result["message"])
        with mock.patch.object(launcher.sys, "stderr", io.StringIO()):
            self.assertEqual(launcher.main(["--request", str(self.request_file)]), 1)
        self.assertEqual(self.result_file.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
