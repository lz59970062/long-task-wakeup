"""PI Agent CLI argument construction for child work and callback resume.

PI is both a child worker and a callback-session adapter. Child work runs a
fresh ephemeral process. The adapter constructs only the managed offline
print command; the callback worker owns online delivery and recovery policy.
Callbacks bind the canonical absolute JSONL from PI_SESSION_FILE.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Literal

from .base import ChildOptions, resume_target

PI_BIN_ENV = "LONG_TASK_WAKEUP_PI_BIN"
PI_SESSION_ID_ENV = "PI_SESSION_ID"
PI_SESSION_FILE_ENV = "PI_SESSION_FILE"
PI_CODING_AGENT_ENV = "PI_CODING_AGENT"
PI_REASONING_EFFORTS = ("off", "minimal", "low", "medium", "high", "xhigh", "max")
PI_MARKER_VALUES = frozenset({"1", "true", "yes", "on"})


class PiAdapter:
    name = "pi"
    display_name = "PI Agent"
    # ``PI_SESSION_FILE`` is an absolute path to the persisted session JSONL, so
    # a callback resumes the exact conversation even from another directory.
    session_id_env = PI_SESSION_FILE_ENV
    parent_env_names = (PI_SESSION_ID_ENV, PI_SESSION_FILE_ENV, PI_CODING_AGENT_ENV,
                        "LTC_PI_RESERVATION", "LTC_PI_RECOVERY",
                        "LTC_PI_CALLBACK_ACK_PATH", "LTC_PI_CALLBACK_CANCEL_PATH")
    # The session variables are only injected into a live PI shell tool. An
    # explicit PI session therefore outranks the parent markers a nested shell
    # may still carry from an outer Codex or Claude Code session.
    detection_priority = 20
    child_result_mode: Literal["file", "stdout"] = "stdout"
    supports_reasoning_effort = True
    reasoning_efforts = PI_REASONING_EFFORTS
    supports_system_prompt_file = True

    def matches_environment(self, environment: Mapping[str, str]) -> bool:
        if environment.get(PI_SESSION_FILE_ENV, "").strip():
            return True
        if environment.get(PI_SESSION_ID_ENV, "").strip():
            return True
        return environment.get(PI_CODING_AGENT_ENV, "").strip().lower() in PI_MARKER_VALUES

    def executable(self, environment: Mapping[str, str]) -> str:
        return environment.get(PI_BIN_ENV, "pi")

    def resume_command(
        self, request: Mapping[str, object], environment: Mapping[str, str]
    ) -> list[str]:
        # Print mode processes the callback prompt from stdin, appends one turn
        # to the bound session and exits; it never opens the interactive UI.
        cmd = [self.executable(environment), "--print"]
        kind, value = resume_target(request)
        if kind == "session":
            cmd.extend(["--session", str(value)])
        else:
            # ``--last`` remains an explicit unsafe fallback: continue the most
            # recent session for the delivery working directory.
            cmd.append("--continue")
        return cmd

    def child_command(
        self, options: ChildOptions, environment: Mapping[str, str]
    ) -> list[str]:
        if options.reasoning_effort and options.reasoning_effort not in self.reasoning_efforts:
            raise ValueError(f"unsupported PI Agent reasoning effort {options.reasoning_effort!r}")
        # Text print mode reserves stdout for the final answer and reports
        # assistant errors/aborts with a nonzero exit, unlike JSON event mode.
        # stdin carries the prompt; --no-session creates a fresh in-memory session.
        cmd = [self.executable(environment), "--print", "--mode", "text", "--no-session"]
        if options.model:
            cmd.extend(["--model", options.model])
        if options.reasoning_effort:
            cmd.extend(["--thinking", options.reasoning_effort])
        if options.system_prompt_path is not None:
            cmd.extend(["--system-prompt", str(options.system_prompt_path)])
        return cmd
