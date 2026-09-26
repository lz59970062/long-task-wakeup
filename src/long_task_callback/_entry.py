"""Private absolute-path entry point used by OS-owned task runners."""
from pathlib import Path
import sys

if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from long_task_callback.cli import main

    raise SystemExit(main())
