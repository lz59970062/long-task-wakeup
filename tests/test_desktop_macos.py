from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
from pathlib import Path
import plistlib
import sys
import tempfile
import unittest
from unittest import mock

from long_task_callback import cli, desktop_macos as desktop
from long_task_callback.desktop_connection import load_unix_bridge_endpoint
from long_task_callback.desktop_core import app_server_arguments


@unittest.skipUnless(sys.platform == "darwin", "macOS Desktop launcher")
class DesktopLauncherTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="ltc-desktop-", dir="/tmp")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.profile = self.root / "profile"
        self.app = self.root / "Example Desktop.app"
        for name in ("Contents/MacOS/Example", "Contents/Resources/codex", "wrapper"):
            p = self.app / name
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("fixture")
            p.chmod(0o700)
        (self.app / "Contents/Info.plist").write_bytes(plistlib.dumps({"CFBundleExecutable": "Example"}))
        self.wrapper = self.app / "wrapper"
        self.config = desktop.paths(self.app, self.wrapper, self.profile)

    def test_configuration_keeps_exact_bundle_core_and_profile(self):
        self.assertEqual(self.config["core"], str(self.app / "Contents/Resources/codex"))
        self.assertEqual(self.config["codex_home"], str(self.profile))
        self.assertEqual(Path(self.config["metadata"]).name, "bridge.json")

    def test_environment_is_process_local_and_drops_stale_tool_pipes(self):
        with mock.patch.dict(os.environ, {"CODEX_APP_TOOLS_PIPE_PATH": "stale", "CODEX_THREAD_ID": "old",
                                         "CODEX_APP_SERVER_WS_URL": "ws://unwanted"}):
            env = desktop.launch_environment(self.config)
            self.assertNotIn("CODEX_APP_TOOLS_PIPE_PATH", env)
            self.assertNotIn("CODEX_THREAD_ID", env)
            self.assertNotIn("CODEX_APP_SERVER_WS_URL", env)
            self.assertEqual(env["CODEX_CLI_PATH"], str(self.wrapper))
            self.assertEqual(os.environ["CODEX_APP_TOOLS_PIPE_PATH"], "stale")

    def test_prepare_only_writes_configuration_and_quoted_launcher(self):
        args = argparse.Namespace(desktop_action="prepare", app=str(self.app), wrapper=str(self.wrapper), force=False)
        with mock.patch.object(sys, "platform", "darwin"), mock.patch.object(cli, "codex_home", return_value=self.profile), \
                mock.patch.object(desktop, "preflight", return_value={"launch_ready": False, "launch_blockers": ["desktop_running"]}), \
                mock.patch.object(desktop.subprocess, "Popen") as launch, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(desktop.command(args), 0)
        launch.assert_not_called()
        launcher = self.profile / "long-task-wakeup/Start LTC Desktop.command"
        self.assertTrue(launcher.is_file())
        self.assertIn("desktop launch", launcher.read_text())
        self.assertEqual(json.loads((self.profile / "long-task-wakeup/desktop.json").read_text()), self.config)

    def test_launch_refuses_existing_desktop_without_signaling_it(self):
        args = argparse.Namespace(desktop_action="launch", check_only=False)
        with mock.patch.object(sys, "platform", "darwin"), mock.patch.object(cli, "codex_home", return_value=self.profile), \
                mock.patch.object(desktop, "read_config", return_value=self.config), \
                mock.patch.object(desktop, "preflight", return_value={"launch_ready": False}), \
                mock.patch.object(desktop.subprocess, "Popen") as launch, \
                contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(desktop.command(args), 2)
        launch.assert_not_called()

    def test_wrapper_passes_daemon_and_proxy_subcommands_through(self):
        for command in (["app-server", "daemon", "version"], ["app-server", "proxy"]):
            self.assertIsNone(app_server_arguments(command))
        self.assertEqual(app_server_arguments(["app-server", "--stdio"]), ["app-server"])


@unittest.skipUnless(sys.platform == "darwin", "Darwin filesystem metadata")
class MacMetadataTests(unittest.TestCase):
    def test_public_or_symlink_metadata_is_rejected_before_connection(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            p = root / "bridge.json"
            p.write_text('{}')
            p.chmod(0o644)
            with self.assertRaisesRegex(ValueError, "private regular file"):
                load_unix_bridge_endpoint(p, root)
            link = root / "alias.json"
            link.symlink_to(p)
            with self.assertRaisesRegex(ValueError, "private regular file"):
                load_unix_bridge_endpoint(link, root)


if __name__ == "__main__":
    unittest.main()
