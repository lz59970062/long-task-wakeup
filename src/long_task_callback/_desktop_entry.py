"""Private absolute-path entry point for a generated Desktop Core wrapper."""
from pathlib import Path
import sys

if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from long_task_callback.desktop_core import main

    raise SystemExit(main())
