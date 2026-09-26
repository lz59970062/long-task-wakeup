"""Platform execution adapters, independent of task and agent policy."""

from .base import ExecutionBackend, LaunchError, OwnerState
from .linux import SystemdUserBackend
from .macos import LaunchdBackend
from .screen import ScreenBackend

__all__ = ["ExecutionBackend", "LaunchError", "OwnerState", "ScreenBackend", "SystemdUserBackend", "LaunchdBackend"]
