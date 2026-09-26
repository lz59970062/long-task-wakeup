"""Launch the same installed runtime, independent of PATH and the caller's cwd.

All private workers use this entry point. A future frozen distribution can use
its executable directly without pretending it is a Python interpreter.
"""
from pathlib import Path
import sys


def worker_command(*arguments: str) -> list[str]:
    if getattr(sys, "frozen", False):
        return [sys.executable, *arguments]
    return [sys.executable, str(Path(__file__).with_name("_entry.py")), *arguments]
