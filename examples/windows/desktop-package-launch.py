"""Compatibility entry point; the installable package owns this implementation.

Execute in this module namespace so existing importers can still replace helper
functions and dependencies without maintaining a second implementation here.
"""
from pathlib import Path as _CompatibilityPath

_source = (_CompatibilityPath(__file__).resolve().parents[2]
           / "src" / "long_task_callback" / "desktop_windows_assets"
           / "desktop-package-launch.py")
if not _source.is_file():
    from importlib.resources import files as _resource_files
    _source = _CompatibilityPath(str(_resource_files("long_task_callback")
                                    / "desktop_windows_assets" / "desktop-package-launch.py"))
__file__ = str(_source)
exec(compile(_source.read_bytes(), __file__, "exec"), globals(), globals())
