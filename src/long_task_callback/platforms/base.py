"""Small execution-owner contract shared by platform integrations.

The coordinator persists task intent before calling a backend. Backends only
manage an independent worker process: the worker is responsible for recording
the command's result. An owner disappearing never proves a task did not run.
"""

from __future__ import annotations

from enum import Enum
from pathlib import Path
from typing import Protocol


class OwnerState(str, Enum):
    """A conservative snapshot of a worker's platform owner."""

    ALIVE = "alive"
    ABSENT = "absent"
    UNKNOWN = "unknown"


class LaunchError(RuntimeError):
    """Submission failed, possibly after the platform accepted the request.

    ``uncertain=True`` means the coordinator must reconcile persisted worker
    evidence instead of immediately launching another copy. A timeout, lost
    manager connection, or nonzero launcher exit can have this outcome.
    """

    def __init__(self, message: str, *, uncertain: bool) -> None:
        super().__init__(message)
        self.uncertain = uncertain


class ExecutionBackend(Protocol):
    """Platform boundary; implementations must not depend on CLI state.

    Launch arguments contain only the worker entry point and references to
    private task files. Prompts, credentials, and task environments belong in
    those files, never in platform command lines or unit properties.
    """

    name: str

    def available(self) -> bool:
        """Check that the platform manager is currently reachable."""
        ...

    def owner_name(self, task_id: str, attempt: int, queue_root: Path) -> str:
        """Return a stable owner identifier unique to queue, task, and attempt."""
        ...

    def launch(self, owner: str, argv: list[str], cwd: Path, log_path: Path) -> None:
        """Submit an independent worker, or raise :class:`LaunchError`."""
        ...

    def probe(self, owner: str) -> OwnerState:
        """Report ownership; unavailable or ambiguous observations are UNKNOWN."""
        ...
