"""PI Agent child commands; PI callback/session resume is not registered."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Literal

from .base import ChildOptions

PI_BIN_ENV = "LONG_TASK_WAKEUP_PI_BIN"
PI_REASONING_EFFORTS = ("off", "minimal", "low", "medium", "high", "xhigh", "max")


class PiChildAdapter:
    name = "pi"
    display_name = "PI Agent"
    parent_env_names = ("PI_SESSION_ID", "PI_SESSION_FILE")
    child_result_mode: Literal["file", "stdout"] = "stdout"
    supports_reasoning_effort = True
    reasoning_efforts = PI_REASONING_EFFORTS
    supports_system_prompt_file = True

    def executable(self, environment: Mapping[str, str]) -> str:
        return environment.get(PI_BIN_ENV, "pi")

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
