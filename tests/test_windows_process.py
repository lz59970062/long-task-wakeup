"""Shell-free Windows launches, worker-owned Jobs and inherited file leases."""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from long_task_callback.platforms import windows_process as processes


NPM_PREAMBLE = """@ECHO off
GOTO start
:find_dp0
SET dp0=%~dp0
EXIT /b
:start
SETLOCAL
CALL :find_dp0
"""
NPM_NODE = NPM_PREAMBLE + r'''
IF EXIST "%dp0%\node.exe" (
  SET "_prog=%dp0%\node.exe"
) ELSE (
  SET "_prog=node"
  SET PATHEXT=%PATHEXT:;.JS;=;%
)

endLocal & goto #_undefined_# 2>NUL || title %COMSPEC% & "%_prog%"  "%dp0%\node_modules\@example\agent\main.js" %*
'''


class CommandFormattingTests(unittest.TestCase):
    def test_powershell_arguments_are_literal_and_single_quotes_double(self):
        self.assertEqual(
            processes.format_command(["C:\\Program Files\\ltc.exe", "a'b", '$x; &(whoami)', "中文", ""]),
            "& 'C:\\Program Files\\ltc.exe' 'a''b' '$x; &(whoami)' '中文' ''",
        )

    def test_posix_command_preparation_is_unchanged(self):
        with mock.patch.object(processes.os, "name", "posix"):
            self.assertEqual(processes.prepare_command(["/bin/echo", "hello & world"]), ["/bin/echo", "hello & world"])
            self.assertEqual(processes.background_popen_kwargs(), {})

    def test_empty_or_nul_command_rejected(self):
        for command in ([], ["x\x00y"], ["x", "bad\x00argument"]):
            with self.assertRaises(ValueError):
                processes.prepare_command(command)


@unittest.skipUnless(os.name == "nt", "native Windows process contracts")
class WindowsCommandTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="ltc process 中文 ")
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name).resolve()

    def test_native_executable_preserves_literal_arguments(self):
        arguments = ["& echo injected", 'a"b', "%PATH%", "!x!", "中文", "", "C:\\trailing\\"]
        command = processes.prepare_command([sys.executable, "-c", "import json,sys; print(json.dumps(sys.argv[1:]))", *arguments])
        result = subprocess.run(command, capture_output=True, text=True, check=True, **processes.background_popen_kwargs())
        self.assertEqual(json.loads(result.stdout), arguments)

    def test_npm_node_template_resolves_script_without_shell(self):
        shim = self.directory / "agent.cmd"
        script = self.directory / "node_modules" / "@example" / "agent" / "main.js"
        script.parent.mkdir(parents=True)
        script.write_text("", encoding="utf-8")
        node = self.directory / "node.exe"
        node.touch()
        shim.write_text(NPM_NODE, encoding="utf-8")
        self.assertEqual(processes.prepare_command([str(shim), 'a&"b']), [str(node), str(script), 'a&"b'])

    def test_npm_native_template_resolves_claude_style_binary(self):
        shim = self.directory / "agent.cmd"
        executable = self.directory / "node_modules" / "agent" / "bin" / "agent.exe"
        executable.parent.mkdir(parents=True)
        executable.touch()
        shim.write_text(NPM_PREAMBLE + '"%dp0%\\node_modules\\agent\\bin\\agent.exe"   %*\n', encoding="utf-8")
        self.assertEqual(processes.prepare_command([str(shim), "x|y"]), [str(executable), "x|y"])

    def test_forwarding_wrapper_updates_only_child_environment(self):
        shim = self.directory / "agent.bat"
        shim.write_text('@echo off\nset LTC_TEST_VALUE=child-value\n"' + sys.executable + '" %*\n', encoding="utf-8")
        environment = dict(os.environ)
        original = environment.get("LTC_TEST_VALUE")
        command = processes.prepare_command([str(shim), "-c", "import os; print(os.environ['LTC_TEST_VALUE'])"], env=environment)
        result = subprocess.run(command, env=environment, capture_output=True, text=True, check=True, **processes.background_popen_kwargs())
        self.assertEqual(result.stdout.strip(), "child-value")
        self.assertEqual(os.environ.get("LTC_TEST_VALUE"), original)

    def test_forwarding_wrapper_without_env_uses_private_launcher(self):
        shim = self.directory / "agent.bat"
        shim.write_text('@echo off\nset LTC_TEST_VALUE=private-value\n"' + sys.executable + '" %*\n', encoding="utf-8")
        argument = '%PATH% & " literal'
        command = processes.prepare_command([str(shim), "-c", "import os,json,sys;print(json.dumps([os.environ['LTC_TEST_VALUE'],sys.argv[1]]))", argument])
        self.assertNotIn("private-value", " ".join(command))
        result = subprocess.run(command, capture_output=True, text=True, check=True, **processes.background_popen_kwargs())
        self.assertEqual(json.loads(result.stdout), ["private-value", argument])

    def test_extensionless_absolute_npm_target_uses_windows_shim(self):
        target = self.directory / "agent"
        target.write_text("#!/bin/sh\n", encoding="utf-8")
        target.with_suffix(".cmd").write_text('"' + sys.executable + '" %*', encoding="utf-8")
        self.assertEqual(processes.prepare_command([str(target), "--version"]), [sys.executable, "--version"])

    def test_fixed_quoted_arguments_and_exit_status_forward_without_shell(self):
        script = self.directory / "child 中文 & quote'.py"
        script.write_text("import json,sys; print(json.dumps(sys.argv[1:])); raise SystemExit(7)", encoding="utf-8")
        shim = self.directory / "agent.cmd"
        shim.write_text('@echo off\n"' + sys.executable + '" "' + str(script) + '" "fixed & literal" "" %*\nexit /b %errorlevel%\n', encoding="utf-8")
        command = processes.prepare_command([str(shim), 'user" & %PATH%'])
        result = subprocess.run(command, capture_output=True, text=True, **processes.background_popen_kwargs())
        self.assertEqual(result.returncode, 7)
        self.assertEqual(json.loads(result.stdout), ["fixed & literal", "", 'user" & %PATH%'])

    def test_fixed_parameter_expansion_and_shell_operators_are_rejected(self):
        shim = self.directory / "agent.cmd"
        for prefix in ('"%PATH%"', '"!VALUE!"', '"safe" & echo injected', '"one""two"'):
            shim.write_text('"' + sys.executable + '" ' + prefix + ' %*\n', encoding="utf-8")
            with self.assertRaises(ValueError):
                processes.prepare_command([str(shim)])

    def test_arbitrary_batch_commands_are_rejected(self):
        shim = self.directory / "unsafe.cmd"
        for content in ["@echo off\necho %*", NPM_NODE + "\necho extra-command", '@echo off\nset A=x & echo bad\n"' + sys.executable + '" %*']:
            shim.write_text(content, encoding="utf-8")
            with self.assertRaises((ValueError, FileNotFoundError)):
                processes.prepare_command([str(shim), "argument"])

    def test_recursive_wrapper_rejected(self):
        shim = self.directory / "recursive.cmd"
        shim.write_text('"' + str(shim) + '" %*', encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "Recursive"):
            processes.prepare_command([str(shim)])

    def test_powershell_script_rejected(self):
        shim = self.directory / "agent.ps1"
        shim.write_text("echo $args", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "Unsupported Windows executable"):
            processes.prepare_command([str(shim)])


@unittest.skipUnless(os.name == "nt", "native Windows Jobs and handle inheritance")
class WindowsProcessTests(unittest.TestCase):
    def _environment(self):
        environment = dict(os.environ)
        environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
        return environment

    def test_runner_death_terminates_children_and_grandchildren(self):
        leaf = "import time; time.sleep(60)"
        child = (
            "import json,os,subprocess,sys,time; "
            f"leaf=subprocess.Popen([sys.executable,'-c',{leaf!r}]); "
            "print(json.dumps([os.getpid(),leaf.pid]),flush=True); time.sleep(60)"
        )
        runner = (
            "from long_task_callback.platforms.windows_process import enter_worker_job; "
            "import os,subprocess,sys,json,time; "
            "job=enter_worker_job(); again=enter_worker_job(); "
            f"child=subprocess.Popen([sys.executable,'-c',{child!r}],stdout=subprocess.PIPE,text=True); "
            "print(json.dumps({'pids':json.loads(child.stdout.readline()),"
            "'inheritable':os.get_handle_inheritable(job),'same':job==again}),flush=True); time.sleep(60)"
        )
        process = subprocess.Popen([sys.executable, "-c", runner], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                   env=self._environment(), **processes.background_popen_kwargs())
        api = ctypes.WinDLL("kernel32", use_last_error=True)
        api.OpenProcess.argtypes, api.OpenProcess.restype = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD], wintypes.HANDLE
        api.WaitForSingleObject.argtypes, api.WaitForSingleObject.restype = [wintypes.HANDLE, wintypes.DWORD], wintypes.DWORD
        api.CloseHandle.argtypes = [wintypes.HANDLE]
        handles = []
        try:
            line = process.stdout.readline()
            if not line:
                self.fail("worker failed to enter its Job: " + process.stderr.read())
            result = json.loads(line)
            self.assertFalse(result["inheritable"])
            self.assertTrue(result["same"])
            for pid in result["pids"]:
                handle = api.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE
                self.assertTrue(handle)
                handles.append(handle)
                self.assertEqual(api.WaitForSingleObject(handle, 0), 258)  # WAIT_TIMEOUT
            process.kill()
            process.wait(timeout=5)
            for handle in handles:
                self.assertEqual(api.WaitForSingleObject(handle, 5000), 0)
        finally:
            if process.poll() is None:
                process.kill()
            process.communicate(timeout=5)
            for handle in handles:
                api.CloseHandle(handle)

    def test_delivery_worker_retains_exclusive_lock_after_parent_close(self):
        from long_task_callback.platforms.windows_io import acquire_path_lock

        with tempfile.TemporaryDirectory(prefix="ltc transfer ") as temporary:
            path = Path(temporary) / "lease.lock"
            handle, _ = acquire_path_lock(path, blocking=False)
            program = (
                "from long_task_callback.platforms.windows_process import inherited_lock_fds; "
                "import json,os,sys; fds=inherited_lock_fds(); "
                "print(json.dumps([os.get_inheritable(fd) for fd in fds]),flush=True); sys.stdin.read(); "
                "[os.close(fd) for fd in fds]"
            )
            process = processes.spawn_delivery_worker([sys.executable, "-c", program], [handle.fileno()], env=self._environment(),
                                                       stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                self.assertEqual(json.loads(process.stdout.readline()), [False])
                self.assertFalse(os.get_inheritable(handle.fileno()))
                handle.close()
                self.assertIsNone(acquire_path_lock(path, blocking=False))
                process.communicate("", timeout=5)
                self.assertEqual(process.returncode, 0)
                reacquired = acquire_path_lock(path, blocking=False)
                self.assertIsNotNone(reacquired)
                reacquired[0].close()
            finally:
                handle.close()
                if process.poll() is None:
                    process.kill()
                process.communicate(timeout=5)

    def test_invalid_inherited_handle_list_is_rejected(self):
        for value in ("null", "[]", "[true]", "[-1]", "[8,8]", '"8"'):
            with mock.patch.dict(os.environ, {processes.LOCK_HANDLES_ENV: value}):
                with self.assertRaises(ValueError):
                    processes.inherited_lock_fds()

    def test_graceful_job_drain_waits_nested_children_and_does_not_kill_foreign_pid(self):
        from long_task_callback.platforms import windows

        outsider = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"],
                                    **processes.background_popen_kwargs())
        try:
            with tempfile.TemporaryDirectory(prefix="ltc-job-drain-") as temporary:
                folder = Path(temporary)
                program = folder / "runner.py"
                program.write_text('''
from long_task_callback.platforms import windows_process as p, windows
from long_task_callback.platforms.windows_io import open_private_text
import json, os, pathlib, subprocess, sys, time
directory=pathlib.Path(sys.argv[1]); outsider=int(sys.argv[2])
job=p.enter_worker_job()
marker=directory/'children.json'
child_code="""
from long_task_callback.platforms.windows_process import enter_worker_job
import json,os,pathlib,subprocess,sys,time
enter_worker_job()
leaf=subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)'])
pathlib.Path(sys.argv[1]).write_text(json.dumps([os.getpid(),leaf.pid]))
time.sleep(30)
"""
log_path=directory/'inherited.log'
log=open_private_text(log_path)
child=subprocess.Popen([sys.executable,'-c',child_code,str(marker)],stdout=log,stderr=log)
deadline=time.monotonic()+5
while not marker.exists():
    if time.monotonic()>deadline: raise RuntimeError('child readiness timeout')
    time.sleep(.01)
pids=json.loads(marker.read_text())
log.close()
query=p._job_process_ids
first=True
def include_recycled_foreign_pid(api,handle):
    global first
    members=query(api,handle)
    if first:
        first=False
        members.add(outsider)
    return members
p._job_process_ids=include_recycled_foreign_pid
p.terminate_worker_children(job,timeout=4)
with log_path.open('rb'): pass
log_path.unlink()
print(json.dumps({'children_stopped':all(not windows.pid_is_running(pid) for pid in pids),
                  'foreign_alive':windows.pid_is_running(outsider),
                  'only_self':query(p._kernel32(),job)=={os.getpid()},
                  'log_unlocked':not log_path.exists()}),flush=True)
''', encoding="utf-8")
                result = subprocess.run([sys.executable, str(program), str(folder), str(outsider.pid)],
                                        env=self._environment(), capture_output=True, text=True, timeout=8,
                                        **processes.background_popen_kwargs())
                self.assertEqual(0, result.returncode, result.stderr)
                self.assertEqual({"children_stopped": True, "foreign_alive": True,
                                  "only_self": True, "log_unlocked": True}, json.loads(result.stdout))
                self.assertIsNone(outsider.poll())
        finally:
            outsider.terminate()
            outsider.wait(timeout=5)

    def test_job_drain_rejects_unowned_handles(self):
        with mock.patch.object(processes, "_WORKER_JOB_HANDLE", 42):
            for value in (None, 0, 41, True):
                with self.assertRaises(ValueError):
                    processes.terminate_worker_children(value)

    def test_job_query_retries_successful_partial_lists_and_validates_length(self):
        api = mock.Mock()
        rounds = []
        def query(_job, _kind, data, size, returned):
            rounds.append(size)
            pids = [os.getpid()] if len(rounds) == 1 else [os.getpid(), 4242]
            wintypes.DWORD.from_buffer(data, 0).value = 2
            wintypes.DWORD.from_buffer(data, 4).value = len(pids)
            for index, pid in enumerate(pids):
                ctypes.c_size_t.from_buffer(data, 8 + index * ctypes.sizeof(ctypes.c_size_t)).value = pid
            ctypes.cast(returned, ctypes.POINTER(wintypes.DWORD)).contents.value = 8 + len(pids) * ctypes.sizeof(ctypes.c_size_t)
            return True
        api.QueryInformationJobObject.side_effect = query
        self.assertEqual({os.getpid(), 4242}, processes._job_process_ids(api, 42))
        self.assertEqual(2, len(rounds))
        self.assertGreater(rounds[1], rounds[0])
        api.QueryInformationJobObject.side_effect = lambda *_args: True
        with self.assertRaisesRegex(RuntimeError, "Invalid worker Job process list"):
            processes._job_process_ids(api, 42)

    def test_job_drain_does_not_accept_a_list_missing_its_owner(self):
        api = mock.Mock()
        def member(_process, _job, output):
            ctypes.cast(output, ctypes.POINTER(wintypes.BOOL)).contents.value = True
            return True
        api.IsProcessInJob.side_effect = member
        with mock.patch.object(processes, "_WORKER_JOB_HANDLE", 42), mock.patch.object(processes, "_kernel32", return_value=api), mock.patch.object(processes, "_job_process_ids", return_value=set()):
            with self.assertRaisesRegex(RuntimeError, "no longer reports its owner"):
                processes.terminate_worker_children(42)
        api.OpenProcess.assert_not_called()

    def test_job_drain_waits_for_already_terminating_native_handle_or_times_out(self):
        def member(_process, _job, output):
            ctypes.cast(output, ctypes.POINTER(wintypes.BOOL)).contents.value = True
            return True
        for wait_status in (0, 258):
            with self.subTest(wait_status=wait_status):
                api = mock.Mock()
                api.IsProcessInJob.side_effect = member
                api.OpenProcess.return_value = 123
                api.TerminateProcess.return_value = False
                api.WaitForSingleObject.return_value = wait_status
                with mock.patch.object(processes, "_WORKER_JOB_HANDLE", 42), mock.patch.object(processes, "_kernel32", return_value=api), mock.patch.object(processes.ctypes, "get_last_error", return_value=5), mock.patch.object(processes, "_job_process_ids", side_effect=[{os.getpid(), 4242}, {os.getpid()}]):
                    if wait_status == 0:
                        processes.terminate_worker_children(42)
                    else:
                        with self.assertRaises(TimeoutError):
                            processes.terminate_worker_children(42)
                self.assertEqual(123, api.WaitForSingleObject.call_args.args[0])
                self.assertGreater(api.WaitForSingleObject.call_args.args[1], 0)
                api.CloseHandle.assert_called_once_with(123)


if __name__ == "__main__":
    unittest.main()
