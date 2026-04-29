---
name: long-task-callback
description: Explicit callback workflow for long-running Codex-started tasks. Use when Codex is about to launch or edit a long-running command, training run, benchmark, test suite, build, deployment, Slurm job, data job, or script and should arrange for that task to resume the same Codex session when it finishes. Do not use for automatic polling, daemons, or passive monitoring.
---

# Long Task Callback

## Rule

Use an explicit callback only when requested or when a task is likely to run long enough that Codex may be inactive when it completes. Do not install hooks, start watchers, or poll.

The callback command is:

```bash
codex-long-task-wakeup
```

## Install

If the command is missing, install it from the GitHub repository containing this skill:

```bash
python3 -m pip install "git+https://github.com/<owner>/<repo>.git#subdirectory=skills/long-task-callback"
```

For the standalone repository:

```bash
python3 -m pip install "git+https://github.com/lz59970062/long-task-wakeup.git"
```

Or use the bundled installer:

```bash
scripts/install_from_git.sh https://github.com/lz59970062/long-task-wakeup.git
```

## Wiring Patterns

Prefer an explicit `--session <session-id>` when available. Use `--last` only when resuming the most recent Codex session is acceptable.

Wrapper form, when Codex launches the command:

```bash
codex-long-task-wakeup run \
  --session <session-id> \
  --cwd "$PWD" \
  --task "train model" \
  -- python train.py --config configs/exp.yaml
```

Callback form, when Codex edits a script, shell trap, Python `finally`, or job epilogue:

```bash
set +e
python train.py --config configs/exp.yaml
status=$?
codex-long-task-wakeup done \
  --session <session-id> \
  --cwd "$PWD" \
  --task "train model" \
  --command "python train.py --config configs/exp.yaml" \
  --exit-code "$status"
exit "$status"
```

Python `finally` pattern:

```python
import subprocess

status = 1
try:
    status = subprocess.call(["python", "train.py", "--config", "configs/exp.yaml"])
finally:
    subprocess.call([
        "codex-long-task-wakeup",
        "done",
        "--session", "<session-id>",
        "--cwd", "/path/to/project",
        "--task", "train model",
        "--command", "python train.py --config configs/exp.yaml",
        "--exit-code", str(status),
    ])
```

## After Wakeup

When the callback resumes Codex, inspect artifacts, metrics, checkpoints, test reports, or generated files that are relevant to the task. Continue if the next step is clear and safe; otherwise ask one concise question.
