---
name: long-task-callback
description: Explicit callback workflow for long-running agent tasks. Use the daemon handoff whenever Codex or Claude Code launches or edits a long-running command, training run, benchmark, test suite, build, deployment, Slurm job, data job, or script and should arrange for that task to resume the same agent session when it finishes.
---

# Long Task Callback

## Rule

Use an explicit callback when requested or when a command may outlive the current Codex or Claude
Code turn.

There are two public workflows:

- `ltc run -- <command>` submits a new long-running task. The daemon launches it in
  GNU screen.
- `ltc done ...` queues a completion callback for work already owned by screen, tmux,
  Slurm, another scheduler, or an existing script.

Daemon handoff is unconditional and requires no opt-in flag. Never add `--via-daemon` to newly
written commands; old scripts may still pass it as a hidden no-op compatibility flag. Do not
invent another public launch flag or a direct-mode flag. Do not use a direct recursive
`codex exec resume` or `claude -p --resume` call from an agent tool sandbox. Do not let callback
behavior change the task's original exit code unless the user explicitly requests `--strict`.

## Ownership invariant

The durable topology for an LTC-launched task is:

```text
systemd user service
  └─ ltc daemon                 control, recovery, callback delivery

GNU screen session
  └─ LTC worker
      └─ wrapped task
```

The daemon must not own the training process as its child. It persists the submission and creates
the detached screen session; screen owns the worker and the worker owns the task.

GNU screen is mandatory. If it is missing, stop and ask the user to install it. Never fall back to
an agent-owned wrapper.

After a successful submission, report the task id, screen session, and log path, then end the
agent turn. Do not poll processes, logs, or artifacts on a timer. Live monitoring is allowed only
when the user asks or callback infrastructure itself is being diagnosed.

The command name is `ltc`; `codex-long-task-wakeup` remains a compatibility alias.

## Install

```bash
sudo apt install screen
python3 -m pip install "git+https://github.com/lz59970062/long-task-wakeup.git"
ltc setup --force --enable --now
```

`setup` installs this skill for Codex and Claude Code and installs the callback daemon. It refuses
to proceed when screen cannot be found.

## Run: submit new work

Use this when the agent is launching the long command:

```bash
ltc run \
  --cwd "$PWD" \
  --task "train model" \
  -- python train.py --config configs/exp.yaml
```

The submission is durable before the command returns. Record the printed values. The usual
inspection commands are:

```bash
screen -ls
screen -r ltc-<task-id>
tail -f ~/.codex/long-task-wakeup/tasks/<task-id>/attempt-1.log
```

Detaching with `Ctrl-a d` leaves the task running.

## Done: report externally managed work

When editing an existing script, shell trap, Python `finally`, or scheduler epilogue, use:

```bash
set +e
python train.py --config configs/exp.yaml
status=$?
ltc done \
  --cwd "$PWD" \
  --task "train model" \
  --command "python train.py --config configs/exp.yaml" \
  --exit-code "$status"
exit "$status"
```

`done` only queues delivery. It does not make the preceding task durable. Confirm that an external
process manager already owns that task.

Python pattern:

```python
import subprocess

status = 1
try:
    status = subprocess.call(["python", "train.py", "--config", "configs/exp.yaml"])
finally:
    subprocess.call([
        "ltc", "done",
        "--cwd", "/path/to/project",
        "--task", "train model",
        "--command", "python train.py --config configs/exp.yaml",
        "--exit-code", str(status),
    ])
```

## Session binding

Run from the agent-owned environment and omit target flags by default. LTC detects
`CODEX_THREAD_ID` or `CLAUDE_CODE_SESSION_ID`.

Use `--agent codex|claude --session <id>` when explicit binding is necessary. Use `--last` only as
an unsafe manual fallback; it always warns. If the target cannot be determined, fail instead of
guessing.

## Restart recovery

### Daemon restart

If the recorded screen session is alive, do nothing. The screen-owned task continues and must not
be relaunched. If a durable result exists but its callback is missing, reconstruct the callback
with the same task/callback id.

### Same-host reboot

Screen does not survive a host reboot. Never automatically rerun the command and do not add a
`--resume-command` mechanism. Restore the originally bound Codex or Claude Code conversation with
the task id, screen name, log path, interruption reason, and available checkpoint/artifact context.

The restored agent must:

1. inspect local logs, outputs, processes, manifests, and checkpoints;
2. supplement the completed status if durable artifacts prove completion;
3. otherwise recreate the task through the normal `ltc run` workflow from a valid
   checkpoint;
4. or record the exact blocking condition.

### Cross-host recovery

Do not attempt it. LTC does not transfer code, data, environments, checkpoints, credentials, or
compute resources between hosts.

## Callback acknowledgement

The callback prompt includes:

```bash
ltc ack --queue-dir <queue-dir> --id <callback-id>
```

After inspecting the result and choosing whether to continue, stop, or ask, run that command.
Acknowledgements are monotonic. Missing ACKs retry with backoff. Delivery is at-least-once, so
always inspect existing processes and artifacts before starting follow-up work.
The resumed agent needs write access only to the callback queue. A normal ACK must not create or
modify global target-lock files; if a rare retained lease needs cleanup, the daemon reconciles it
after the durable ACK marker appears.

Callback ACK confirms one delivery only. It must never implicitly complete a persistent goal.

## Goal acknowledgement and automatic inquiry

For a multi-stage goal:

1. Create it once:

   ```bash
   ltc goal start --id <goal-id> --session <session-id> --cwd "$PWD" --task "..."
   ```

2. Bind each `run` or `done` callback with `--goal-id <goal-id>`.
3. If the whole goal is complete:

   ```bash
   ltc goal ack --id <goal-id> --state completed
   ```

4. If progress requires a specific missing condition:

   ```bash
   ltc goal ack --id <goal-id> --state blocked_conditions \
     --condition "specific prerequisite"
   ```

5. When that condition is met:

   ```bash
   ltc goal resume --id <goal-id>
   ```

Completion is terminal. A callback ACK does not count as goal completion.

After three hours without a newly queued ordinary callback, the daemon automatically restores the
same conversation and asks it to continue, ACK completion, or state the exact blocking condition.
The inquiry repeats until the goal is completed or blocked. Reboot recovery callbacks follow the
same goal rules and do not disable this inquiry.

## Multi-round behavior

After each wakeup, inspect relevant logs, metrics, checkpoints, reports, and outputs. Decide:

```text
current_goal:
last_result:
decision: continue | stop_success | stop_blocked | ask_user
reason:
next_command:
budget_remaining:
```

Continue only when the next action is clear, low-risk, aligned with the same goal, and within the
user's budget and project rules. Ask before expanding resource use, changing the scientific or
product objective, deleting artifacts, or making a strategic tradeoff.

When stopping, summarize what was attempted, what changed, and the smallest decision or resource
needed from the user.

## Daemon operations

The systemd user service keeps `ltc daemon` available for submissions, recovery, and callbacks:

```bash
systemctl --user status codex-long-task-wakeup.service
journalctl --user -u codex-long-task-wakeup.service -f
```

The daemon watches `${CODEX_HOME:-~/.codex}/long-task-wakeup/queue` by default. Use `--queue-dir`
or `CODEX_LONG_TASK_WAKEUP_QUEUE_DIR` for an explicit queue.

Before restarting a service or container, drain live callback deliveries where possible.
Unacknowledged interrupted delivery is eligible for replay.

Cancel callbacks through the CLI rather than deleting state:

```bash
ltc cancel --id <callback-id>
ltc cancel --queue-dir <queue-dir> --all --message "no longer needed"
```

`cancel` does not kill a screen-owned task.
