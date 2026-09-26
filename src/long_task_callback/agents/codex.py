"""Codex CLI argument construction; no subprocess or filesystem operations."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Literal

from .base import (
    ChildOptions,
    DEFAULT_APPROVAL_POLICY,
    DEFAULT_APPROVALS_REVIEWER,
    DEFAULT_SANDBOX_MODE,
    resume_target,
)

CODEX_THREAD_ID_ENV = "CODEX_THREAD_ID"
CODEX_BIN_ENV = "CODEX_LONG_TASK_WAKEUP_CODEX_BIN"


class CodexAdapter:
    name = "codex"
    display_name = "Codex"
    session_id_env = CODEX_THREAD_ID_ENV
    parent_env_names = (CODEX_THREAD_ID_ENV,)
    detection_priority = 0
    child_result_mode: Literal["file", "stdout"] = "file"
    supports_reasoning_effort = True

    def matches_environment(self, environment: Mapping[str, str]) -> bool:
        return bool(environment.get(self.session_id_env, "").strip())

    def executable(self, environment: Mapping[str, str]) -> str:
        return environment.get(CODEX_BIN_ENV, "codex")

    def resume_command(
        self, request: Mapping[str, object], environment: Mapping[str, str]
    ) -> list[str]:
        cmd = [self.executable(environment), "exec", "resume", "--all"]
        for key, default in (
            ("approvals_reviewer", DEFAULT_APPROVALS_REVIEWER),
            ("approval_policy", DEFAULT_APPROVAL_POLICY),
            ("sandbox_mode", DEFAULT_SANDBOX_MODE),
        ):
            value = request.get(key, default)
            if isinstance(value, str) and value:
                cmd.extend(["-c", f"{key}={json.dumps(value)}"])
        queue_root = request.get("queue_dir")
        if isinstance(queue_root, str) and queue_root:
            cmd.extend(["-c", f"sandbox_workspace_write.writable_roots=[{json.dumps(queue_root)}]"])
        kind, value = resume_target(request)
        cmd.append(str(value) if kind == "session" else "--last")
        cmd.append("-")
        return cmd

    def child_command(
        self, options: ChildOptions, environment: Mapping[str, str]
    ) -> list[str]:
        cmd = [self.executable(environment), "exec", "--ephemeral"]
        if options.model:
            cmd.extend(["--model", options.model])
        if options.reasoning_effort:
            cmd.extend(["-c", f"model_reasoning_effort={json.dumps(options.reasoning_effort)}"])
        # --approve-for-me conflicts with --sandbox in Codex 0.153.4. Keep the
        # requested sandbox and set approval configuration explicitly instead.
        cmd.extend([
            "-C", options.cwd,
            "-s", options.sandbox_mode,
            "-c", f"approvals_reviewer={json.dumps(DEFAULT_APPROVALS_REVIEWER)}",
            "-c", f"approval_policy={json.dumps(DEFAULT_APPROVAL_POLICY)}",
            "-o", str(options.result_path),
            "-",
        ])
        return cmd
