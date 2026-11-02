"""DeepSeek Harness (dsh) CLI argument construction and session routing.

DSH is a callback parent and a child worker. Callbacks are delivered to the
live DSH session by ``ltc wait`` running as one of the harness's own background
jobs; DSH has no supported non-interactive way to append a turn to a session
created under the desktop agent preset, so LTC never opens a second writer for
a DSH conversation.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Literal

from .base import ChildOptions, resume_target

DSH_BIN_ENV = "LONG_TASK_WAKEUP_DSH_BIN"
DSH_SESSION_ID_ENV = "DSH_SESSION_ID"
DSH_SHELL_ENV = "DSH_SHELL"
DSH_WEB_URL_ENV = "DSH_WEB_URL"
DSH_PROFILE_ENV = "DSH_PROFILE"
DSH_HOME_ENV = "DSH_HOME"
DSH_MARKER_VALUES = frozenset({"1", "true", "yes", "on"})
DSH_HEADLESS_PROFILE = "headless"


class DshAdapter:
    name = "dsh"
    display_name = "DeepSeek Harness"
    # DSH injects DSH_SESSION_ID into every shell it runs, so a submission made
    # from its Bash tool binds the exact conversation even from another cwd.
    session_id_env = DSH_SESSION_ID_ENV
    # Only session/ownership markers are parent state. DSH_HOME and DSH_PROFILE
    # select the child's own config and credentials, so they are preserved.
    parent_env_names = (
        DSH_SESSION_ID_ENV,
        DSH_SHELL_ENV,
        DSH_WEB_URL_ENV,
    )
    # An explicit DSH shell marker outranks parent markers a nested shell may
    # retain from Codex, Claude Code or Pi.
    detection_priority = 30
    child_result_mode: Literal["file", "stdout"] = "stdout"
    supports_model = False
    supports_reasoning_effort = False
    reasoning_efforts: tuple[str, ...] = ()
    supports_system_prompt_file = False
    # ``live`` means the daemon never resumes this agent: a callback waits for
    # the bound session to run ``ltc wait`` and is delivered there.
    callback_delivery: Literal["resume", "live"] = "live"

    def matches_environment(self, environment: Mapping[str, str]) -> bool:
        if environment.get(DSH_SESSION_ID_ENV, "").strip():
            return True
        return environment.get(DSH_SHELL_ENV, "").strip().lower() in DSH_MARKER_VALUES

    def executable(self, environment: Mapping[str, str]) -> str:
        return environment.get(DSH_BIN_ENV, "dsh")

    def resume_command(
        self, request: Mapping[str, object], environment: Mapping[str, str]
    ) -> list[str]:
        # DSH refuses a Session created under the desktop preset, and a
        # host-side resume would race the live writer in any case.
        resume_target(request)
        raise ValueError(
            "DeepSeek Harness callbacks are delivered only to the live session by `ltc wait`; "
            "DSH cannot be resumed headlessly"
        )

    def child_command(
        self, options: ChildOptions, environment: Mapping[str, str]
    ) -> list[str]:
        if options.reasoning_effort:
            raise ValueError("--reasoning-effort is not supported for DeepSeek Harness children")
        if options.system_prompt_path is not None:
            raise ValueError("--system-prompt-file is not supported for DeepSeek Harness children")
        if options.model:
            raise ValueError("--model is not supported for DeepSeek Harness children")
        # The one-shot runner reads the prompt from stdin when no positional
        # task is supplied and prints the final answer on stdout.
        return [self.executable(environment), "--profile", DSH_HEADLESS_PROFILE]
