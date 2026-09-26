"""Agent command contracts, independent of task storage and execution backends.

Adapters construct argument vectors from explicit inputs. They never launch
processes, mutate the environment, or own callback retries and task state.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

DEFAULT_APPROVALS_REVIEWER = "auto_review"
DEFAULT_APPROVAL_POLICY = "on-request"
DEFAULT_SANDBOX_MODE = "workspace-write"
DEFAULT_CLAUDE_PERMISSION_MODE = "auto"


@dataclass(frozen=True)
class ChildOptions:
    """Inputs shared by child-agent command builders.

    The caller supplies the prompt on stdin and runs the command in ``cwd``.
    ``child_result_mode`` tells the executor whether to capture stdout or read
    the result file written by the agent itself.
    """

    cwd: str
    result_path: Path
    sandbox_mode: str
    permission_mode: str
    model: str | None = None
    reasoning_effort: str | None = None


class AgentAdapter(Protocol):
    name: str
    display_name: str
    session_id_env: str
    parent_env_names: tuple[str, ...]
    detection_priority: int
    child_result_mode: Literal["file", "stdout"]
    supports_reasoning_effort: bool

    def matches_environment(self, environment: Mapping[str, str]) -> bool: ...

    def executable(self, environment: Mapping[str, str]) -> str: ...

    def resume_command(
        self, request: Mapping[str, object], environment: Mapping[str, str]
    ) -> list[str]: ...

    def child_command(
        self, options: ChildOptions, environment: Mapping[str, str]
    ) -> list[str]: ...


def resume_target(request: Mapping[str, object]) -> tuple[str, str | None]:
    """Validate stored routing before constructing any resume command."""
    target = request.get("target")
    if not isinstance(target, dict):
        raise ValueError("request target must be an object")
    kind = target.get("kind")
    if kind == "session":
        value = target.get("value")
        if not isinstance(value, str) or not value:
            raise ValueError("session target requires a non-empty value")
        return "session", value
    if kind == "last":
        return "last", None
    raise ValueError("request target kind must be 'session' or 'last'")
