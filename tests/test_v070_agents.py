"""Registry contracts for adding agents without changing task ownership."""
from __future__ import annotations

import unittest
from pathlib import Path

from long_task_callback.agents import AGENTS, AgentRegistry, ChildOptions, get_agent


class FixtureAdapter:
    """A deliberately nonproduction third adapter used to exercise the seam."""

    name = "fixture-agent"
    display_name = "Fixture Agent"
    session_id_env = "FIXTURE_SESSION_ID"
    parent_env_names = ("FIXTURE_SESSION_ID", "FIXTURE_PARENT_MODE")
    detection_priority = 1000
    child_result_mode = "stdout"
    supports_reasoning_effort = False

    def matches_environment(self, environment):
        return bool(environment.get(self.session_id_env))

    def executable(self, environment):
        return environment.get("FIXTURE_EXECUTABLE", "fixture-agent")

    def resume_command(self, request, environment):
        return [self.executable(environment), "resume", request["target"]["value"]]

    def child_command(self, options, environment):
        return [self.executable(environment), "run", "--cwd", options.cwd]


class AgentRegistryTests(unittest.TestCase):
    def test_current_registry_rejects_unimplemented_agents_without_fallback(self) -> None:
        self.assertEqual(set(AGENTS.names), {"codex", "claude"})
        for name in ("pi", "dsh", "unknown", "", "Codex"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                get_agent(name)

    def test_third_adapter_can_be_added_to_registry_without_lifecycle_changes(self) -> None:
        future = FixtureAdapter()
        registry = AgentRegistry([get_agent(name) for name in AGENTS.names] + [future])
        environment = {"FIXTURE_SESSION_ID": "session-from-new-agent", "CODEX_THREAD_ID": "unrelated-codex"}
        self.assertIs(registry.get("fixture-agent"), future)
        self.assertEqual(registry.detect(environment), "fixture-agent")
        self.assertEqual(
            registry.get("fixture-agent").resume_command(
                {"target": {"kind": "session", "value": "bound-future-session"}}, environment
            ),
            ["fixture-agent", "resume", "bound-future-session"],
        )
        self.assertNotIn("fixture-agent", AGENTS.names)

    def test_all_registered_parent_markers_are_stripped_without_losing_credentials(self) -> None:
        registry = AgentRegistry([get_agent(name) for name in AGENTS.names] + [FixtureAdapter()])
        environment = {
            "CODEX_THREAD_ID": "parent-codex", "CLAUDE_CODE_SESSION_ID": "parent-claude",
            "CLAUDECODE": "1", "FIXTURE_SESSION_ID": "parent-future", "FIXTURE_PARENT_MODE": "1",
            "OPENAI_API_KEY": "fixture-key", "ANTHROPIC_API_KEY": "another-fixture-key",
            "PATH": "/fixture/bin", "CUSTOM_WORKLOAD_SETTING": "preserve",
        }
        before = dict(environment)
        child = registry.child_environment(environment)
        self.assertEqual(environment, before)
        self.assertEqual(child, {
            "OPENAI_API_KEY": "fixture-key", "ANTHROPIC_API_KEY": "another-fixture-key",
            "PATH": "/fixture/bin", "CUSTOM_WORKLOAD_SETTING": "preserve",
        })

    def test_environment_detection_preserves_existing_agent_precedence(self) -> None:
        self.assertEqual(AGENTS.detect({}), "codex")
        self.assertEqual(AGENTS.detect({"CODEX_THREAD_ID": "codex-parent"}), "codex")
        self.assertEqual(AGENTS.detect({"CLAUDE_CODE_SESSION_ID": "claude-parent"}), "claude")
        self.assertEqual(AGENTS.detect({"CODEX_THREAD_ID": "codex-parent", "CLAUDECODE": "1"}), "claude")

    def test_native_resume_commands_preserve_bound_session_and_reject_malformed_targets(self) -> None:
        for name in AGENTS.names:
            adapter = get_agent(name)
            with self.subTest(agent=name):
                command = adapter.resume_command({"target": {"kind": "session", "value": "bound-session"}}, {})
                self.assertIn("bound-session", command)
                self.assertNotIn("--last", command)
            for target in (None, {}, {"kind": "session", "value": ""}, {"kind": "future-unknown"}):
                with self.subTest(agent=name, target=target), self.assertRaises(ValueError):
                    adapter.resume_command({"target": target}, {})

    def test_codex_child_keeps_approval_and_sandbox_options_compatible(self) -> None:
        options = ChildOptions(
            cwd="/tmp/work with spaces", result_path=Path("/tmp/result with spaces.md"),
            sandbox_mode="workspace-write", permission_mode="auto", model="fixture-model",
            reasoning_effort="max",
        )
        environment = {"CODEX_LONG_TASK_WAKEUP_CODEX_BIN": "/opt/my codex/codex"}
        before = dict(environment)
        command = get_agent("codex").child_command(options, environment)
        self.assertEqual(command[0], "/opt/my codex/codex")
        self.assertIn("workspace-write", command)
        self.assertIn("--ephemeral", command)
        self.assertIn("fixture-model", command)
        self.assertIn('model_reasoning_effort="max"', command)
        self.assertNotIn("--approve-for-me", command)
        self.assertNotIn("--full-auto", command)
        self.assertEqual(environment, before)

    def test_registry_rejects_duplicate_or_missing_default_integration(self) -> None:
        with self.assertRaises(ValueError):
            AgentRegistry([get_agent("codex"), get_agent("codex")])
        with self.assertRaises(ValueError):
            AgentRegistry([FixtureAdapter()], default="codex")


if __name__ == "__main__":
    unittest.main()
