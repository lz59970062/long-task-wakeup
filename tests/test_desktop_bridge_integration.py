"""Opt-in real Codex protocol test, with no model calls or user profile access."""
from __future__ import annotations

import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import tempfile
import threading
import time
import unittest

from long_task_callback import cli
from long_task_callback.desktop_connection import load_bridge_endpoint
from long_task_callback.desktop_bridge import bridge_status, stop_bridge
from long_task_callback.platforms import windows_process


@unittest.skipUnless(os.name == "nt" and os.environ.get("LTC_TEST_CODEX_BRIDGE") == "1",
                     "opt in to installed Codex bridge protocol test")
class RealCodexBridgeTests(unittest.TestCase):
    def test_desktop_stdio_and_callback_share_core_and_eof_stops_both(self):
        with tempfile.TemporaryDirectory(prefix="ltc-real-desktop-core-") as directory:
            root = Path(directory)
            profile, metadata = root / "profile", root / "state" / "bridge.json"
            codex = os.environ.get("LTC_TEST_CODEX_BRIDGE_BIN")
            if not codex:
                self.skipTest("Set LTC_TEST_CODEX_BRIDGE_BIN to a native Core executable for the Desktop adapter")
            env = dict(os.environ, CODEX_HOME=str(profile), PYTHONIOENCODING="utf-8",
                       CODEX_LONG_TASK_WAKEUP_DESKTOP_REAL_CODEX=codex,
                       CODEX_LONG_TASK_WAKEUP_DESKTOP_BRIDGE_FILE=str(metadata))
            env.pop("CODEX_THREAD_ID", None)
            incoming = queue.Queue()
            callback = None
            with (root / "wrapper.log").open("w", encoding="utf-8") as log:
                installed_wrapper = os.environ.get("LTC_TEST_DESKTOP_CORE_WRAPPER")
                launcher = ([installed_wrapper] if installed_wrapper else
                            [sys.executable, "-m", "long_task_callback.desktop_core"])
                process = subprocess.Popen(
                    [*launcher, "app-server",
                     "-c", "features.code_mode_host=true", "--analytics-default-enabled"],
                    env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=log,
                    **windows_process.background_popen_kwargs())

                def read_output():
                    for line in iter(process.stdout.readline, b""):
                        incoming.put(line)
                    incoming.put(None)

                reader = threading.Thread(target=read_output, daemon=True)
                reader.start()

                def request(identifier, method, params):
                    payload = {"id": identifier, "method": method, "params": params}
                    process.stdin.write(json.dumps(payload).encode("utf-8") + b"\n")
                    process.stdin.flush()
                    deadline = time.monotonic() + 20
                    while True:
                        line = incoming.get(timeout=max(0.01, deadline - time.monotonic()))
                        self.assertIsNotNone(line, "Desktop adapter closed before responding")
                        response = json.loads(line)
                        if response.get("id") == identifier and "method" not in response:
                            self.assertNotIn("error", response)
                            return response["result"]

                try:
                    request("desktop-init", "initialize", {
                        "clientInfo": {"name": "ltc-stdio-fixture", "version": "0.7.0a1"},
                        "capabilities": {"experimentalApi": True}})
                    process.stdin.write(b'{"method":"initialized","params":{}}\n')
                    process.stdin.flush()
                    thread = request(7, "thread/start", {"cwd": str(root)})["thread"]
                    request("persist-fixture", "thread/inject_items", {
                        "threadId": thread["id"], "items": [{"type": "message", "role": "assistant",
                        "content": [{"type": "output_text", "text": "Desktop stdio fixture; no model call."}]}]})
                    endpoint = load_bridge_endpoint(metadata, profile)
                    callback = cli.AppServerConnection(endpoint, 8)
                    callback.connect()
                    callback.request("initialize", {
                        "clientInfo": {"name": "ltc-callback-fixture", "version": "0.7.0a1"},
                        "capabilities": {"experimentalApi": True}})
                    callback.notify("initialized", {})
                    resumed = callback.request("thread/resume", {"threadId": thread["id"], "excludeTurns": True})
                    self.assertEqual(resumed["thread"]["id"], thread["id"])
                    self.assertIn(thread["id"], request("loaded", "thread/loaded/list", {})["data"])
                    self.assertEqual(bridge_status(metadata)["active_clients"], 2)
                    # Desktop lifetime owns its tool environment. An LTC client
                    # cannot keep a Core alive after Desktop's stdio EOF.
                    process.stdin.close()
                    process.wait(timeout=8)
                    reader.join(timeout=2)
                    self.assertEqual(process.returncode, 0)
                    self.assertFalse(reader.is_alive())
                    self.assertFalse(metadata.exists())
                    self.assertEqual(list(metadata.parent.glob("*.token")), [])
                    with self.assertRaises((OSError, cli.AppServerProtocolError)):
                        callback.request("thread/loaded/list", {})
                finally:
                    if callback:
                        callback.close()
                    if process.poll() is None:
                        process.terminate()
                        process.wait(timeout=5)
                    if process.stdin and not process.stdin.closed:
                        process.stdin.close()
                    reader.join(timeout=2)
                    process.stdout.close()

    def test_two_clients_resume_one_owned_test_thread_without_a_model_turn(self):
        with tempfile.TemporaryDirectory(prefix="ltc-real-bridge-") as directory:
            root = Path(directory)
            profile = root / "profile"
            metadata = root / "state" / "bridge.json"
            env = dict(os.environ, CODEX_HOME=str(profile), PYTHONIOENCODING="utf-8")
            env.pop("CODEX_THREAD_ID", None)
            command = [sys.executable, "-m", "long_task_callback.desktop_bridge",
                       "--codex-home", str(profile), "--metadata-file", str(metadata)]
            if os.environ.get("LTC_TEST_CODEX_BRIDGE_BIN"):
                command.extend(["--codex-bin", os.environ["LTC_TEST_CODEX_BRIDGE_BIN"]])
            clients = []
            with (root / "probe.log").open("w", encoding="utf-8") as log:
                process = subprocess.Popen(command, env=env, stdout=log, stderr=log,
                                           **windows_process.background_popen_kwargs())
                try:
                    deadline = time.monotonic() + 20
                    while True:
                        try:
                            endpoint = load_bridge_endpoint(metadata, profile)
                            break
                        except (OSError, ValueError):
                            if process.poll() is not None or time.monotonic() >= deadline:
                                self.fail("Isolated bridge startup failed")
                            time.sleep(0.1)
                    for name in ("ltc-desktop-fixture", "ltc-callback-fixture"):
                        client = cli.AppServerConnection(endpoint, 8)
                        clients.append(client)
                        client.connect()
                        client.request("initialize", {"clientInfo": {"name": name, "version": "0.7.0a1"},
                                                      "capabilities": {"experimentalApi": True}})
                        client.notify("initialized", {})
                    thread = clients[0].request("thread/start", {"cwd": str(root)})["thread"]
                    # A new thread has no rollout until it receives history.
                    # This documented operation persists a test item without
                    # a model turn, tool run or credential requirement.
                    clients[0].request("thread/inject_items", {"threadId": thread["id"], "items": [
                        {"type": "message", "role": "assistant", "content": [
                            {"type": "output_text", "text": "Isolated LTC protocol fixture; no model call."}]}]})
                    resumed = clients[1].request("thread/resume", {"threadId": thread["id"], "excludeTurns": True})
                    self.assertEqual(resumed["thread"]["id"], thread["id"])
                    self.assertIn(thread["id"], clients[0].request("thread/loaded/list", {})["data"])
                    self.assertEqual(bridge_status(metadata)["active_clients"], 2)
                    for client in clients:
                        client.close()
                    clients.clear()
                    deadline = time.monotonic() + 5
                    while bridge_status(metadata)["active_clients"]:
                        self.assertLess(time.monotonic(), deadline, "Closed clients did not drain")
                        time.sleep(0.05)
                    self.assertTrue(stop_bridge(metadata)["stopped"])
                    process.wait(timeout=5)
                    self.assertEqual(process.returncode, 0)
                    self.assertFalse(metadata.exists())
                    self.assertEqual(list(metadata.parent.glob("*.token")), [])
                finally:
                    for client in clients:
                        client.close()
                    if process.poll() is None:
                        process.terminate()
                        process.wait(timeout=5)


if __name__ == "__main__":
    unittest.main()
