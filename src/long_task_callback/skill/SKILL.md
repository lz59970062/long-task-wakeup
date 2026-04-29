---
name: long-task-callback
description: Explicit callback workflow for long-running Codex-started tasks. Use the daemon handoff whenever Codex launches or edits a long-running command, training run, benchmark, test suite, build, deployment, Slurm job, data job, or script and should arrange for that task to resume the same Codex session when it finishes.
---

# Long Task Callback

## Rule

Use an explicit callback only when requested or when a task is likely to run long enough that Codex may be inactive when it completes.

Default to the daemon handoff path. Do not teach or suggest direct recursive `codex exec resume`
callbacks from inside Codex tool sandboxes. Direct callback mode exists only as a legacy/manual
fallback; the skill should use `--via-daemon` for normal work.

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

Or use the bundled installer:

```bash
scripts/install_from_git.sh https://github.com/lz59970062/long-task-wakeup.git
```

## Wiring Patterns

Prefer an explicit `--session <session-id>` when available. Use `--last` only when resuming the most recent Codex session is acceptable.

Daemon wrapper form, when Codex launches the command:

```bash
codex-long-task-wakeup run \
  --via-daemon \
  --session <session-id> \
  --cwd "$PWD" \
  --task "train model" \
  -- python train.py --config configs/exp.yaml
```

Daemon callback form, when Codex edits a script, shell trap, Python `finally`, or job epilogue:

```bash
set +e
python train.py --config configs/exp.yaml
status=$?
codex-long-task-wakeup done \
  --via-daemon \
  --session <session-id> \
  --cwd "$PWD" \
  --task "train model" \
  --command "python train.py --config configs/exp.yaml" \
  --exit-code "$status"
exit "$status"
```

For durable daemon handoff, standardize on a user-level systemd service:

```bash
codex-long-task-wakeup install-systemd --enable --now
```

The service runs outside Codex tool sandboxes and keeps `codex-long-task-wakeup daemon` alive with
systemd restart behavior. The installer records the resolved `codex` executable path in
`CODEX_LONG_TASK_WAKEUP_CODEX_BIN` and records the current `PATH` so Codex's runtime dependencies
such as Node/NVM are available under systemd. Use `--codex-bin /path/to/codex` or `--path "$PATH"`
if discovery is not correct. Inspect it with:

```bash
systemctl --user status codex-long-task-wakeup.service
journalctl --user -u codex-long-task-wakeup.service -f
```

Use this foreground form only for debugging:

```bash
codex-long-task-wakeup daemon
```

The daemon watches `${CODEX_HOME:-~/.codex}/long-task-wakeup/queue` by default. Use `--queue-dir`
or `CODEX_LONG_TASK_WAKEUP_QUEUE_DIR` when a different queue location is needed.

If user services must survive logout on the host, run `loginctl enable-linger "$USER"` once.

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
        "--via-daemon",
        "--session", "<session-id>",
        "--cwd", "/path/to/project",
        "--task", "train model",
        "--command", "python train.py --config configs/exp.yaml",
        "--exit-code", str(status),
    ])
```

## After Wakeup

When the callback resumes Codex, inspect artifacts, metrics, checkpoints, test reports, or generated files that are relevant to the task. Continue if the next step is clear and safe; otherwise ask one concise question.

## Multi-Round Autonomy

Use daemon handoff for controlled multi-round work, not unbounded autonomy. After every wakeup,
decide explicitly whether to continue, stop successfully, stop blocked, or ask the user. Continue
only when the next action is clear, low-risk, aligned with the same goal, and within the user's
budget and project rules.

Before launching another long task in the same goal chain, write a short decision record in the
conversation or experiment notes:

```text
current_goal:
last_result:
decision: continue | stop_success | stop_blocked | ask_user
reason:
next_command:
budget_remaining:
```

Continue automatically only for concrete follow-ups such as checking artifacts, running comparable
backtests, retrying a transient infrastructure failure, or launching the next pre-planned experiment.
Do not expand the search space, change the main research variable, alter the goal, or consume a
materially larger budget without user approval.

Stop and ask the user when repeated attempts do not resolve the issue, when the next step requires
new key resources, or when the decision is strategic rather than mechanical. Examples include:

- more GPU time, compute quota, disk, credentials, data, or external access
- changing the model family, feature set, loss design, benchmark period, or evaluation objective
- deleting or overwriting old experiments, databases, checkpoints, or production artifacts
- ambiguous metric tradeoffs, unclear success criteria, or evidence that the original hypothesis is wrong
- repeated failures whose cause is no longer a simple script, parameter, or transient environment issue

When stopping, summarize what was tried, what changed, why the loop stopped, and the smallest
decision or resource needed from the user.

Use `--strict` only when the user explicitly wants callback failure to fail the wrapper or epilogue.
