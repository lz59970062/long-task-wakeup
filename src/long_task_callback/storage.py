"""Private, atomic file publication for durable LTC records.

The current implementation requires POSIX permissions and directory fsync.
Files are written beside their destination, flushed, and replaced atomically;
readers therefore see either the old complete file or the new complete file.
This does not serialize read-modify-write operations: callers still need locks.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Callable
import uuid

from .platforms import posix


def write_private_text(
    path: Path,
    text: str,
    *,
    sync_directory: Callable[[Path], None] | None = None,
) -> None:
    """Replace a UTF-8 file with mode 0600 and persist its directory entry.

    If directory synchronization fails after replacement, the new data may
    already be visible. An exception must not be read as proof of no write.
    ``sync_directory`` preserves the coordinator's existing durability hook.
    """
    posix.require_posix("private durable files")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            handle = os.fdopen(descriptor, "w", encoding="utf-8")
        except BaseException:
            os.close(descriptor)
            raise
        with handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        (sync_directory or posix.fsync_directory)(path.parent)
    except BaseException:
        try:
            temporary.unlink()
        except OSError:
            pass
        raise


def write_request(
    path: Path,
    request: dict[str, object],
    *,
    sync_directory: Callable[[Path], None] | None = None,
) -> None:
    """Publish deterministic JSON using the same private file guarantees."""
    text = json.dumps(request, ensure_ascii=False, sort_keys=True) + "\n"
    write_private_text(path, text, sync_directory=sync_directory)
