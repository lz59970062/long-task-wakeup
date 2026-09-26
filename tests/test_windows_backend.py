import contextlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock
import uuid
import xml.etree.ElementTree as ET

from long_task_callback.platforms import LaunchError, OwnerState
from long_task_callback.platforms import windows


class WindowsBackendContracts(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="ltc windows ")
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name).resolve()
        self.backend = windows.WindowsBackend()
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(mock.patch.object(sys, "platform", "win32"))

    def test_owner_names_bind_queue_attempt_and_task(self):
        owner = self.backend.owner_name("abc123", 1, self.directory)
        self.assertEqual(owner, self.backend.owner_name("abc123", 1, self.directory))
        self.assertEqual(len({owner, self.backend.owner_name("abc123", 2, self.directory),
                              self.backend.owner_name("def456", 1, self.directory),
                              self.backend.owner_name("abc123", 1, self.directory / "other")}), 4)
        for task, attempt in (("../abc", 1), ("abc", 0), ("abc", True)):
            with self.subTest(task=task, attempt=attempt), self.assertRaises(ValueError):
                self.backend.owner_name(task, attempt, self.directory)

    def test_launch_uses_create_only_scheduler_request_with_literal_arguments(self):
        argv = [sys.executable, str(self.directory / "entry script.py"), "_task-worker", "--task-file",
                str(self.directory / "中文 & $value; files" / "task.json"), "--attempt", "1"]
        with mock.patch.object(windows, "scheduler_call", return_value={"submitted": True}) as control:
            self.backend.launch("ltc.test", argv, self.directory, self.directory / "worker.log")
        control.assert_called_once()
        operation, payload = control.call_args.args
        self.assertEqual(operation, "launch")
        self.assertEqual(payload["owner"], "ltc.test")
        config = payload["configuration"]
        self.assertEqual(config["arguments"], subprocess.list2cmdline(argv[1:]))
        self.assertEqual(config["executable"], argv[0])
        self.assertFalse(config["login"])
        self.assertTrue(config["enabled"])
        self.assertEqual(set(config), {"executable", "arguments", "cwd", "login", "enabled"})

    def test_failures_after_submission_preserve_uncertainty(self):
        for error, uncertain in ((subprocess.TimeoutExpired("powershell", 15), True),
                                 (windows.SchedulerError("RPC failed"), True),
                                 (FileNotFoundError("powershell"), False)):
            with self.subTest(error=error), mock.patch.object(windows, "scheduler_call", side_effect=error), self.assertRaises(LaunchError) as caught:
                self.backend.launch("ltc.test", [sys.executable], self.directory, self.directory / "log")
            self.assertEqual(caught.exception.uncertain, uncertain)
        with mock.patch.object(windows, "scheduler_call") as control, self.assertRaises(LaunchError) as caught:
            self.backend.launch("../invalid", [sys.executable], self.directory, self.directory / "log")
        self.assertFalse(caught.exception.uncertain)
        control.assert_not_called()

    def test_probe_keeps_unknown_for_unrun_or_unreadable_owners(self):
        samples = [
            ({"registered": False}, OwnerState.ABSENT),
            ({"registered": True, "state": 4, "instances": [{"pid": 123}]}, OwnerState.ALIVE),
            ({"registered": True, "state": 2, "instances": []}, OwnerState.ALIVE),
            ({"registered": True, "state": 3, "instances": [], "has_run": True}, OwnerState.ABSENT),
            ({"registered": True, "state": 1, "instances": [], "has_run": True}, OwnerState.ABSENT),
            ({"registered": True, "state": 3, "instances": [], "has_run": False}, OwnerState.UNKNOWN),
            ({"registered": True, "state": 0, "instances": [], "has_run": True}, OwnerState.UNKNOWN),
            ({"registered": True, "state": True, "instances": []}, OwnerState.UNKNOWN),
            ({"registered": True, "state": 4}, OwnerState.UNKNOWN),
            ({}, OwnerState.UNKNOWN),
        ]
        for data, expected in samples:
            with self.subTest(data=data), mock.patch.object(windows, "scheduler_call", return_value=data):
                self.assertEqual(self.backend.probe("ltc.test"), expected)
        for error in (windows.SchedulerError("denied"), subprocess.TimeoutExpired("powershell", 15), OSError("RPC")):
            with self.subTest(error=error), mock.patch.object(windows, "scheduler_call", side_effect=error):
                self.assertEqual(self.backend.probe("ltc.test"), OwnerState.UNKNOWN)

    def test_collection_rechecks_without_stopping_live_workers(self):
        for state in (OwnerState.ALIVE, OwnerState.UNKNOWN):
            with mock.patch.object(windows, "job_status", return_value=(state, None, True)), mock.patch.object(windows, "scheduler_call") as control:
                self.assertFalse(self.backend.collect("ltc.test"))
                control.assert_not_called()
        with mock.patch.object(windows, "job_status", return_value=(OwnerState.ABSENT, None, True)), mock.patch.object(
            windows, "scheduler_call", return_value={"collected": False}
        ) as control:
            self.assertFalse(self.backend.collect("ltc.test"))
            control.assert_called_once_with("collect", {"owner": "ltc.test"})

    def test_worker_admission_requires_scheduler_pid_and_stable_live_identity(self):
        identity = {"creation_time": 10, "boot_id": "boot", "sid": "sid"}
        for status, identities, expected in (
            ((OwnerState.ALIVE, os.getpid(), True), [identity, identity], True),
            ((OwnerState.ALIVE, os.getpid() + 1, True), [identity], False),
            ((OwnerState.UNKNOWN, os.getpid(), True), [identity], False),
            ((OwnerState.ALIVE, os.getpid(), True), [None], False),
            ((OwnerState.ALIVE, os.getpid(), True), [identity, dict(identity, creation_time=11)], False),
        ):
            with self.subTest(status=status, identities=identities), mock.patch.object(windows, "job_status", return_value=status), mock.patch.object(
                windows, "process_identity", side_effect=identities
            ):
                self.assertEqual(self.backend.admits_worker("ltc.test"), expected)

    def test_venv_admission_rejects_unrelated_or_reused_parent_processes(self):
        child_id = {"creation_time": 20, "boot_id": "boot", "machine_id": "host", "sid": "user"}
        parent_id = dict(child_id, creation_time=10)
        parent_image = os.path.normcase(str(self.directory / "venv" / "python.exe"))
        child_image = os.path.normcase(str(self.directory / "base" / "python.exe"))
        for change, expected in (({}, True), ({"image": child_image}, False),
                                 ({"executable": child_image}, False),
                                 ({"identity": dict(parent_id, sid="other")}, False),
                                 ({"identity": dict(parent_id, creation_time=21)}, False),
                                 ({"reused": True}, False), ({"new_engine": 503}, False)):
            seen_parent = []

            def identity(pid):
                if pid == 501:
                    return child_id
                seen_parent.append(True)
                value = change.get("identity", parent_id)
                return dict(value, creation_time=11) if change.get("reused") and len(seen_parent) > 1 else value

            def image(pid):
                return change.get("image", parent_image) if pid == 502 else child_image

            with self.subTest(change=change), contextlib.ExitStack() as stack:
                for attribute, value in (("prefix", "venv"), ("base_prefix", "base"),
                                         ("executable", parent_image), ("_base_executable", child_image)):
                    stack.enter_context(mock.patch.object(sys, attribute, value, create=True))
                stack.enter_context(mock.patch.object(os, "getpid", return_value=501))
                stack.enter_context(mock.patch.object(os, "getppid", return_value=502))
                stack.enter_context(mock.patch.object(windows, "process_identity", side_effect=identity))
                stack.enter_context(mock.patch.object(windows, "_process_image", side_effect=image))
                stack.enter_context(mock.patch.object(windows, "scheduler_call", return_value={
                    "executable": change.get("executable", parent_image)}))
                stack.enter_context(mock.patch.object(windows, "job_status", side_effect=[
                    (OwnerState.ALIVE, 502, True), (OwnerState.ALIVE, change.get("new_engine", 502), True)]))
                self.assertEqual(self.backend.admits_worker("ltc.test"), expected)

    def test_scheduler_protocol_uses_json_stdin_and_hides_error_text(self):
        payload = {"owner": "ltc.test", "configuration": {"cwd": "中文 & literal"}}
        with mock.patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], 0, b'{"ok":true}', b"")) as run:
            self.assertEqual(windows.scheduler_call("register", payload), {"ok": True})
        self.assertEqual(json.loads(run.call_args.kwargs["input"]), dict(payload, operation="register"))
        self.assertNotIn("中文", " ".join(run.call_args.args[0]))
        self.assertFalse(run.call_args.kwargs.get("shell", False))
        with mock.patch.object(subprocess, "run", return_value=subprocess.CompletedProcess(
            [], 1, b'{"ok":false,"hresult":-5,"error":"SECRET"}', b"SECRET"
        )), self.assertRaises(windows.SchedulerError) as caught:
            windows.scheduler_call("status", {"owner": "ltc.test"})
        self.assertNotIn("SECRET", str(caught.exception))

    def test_unavailable_platform_is_importable_and_fails_closed(self):
        with mock.patch.object(sys, "platform", "linux"):
            self.assertFalse(self.backend.available())
            self.assertFalse(self.backend.admits_worker("ltc.test"))
            self.assertEqual(self.backend.probe("ltc.test"), OwnerState.UNKNOWN)
            self.assertIsNone(windows.current_boot_id())
            self.assertIsNone(windows.process_identity(os.getpid()))

    def test_boot_identity_uses_boot_counter_and_keeps_missing_information_unknown(self):
        registry = mock.MagicMock(REG_DWORD=4, KEY_READ=1, KEY_WOW64_64KEY=256)
        with mock.patch.dict(sys.modules, {"winreg": registry}):
            for value, kind, expected in ((14, 4, "windows-boot-14"), (15, 4, "windows-boot-15"),
                                           ("14", 1, None), (-1, 4, None)):
                registry.QueryValueEx.return_value = (value, kind)
                self.assertEqual(windows.current_boot_id(), expected)
            registry.QueryValueEx.side_effect = PermissionError("unavailable")
            self.assertIsNone(windows.current_boot_id())


@unittest.skipUnless(sys.platform == "win32", "requires native Windows process interfaces")
class NativeWindowsIdentityTests(unittest.TestCase):
    def test_process_and_boot_identity_are_stable_without_signals(self):
        with mock.patch.object(os, "kill", side_effect=AssertionError("must not signal a Windows PID")):
            identity = windows.process_identity(os.getpid())
            self.assertIsNotNone(identity)
            self.assertEqual(identity, windows.process_identity(os.getpid()))
            self.assertEqual(identity["boot_id"], windows.current_boot_id())
            self.assertEqual(identity["machine_id"], windows.current_machine_id())
            self.assertGreater(identity["creation_time"], 0)
            self.assertTrue(identity["sid"].startswith("S-1-"))
            self.assertTrue(windows.pid_is_running(os.getpid()))
            self.assertIsNone(windows.process_identity(-1))
            self.assertIsNone(windows.process_identity(True))
            self.assertFalse(windows.pid_is_running(-1))

    def test_exited_child_is_absent_even_when_retained_process_handle_exists(self):
        process = subprocess.Popen([sys.executable, "-c", "pass"])
        process.wait(timeout=10)
        self.assertFalse(windows.pid_is_running(process.pid))
        self.assertIsNone(windows.process_identity(process.pid))


@unittest.skipUnless(sys.platform == "win32" and os.environ.get("LTC_TEST_WINDOWS_SCHEDULER") == "1",
                     "opt-in isolated native Task Scheduler test")
class NativeWindowsSchedulerTests(unittest.TestCase):
    def test_independent_owner_admission_arguments_policy_and_collection(self):
        backend = windows.WindowsBackend()
        owner = "ltc-test-" + uuid.uuid4().hex
        with tempfile.TemporaryDirectory(prefix="ltc task 中文 & ") as temporary:
            directory = Path(temporary).resolve()
            output = directory / "process.json"
            script = directory / "worker.py"
            script.write_text(
                "import json,os,sys,time\n"
                f"sys.path.insert(0, {str(Path(windows.__file__).resolve().parents[2])!r})\n"
                "from long_task_callback.platforms import windows\n"
                "from pathlib import Path\n"
                "Path(sys.argv[1]).write_text(json.dumps({'pid':os.getpid(),'ppid':os.getppid(),"
                "'admitted':windows.WindowsBackend().admits_worker(sys.argv[2]),"
                "'identity':windows.process_identity(os.getpid())}),encoding='utf-8')\n"
                "time.sleep(6)\n", encoding="utf-8")
            launched = False
            try:
                backend.launch(owner, [sys.executable, str(script), str(output), owner], directory,
                               directory / "worker.log")
                launched = True
                definition = windows.scheduler_call("definition", {"owner": owner})
                document = ET.fromstring(definition["xml"])
                ns = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
                self.assertEqual(list(document.find("t:Triggers", ns)), [])
                self.assertEqual(definition["run_level"], 0)
                self.assertEqual(definition["logon_type"], 3)
                self.assertEqual(definition["multiple_instances"], 2)
                self.assertEqual(definition["execution_time_limit"], "PT0S")
                self.assertEqual(definition["restart_count"], 0)
                for name in ("disallow_batteries", "stop_batteries", "only_idle", "start_when_available"):
                    self.assertFalse(definition[name])
                self.assertIsNone(document.find("t:Settings/t:RestartOnFailure", ns))
                deadline = time.monotonic() + 12
                while not output.exists() and time.monotonic() < deadline:
                    time.sleep(0.1)
                self.assertTrue(output.exists(), windows.scheduler_call("status", {"owner": owner}))
                data = json.loads(output.read_text(encoding="utf-8"))
                state, pid, registered = windows.job_status(owner)
                self.assertEqual((state, registered), (OwnerState.ALIVE, True))
                expected_engine = data["ppid"] if sys.prefix != sys.base_prefix else data["pid"]
                self.assertEqual(pid, expected_engine)
                self.assertTrue(data["admitted"], data)
                self.assertEqual(data["identity"], windows.process_identity(data["pid"]))
                self.assertFalse(backend.collect(owner))
            finally:
                if launched:
                    deadline = time.monotonic() + 15
                    while backend.probe(owner) == OwnerState.ALIVE and time.monotonic() < deadline:
                        time.sleep(0.2)
                    self.assertTrue(backend.collect(owner), windows.scheduler_call("status", {"owner": owner}))
                    self.assertEqual(backend.probe(owner), OwnerState.ABSENT)


if __name__ == "__main__":
    unittest.main()
