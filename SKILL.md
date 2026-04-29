---
name: long-task-callback
description: Explicit callback workflow for long-running Codex-started tasks. Use when Codex is about to launch or edit a long-running command, training run, benchmark, test suite, build, deployment, Slurm job, data job, or script and should arrange for that task to resume the same Codex session when it finishes. Use the daemon handoff for recursive or multi-stage callbacks that may run inside Codex tool sandboxes.
---

# Long Task Callback

## Rule

Use an explicit callback only when requested or when a task is likely to run long enough that Codex may be inactive when it completes. Do not install hooks, start watchers, or poll unless the user explicitly wants the wakeup daemon for recursive or multi-stage callbacks.

Do not let callback behavior interfere with task behavior. The task's original exit code and control flow must remain the source of truth.

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

After pip installation, install the bundled Codex skill:

```bash
codex-long-task-wakeup install-skill
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

Daemon handoff, preferred when this callback may be invoked from inside a resumed Codex turn:

```bash
codex-long-task-wakeup run \
  --via-daemon \
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

Add `--via-daemon` to `done` when that callback may run inside a resumed Codex tool sandbox.

Run the daemon from a normal user shell, screen, tmux, or a user service outside Codex tool sandboxes:

```bash
codex-long-task-wakeup daemon
```

The daemon watches `${CODEX_HOME:-~/.codex}/long-task-wakeup/queue` by default. Use `--queue-dir`
or `CODEX_LONG_TASK_WAKEUP_QUEUE_DIR` when a different queue location is needed.

Keep `exit "$status"` after the callback. By default `codex-long-task-wakeup done` returns 0 even when Codex cannot be resumed, so the task result remains independent of wakeup success.

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

Use `--strict` only when the user explicitly wants callback failure to fail the wrapper or epilogue.
