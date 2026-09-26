"""GNU screen ownership for Linux containers and macOS compatibility tasks.

Screen keeps a worker independent of its submitting shell or standalone LTC
coordinator. It does not escape a container's lifetime or a systemd service's
control group. Durable task/result records, rather than terminal sessions,
remain the source of truth for completion and recovery.
"""

from __future__ import annotations

import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

from .base import LaunchError, OwnerState


SCREEN_BIN_ENV = "LONG_TASK_WAKEUP_SCREEN_BIN"
CONTROL_TIMEOUT_SECONDS = 10.0
_TASK_ID = re.compile(r"[A-Za-z0-9_-]+\Z")
_OWNER = re.compile(r"ltc-[A-Za-z0-9_-]+\Z")
_LIVE_STATUS = re.compile(r"\((?:Attached|Detached|Multi, (?:attached|detached))\)\s*$")


def screen_binary() -> str | None:
    """Honor the historical executable override without shell evaluation."""
    configured = os.environ.get(SCREEN_BIN_ENV)
    return shutil.which(configured) if configured else shutil.which("screen")


def _query_environment() -> dict[str, str]:
    # GNU screen does not offer structured session listings. Fix the locale
    # so a genuine empty socket directory can be distinguished from errors.
    return dict(os.environ, LC_ALL="C", LANG="C")


def _validate_owner(owner: str) -> None:
    if not isinstance(owner, str) or not _OWNER.fullmatch(owner):
        raise ValueError("invalid LTC screen session name")


class ScreenBackend:
    """Compatibility owner used when a systemd user manager is unavailable."""

    name = "screen"

    def available(self) -> bool:
        if sys.platform not in ("linux", "darwin"):
            return False
        executable = screen_binary()
        if executable is None:
            return False
        try:
            result = subprocess.run(
                [executable, "-v"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
                timeout=CONTROL_TIMEOUT_SECONDS,
                env=_query_environment(),
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
        return result.returncode == 0 and bool(re.match(r"Screen version \d", result.stdout or ""))

    def owner_name(self, task_id: str, attempt: int, queue_root: Path) -> str:
        """Preserve the legacy session name for saved screen task records.

        Unlike native service names, this identifier is reused across launch
        attempts. The coordinator must serialize attempts and confirm the
        previous owner is absent before retrying. Task IDs must be unique in
        the user's screen namespace, including across independent queues.
        """
        if not isinstance(task_id, str) or not _TASK_ID.fullmatch(task_id):
            raise ValueError("task id must contain only letters, numbers, '-' or '_'")
        if isinstance(attempt, bool) or not isinstance(attempt, int) or attempt < 1:
            raise ValueError("launch attempt must be a positive integer")
        return f"ltc-{task_id}"

    def launch(self, owner: str, argv: list[str], cwd: Path, log_path: Path) -> None:
        try:
            _validate_owner(owner)
            if not argv or any(not isinstance(value, str) or "\0" in value for value in argv):
                raise ValueError("worker argv must contain non-NUL strings")
            if not Path(argv[0]).is_absolute():
                raise ValueError("worker executable must use an absolute path")
            working_directory = str(Path(cwd).expanduser().resolve())
            output_path = str(Path(log_path).expanduser().resolve())
            if "\0" in working_directory or "\0" in output_path:
                raise ValueError("worker paths must not contain NUL")
        except (OSError, ValueError) as error:
            raise LaunchError(str(error), uncertain=False) from error
        if sys.platform not in ("linux", "darwin"):
            raise LaunchError("screen workers require Linux or macOS", uncertain=False)
        executable = screen_binary()
        if executable is None:
            raise LaunchError("GNU screen was not found", uncertain=False)
        command = [executable, "-dmS", owner, "-L", "-Logfile", output_path, *argv]
        if sys.platform == "darwin":
            # Apple's bundled screen 4.0 lacks -Logfile. The fixed shell program
            # only redirects output; every path/worker argument stays literal.
            # Ignore screenrc so user settings cannot create extra windows.
            command = [executable, "-c", "/dev/null", "-dmS", owner,
                       "/bin/sh", "-c", 'log=$1; shift; exec "$@" >>"$log" 2>&1',
                       "ltc-screen", output_path, *argv]
        try:
            result = subprocess.run(
                command,
                cwd=working_directory,
                stdin=subprocess.DEVNULL,
                # A detached owner must not retain coordinator-owned pipes.
                # Workload output is already captured by screen's log file.
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                text=True,
                check=False,
                timeout=CONTROL_TIMEOUT_SECONDS,
                umask=0o077,
            )
        except subprocess.TimeoutExpired as error:
            raise LaunchError("screen worker submission timed out; its outcome is unknown", uncertain=True) from error
        except OSError as error:
            raise LaunchError(
                f"could not start screen: {error.strerror or type(error).__name__}", uncertain=False,
            ) from error
        if result.returncode != 0:
            raise LaunchError(
                f"screen exited with status {result.returncode}; reconcile worker state before retrying",
                uncertain=True,
            )

    def probe(self, owner: str) -> OwnerState:
        try:
            _validate_owner(owner)
        except ValueError:
            return OwnerState.UNKNOWN
        if sys.platform not in ("linux", "darwin"):
            return OwnerState.UNKNOWN
        executable = screen_binary()
        if executable is None:
            return OwnerState.UNKNOWN
        try:
            result = subprocess.run(
                [executable, "-ls", owner],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
                timeout=CONTROL_TIMEOUT_SECONDS,
                env=_query_environment(),
            )
        except (OSError, subprocess.TimeoutExpired):
            return OwnerState.UNKNOWN
        if result.returncode not in (0, 1) or (result.stderr or "").strip():
            return OwnerState.UNKNOWN
        output = result.stdout or ""
        matches = re.findall(rf"^[ \t]*\d+\.{re.escape(owner)}[ \t]+[^\r\n]*", output, re.MULTILINE)
        if any(_LIVE_STATUS.search(line) for line in matches):
            return OwnerState.ALIVE
        if matches:
            # A dead socket is affirmative evidence that this screen owner
            # vanished. It is not evidence that its task never executed.
            return OwnerState.ABSENT if all("(Dead" in line for line in matches) else OwnerState.UNKNOWN
        if re.fullmatch(r"No Sockets found in [^\r\n]+\.", output.strip()):
            return OwnerState.ABSENT
        # `screen -ls name` also matches names containing `name`. A successful
        # normal listing without the exact owner is an explicit negative.
        if result.returncode == 0 and re.search(r"^There (?:is a screen|are screens) on:", output, re.MULTILINE):
            return OwnerState.ABSENT
        return OwnerState.UNKNOWN
