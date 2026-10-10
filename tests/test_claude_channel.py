from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from long_task_callback import claude_channel, claude_code, cli

SRC = str(Path(__file__).resolve().parents[1] / "src")
SESSION = "3f0c9a52-claude-session"
OTHER_SESSION = "77aa01b2-claude-session"
CLAUDE_PID = 424242


class ChannelOptInTests(unittest.TestCase):
    def test_only_a_server_ltc_opt_in_enables_delivery(self) -> None:
        enabled = claude_channel.channel_enabled
        self.assertTrue(enabled(["claude", "--dangerously-load-development-channels", "server:ltc"]))
        self.assertTrue(enabled(["claude", "--dangerously-load-development-channels", "server:x", "server:ltc"]))
        self.assertTrue(enabled(["claude", "--channels=server:ltc"]))
        self.assertFalse(enabled(["claude", "--mcp-config", "{}"]))
        self.assertFalse(enabled(["claude", "--channels", "plugin:telegram@claude-plugins-official"]))
        self.assertFalse(enabled(["claude", "--dangerously-load-development-channels", "--resume", "server:ltc"]))

    def test_launch_adds_the_server_and_the_development_flag(self) -> None:
        root = Path("/q")
        command = claude_channel.launch_command("/bin/claude", root, ["--resume", SESSION],
                                                {cli.TARGET_LOCK_DIR_ENV: "/locks", "PATH": "/bin"})
        self.assertEqual(command[0], "/bin/claude")
        self.assertEqual(command[3:6], ["--dangerously-load-development-channels", "server:ltc", "--resume"])
        server = json.loads(command[2])["mcpServers"]["ltc"]
        self.assertEqual(server["args"][-3:], ["_claude-channel", "--queue-dir", "/q"])
        self.assertEqual(server["env"], {cli.TARGET_LOCK_DIR_ENV: "/locks"})
        self.assertTrue(claude_channel.channel_enabled(command))


class ChannelServerTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.root = self.directory / "queue"
        self.environment = {
            "CODEX_HOME": str(self.directory / "codex-home"),
            "CLAUDE_CONFIG_DIR": str(self.directory / "claude"),
            cli.TARGET_LOCK_DIR_ENV: str(self.directory / "target-locks"),
        }
        patcher = mock.patch.dict(os.environ, self.environment)
        patcher.start()
        self.addCleanup(patcher.stop)
        cli.ensure_daemon_dirs(self.root)
        self.set_session(SESSION)

    def set_session(self, session: str) -> None:
        path = self.directory / "claude/sessions" / f"{CLAUDE_PID}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"pid": CLAUDE_PID, "sessionId": session}), encoding="utf-8")

    def enqueue(self, ident: str, *, session: str = SESSION, agent: str = "claude") -> Path:
        args = argparse.Namespace(
            agent=agent, session=session, last=False, task="train model", cwd=str(self.directory),
            command="python train.py", exit_code=0, message=None, queue_dir=str(self.root), _callback_id=ident,
        )
        prompt = cli.build_prompt(args, duration=12.0)
        request = cli.make_request(args, prompt)
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(cli.enqueue_existing_request(self.root, request, prompt), 0)
        return cli.request_path(self.root, "pending", ident)

    def server(self, *, enabled: bool = True, ack_grace: float = 3600.0) -> claude_channel.ChannelServer:
        with mock.patch.object(claude_channel, "parent_command_line",
                               return_value=["claude", "--dangerously-load-development-channels", "server:ltc"]
                               if enabled else ["claude"]):
            server = claude_channel.ChannelServer(self.root, parent_pid=CLAUDE_PID, environment=os.environ,
                                                  poll_interval=0.05, ack_grace=ack_grace, out=io.StringIO())
        self.addCleanup(server.release)
        return server

    def messages(self, server: claude_channel.ChannelServer) -> list[dict]:
        return [json.loads(line) for line in server.out.getvalue().splitlines()]

    def poll(self, server: claude_channel.ChannelServer) -> str:
        errors = io.StringIO()
        with contextlib.redirect_stderr(errors):
            server.poll()
        return errors.getvalue()

    def test_initialize_declares_the_channel_and_avoids_unsupported_protocols(self) -> None:
        server = self.server()
        server.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                       "params": {"protocolVersion": "2026-07-28", "capabilities": {}}})
        server.handle({"jsonrpc": "2.0", "id": 2, "method": "initialize",
                       "params": {"protocolVersion": "2025-03-26"}})
        server.handle({"jsonrpc": "2.0", "id": 3, "method": "ping"})
        server.handle({"jsonrpc": "2.0", "id": 4, "method": "tools/call"})
        server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"})
        first, second, ping, unknown = self.messages(server)
        self.assertEqual(first["result"]["capabilities"], {"experimental": {"claude/channel": {}}})
        self.assertEqual(first["result"]["protocolVersion"], "2025-06-18")
        self.assertIn("ltc wait", first["result"]["instructions"])
        self.assertEqual(second["result"]["protocolVersion"], "2025-03-26")
        self.assertEqual(ping, {"jsonrpc": "2.0", "id": 3, "result": {}})
        self.assertEqual(unknown["error"]["code"], -32601)
        self.assertTrue(server.initialized.is_set())

    def test_pushes_each_session_callback_once_and_owns_delivery(self) -> None:
        mine = self.enqueue("cb-mine")
        self.enqueue("cb-other", session=OTHER_SESSION)
        codex = self.enqueue("cb-codex", agent="codex")
        server = self.server()
        self.poll(server)
        self.poll(server)
        (event,) = self.messages(server)
        self.assertEqual(event["method"], "notifications/claude/channel")
        self.assertEqual(event["params"]["meta"], {"callback_id": "cb-mine"})
        self.assertIn("[long-task-callback]", event["params"]["content"])
        self.assertTrue(mine.exists())  # Back in pending until ACKed.
        self.assertTrue(claude_code.channel_is_held(SESSION))
        self.assertFalse(claude_code.channel_is_held(OTHER_SESSION))
        # The daemon only sees callbacks the channel does not own.
        selected = cli.select_pending(self.root, time.time() + 2 * claude_code.LIVE_ACK_GRACE_SECONDS)
        self.assertIn(selected, (codex, cli.request_path(self.root, "pending", "cb-other")))
        self.assertNotEqual(selected, mine)
        # A stray `ltc wait` in the channel session exits instead of printing a duplicate.
        errors = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()) as output, contextlib.redirect_stderr(errors):
            status = claude_code.wait(argparse.Namespace(queue_dir=str(self.root), session=SESSION, task=None,
                                                         timeout=1.0, poll_interval=0.05))
        self.assertEqual((status, output.getvalue()), (0, ""))
        self.assertIn("ltc channel", errors.getvalue())
        server.release()
        self.assertFalse(claude_code.channel_is_held(SESSION))

    def test_follows_the_window_to_a_new_session(self) -> None:
        server = self.server()
        self.poll(server)
        self.set_session(OTHER_SESSION)
        self.enqueue("cb-other", session=OTHER_SESSION)
        self.poll(server)
        self.assertFalse(claude_code.channel_is_held(SESSION))
        self.assertTrue(claude_code.channel_is_held(OTHER_SESSION))
        (event,) = self.messages(server)
        self.assertEqual(event["params"]["meta"]["callback_id"], "cb-other")

    def test_unacknowledged_push_hands_delivery_back_to_the_daemon(self) -> None:
        self.enqueue("cb-mine")
        server = self.server(ack_grace=0.0)
        self.poll(server)
        errors = self.poll(server)
        self.assertIn("daemon takes over", errors)
        self.assertFalse(claude_code.channel_is_held(SESSION))
        self.poll(server)
        self.assertEqual(len(self.messages(server)), 1)

    def test_acknowledged_callbacks_are_not_pushed(self) -> None:
        self.enqueue("cb-mine")
        with contextlib.redirect_stderr(io.StringIO()):
            cli.ack(argparse.Namespace(queue_dir=str(self.root), id="cb-mine", message=None))
        server = self.server()
        self.poll(server)
        self.assertEqual(self.messages(server), [])

    def test_plain_mcp_load_never_claims_or_holds_delivery(self) -> None:
        self.enqueue("cb-mine")
        server = self.server(enabled=False)
        stdin = io.StringIO(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize"}) + "\n")
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(server.run(stdin), 0)
        (reply,) = self.messages(server)
        self.assertEqual(reply["id"], 1)
        self.assertFalse(claude_code.channel_is_held(SESSION))

    def test_stdio_server_pushes_and_exits_when_claude_closes_the_pipe(self) -> None:
        # The parent's command line carries the channel opt-in, like `ltc claude`.
        self.enqueue("cb-mine")
        environment = dict(os.environ, PYTHONPATH=SRC, CODEX_LONG_TASK_WAKEUP_QUEUE_DIR=str(self.root))
        child = [sys.executable, "-m", "long_task_callback", "_claude-channel", "--poll-interval", "0.05"]
        parent = ("import os,subprocess,sys,json;"
                  f"open(os.path.join({str(self.directory / 'claude/sessions')!r}, f'{{os.getpid()}}.json'),'w')"
                  f".write(json.dumps({{'sessionId': {SESSION!r}}}));"
                  "p=subprocess.Popen(json.loads(sys.argv[1]),stdin=subprocess.PIPE,stdout=subprocess.PIPE,text=True);"
                  "p.stdin.write(json.dumps({'jsonrpc':'2.0','id':1,'method':'initialize','params':{}})+'\\n');"
                  "p.stdin.write(json.dumps({'jsonrpc':'2.0','method':'notifications/initialized'})+'\\n');"
                  "p.stdin.flush();print(p.stdout.readline(),end='');print(p.stdout.readline(),end='');"
                  "p.stdin.close();sys.exit(p.wait(timeout=10))")
        result = subprocess.run([sys.executable, "-c", parent, json.dumps(child),
                                 "--dangerously-load-development-channels", "server:ltc"],
                                capture_output=True, text=True, timeout=30, env=environment)
        self.assertEqual(result.returncode, 0, result.stderr)
        initialize, event = (json.loads(line) for line in result.stdout.splitlines())
        self.assertIn("claude/channel", initialize["result"]["capabilities"]["experimental"])
        self.assertEqual(event["params"]["meta"]["callback_id"], "cb-mine")
        self.assertFalse(claude_code.channel_is_held(SESSION))


if __name__ == "__main__":
    unittest.main()
