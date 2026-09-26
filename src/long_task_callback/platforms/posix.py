"""POSIX file durability and crash-released locks.

These operations intentionally fail on unsupported platforms instead of
pretending that file modes or lock ownership provide equivalent guarantees.
A future Windows adapter must supply its own implementations and validation.
"""

from __future__ import annotations

import os
from pathlib import Path

try:
    import fcntl
except ImportError:  # pragma: no cover - platform explicitly unsupported
    fcntl = None


def require_posix(operation: str) -> None:
    if os.name != "posix":
        raise RuntimeError(f"{operation} requires a supported POSIX platform")


def fsync_directory(path: Path) -> None:
    """Persist directory entry changes after atomic replacement or removal.

    A filesystem that rejects directory fsync raises its native OSError. Do
    not swallow this error: the caller cannot claim durable publication.
    """
    require_posix("durable directory synchronization")
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def acquire_path_lock(path: Path, *, blocking: bool) -> tuple[object, Path] | None:
    """Acquire an exclusive advisory lock, released by process exit.

    The descriptor is not inherited across exec. Keep the lock inode stable
    while competing owners can exist; unlinking it creates a second lock
    namespace and permits simultaneous ownership.
    """
    require_posix("durable callback ownership")
    if fcntl is None:
        raise RuntimeError("durable callback ownership requires POSIX flock support")
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+", encoding="utf-8")
    try:
        os.set_inheritable(handle.fileno(), False)
        operation = fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB)
        fcntl.flock(handle.fileno(), operation)
    except BlockingIOError:
        handle.close()
        return None
    except BaseException:
        handle.close()
        raise
    return handle, path
