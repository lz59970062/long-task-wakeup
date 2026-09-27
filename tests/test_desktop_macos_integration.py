"""Opt-in real Core probe using an empty temporary profile and no model turns."""
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
from long_task_callback.desktop_connection import load_unix_bridge_endpoint


@unittest.skipUnless(sys.platform == "darwin" and os.environ.get("LTC_TEST_MACOS_DESKTOP") == "1",
                     "opt in to the installed macOS Core protocol probe")
class MacDesktopIntegrationTests(unittest.TestCase):
    def test_two_clients_share_core_and_desktop_eof_stops_it(self):
        core = os.environ.get("LTC_TEST_CODEX_BRIDGE_BIN", "/Applications/ChatGPT.app/Contents/Resources/codex")
        with tempfile.TemporaryDirectory(prefix="ltc-mac-core-", dir="/tmp") as temporary:
            root = Path(temporary)
            profile, metadata = root / "profile", root / "bridge/bridge.json"
            profile.mkdir()
            env = dict(os.environ, CODEX_HOME=str(profile), CODEX_LONG_TASK_WAKEUP_DESKTOP_REAL_CODEX=core,
                       CODEX_LONG_TASK_WAKEUP_DESKTOP_BRIDGE_FILE=str(metadata))
            for key in ("CODEX_THREAD_ID", "CODEX_SESSION_ID", "CODEX_APP_TOOLS_PIPE_PATH"):
                env.pop(key, None)
            incoming = queue.Queue()
            callback = None
            with (root / "wrapper.log").open("w") as log:
                wrapper = os.environ.get("LTC_TEST_DESKTOP_CORE_WRAPPER")
                command = [wrapper] if wrapper else [sys.executable, "-m", "long_task_callback.desktop_core"]
                process = subprocess.Popen([*command, "-c", "features.code_mode_host=true", "app-server"],
                                           env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=log)
                def read():
                    for line in process.stdout:
                        incoming.put(line)
                    incoming.put(None)
                reader = threading.Thread(target=read, daemon=True)
                reader.start()
                def request(identifier, method, params):
                    process.stdin.write((json.dumps({"id": identifier, "method": method, "params": params}) + "\n").encode())
                    process.stdin.flush()
                    deadline = time.monotonic() + 15
                    while True:
                        line = incoming.get(timeout=max(.01, deadline - time.monotonic()))
                        self.assertIsNotNone(line, (root / "wrapper.log").read_text())
                        record = json.loads(line)
                        if record.get("id") == identifier and "method" not in record:
                            self.assertNotIn("error", record)
                            return record["result"]
                try:
                    request("init", "initialize", {"clientInfo": {"name": "ltc-desktop-fixture", "version": "1"},
                                                   "capabilities": {"experimentalApi": True}})
                    process.stdin.write(b'{"method":"initialized","params":{}}\n')
                    process.stdin.flush()
                    thread = request("new", "thread/start", {"cwd": str(root)})["thread"]["id"]
                    request("persist", "thread/inject_items", {"threadId": thread, "items": [{"type": "message", "role": "assistant",
                        "content": [{"type": "output_text", "text": "Disposable protocol fixture; no model call."}]}]})
                    endpoint = load_unix_bridge_endpoint(metadata, profile)
                    callback = cli.AppServerConnection(endpoint, 5)
                    callback.connect()
                    callback.request("initialize", {"clientInfo": {"name": "ltc-callback-fixture", "version": "1"},
                                                    "capabilities": {"experimentalApi": True}})
                    callback.notify("initialized", {})
                    resumed = callback.request("thread/resume", {"threadId": thread, "excludeTurns": True})
                    self.assertEqual(resumed["thread"]["id"], thread)
                    self.assertIn(thread, request("loaded", "thread/loaded/list", {})["data"])
                    process.stdin.close()
                    process.wait(timeout=8)
                    self.assertEqual(process.returncode, 0, (root / "wrapper.log").read_text())
                    self.assertFalse(metadata.exists())
                    self.assertFalse(endpoint.path.exists())
                    with self.assertRaises((OSError, cli.AppServerProtocolError)):
                        callback.request("thread/loaded/list", {})
                finally:
                    if callback:
                        callback.close()
                    if process.poll() is None:
                        process.terminate()
                        process.wait(timeout=8)
                    if not process.stdin.closed:
                        process.stdin.close()
                    reader.join(timeout=2)
                    process.stdout.close()


if __name__ == "__main__":
    unittest.main()
