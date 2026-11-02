"""Platform dispatch for the retired Desktop launcher; the `ltc` command no longer reaches it."""
from __future__ import annotations

import argparse
import sys


def command(args: argparse.Namespace) -> int:
    if sys.platform == "win32":
        from . import desktop_windows
        return desktop_windows.command(args)
    if sys.platform == "darwin":
        if getattr(args, "core", None) or getattr(args, "launch_mode", None):
            print("ltc: Desktop --core and --launch-mode options require Windows", file=sys.stderr)
            return 2
        from . import desktop_macos
        return desktop_macos.command(args)
    print("ltc: Desktop launchers support macOS and Windows", file=sys.stderr)
    return 2
