"""Platform dispatch for the installed Desktop launcher lifecycle."""
from __future__ import annotations

import argparse
from pathlib import Path
import sys


def validate_setup(args: argparse.Namespace) -> None:
    app = getattr(args, "desktop_app", None)
    mode = getattr(args, "desktop_launch_mode", None)
    if app and sys.platform not in ("darwin", "win32"):
        raise ValueError("--desktop-app requires macOS or Windows")
    if mode and sys.platform != "win32":
        raise ValueError("--desktop-launch-mode is a Windows setup option")
    if mode and getattr(args, "no_desktop_launcher", False):
        raise ValueError("--desktop-launch-mode cannot be used with --no-desktop-launcher")


def install_for_setup(profile: Path, args: argparse.Namespace) -> None:
    if getattr(args, "no_desktop_launcher", False):
        return
    if sys.platform == "darwin":
        from . import desktop_macos
        desktop_macos.install_for_setup(profile, app=getattr(args, "desktop_app", None))
    elif sys.platform == "win32":
        from . import desktop_windows
        desktop_windows.install_for_setup(profile, app=getattr(args, "desktop_app", None),
                                          launch_mode=getattr(args, "desktop_launch_mode", None))


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
