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

After pip installation, install the bundled Codex skill and user-level daemon together:

```bash
codex-long-task-wakeup setup --force --enable --now
```

Or use the bundled installer:

```bash
scripts/install_from_git.sh https://github.com/lz59970062/long-task-wakeup.git
```

## Wiring Patterns

Run the callback tool from the Codex-owned process environment and omit the target flag by default.
The CLI must capture `CODEX_THREAD_ID` when the task is launched, bind the callback to that exact
session, and include a `Callback routing` block in every callback prompt showing the bound session
and binding source. This is the normal safe path.

Pass `--session <session-id>` only to override the automatically captured session deliberately.
Never use `--last` automatically. Use it only when the user explicitly accepts that the callback may
resume an unrelated recently active thread. If `CODEX_THREAD_ID` is unavailable and no explicit
`--session` was supplied, the CLI must fail callback setup instead of falling back to `--last`.

Install the Bash pending-status hook once when the user wants terminal-startup reminders:

```bash
codex-long-task-wakeup install-shell-hook
```

Keep the hook idempotent, run it only once per interactive shell environment, use a two-second
timeout, stay silent when the queue is empty, and list only `pending` and `running` callbacks. Keep
it informational: never resume, acknowledge, cancel, or reroute a callback from `.bashrc`. Inspect
failed callbacks explicitly with `codex-long-task-wakeup status --state failed`.

Daemon wrapper form, when Codex launches the command:

```bash
codex-long-task-wakeup run \
  --via-daemon \
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
  --cwd "$PWD" \
  --task "train model" \
  --command "python train.py --config configs/exp.yaml" \
  --exit-code "$status"
exit "$status"
```

For durable daemon handoff, standardize on a user-level systemd service:

```bash
codex-long-task-wakeup setup --force --enable --now
```

Use `codex-long-task-wakeup install-systemd --enable --now` only when the skill is already installed
and only the daemon service needs to be refreshed.

The service runs outside Codex tool sandboxes and keeps `codex-long-task-wakeup daemon` alive with
systemd restart behavior. The installer records the resolved `codex` executable path in
`CODEX_LONG_TASK_WAKEUP_CODEX_BIN` and records the current `PATH` so Codex's runtime dependencies
such as Node/NVM are available under systemd. Resume calls also default to
`-c approvals_reviewer="auto_review"`, `-c approval_policy="on-request"`, and
`-c sandbox_mode="workspace-write"`. Queued callbacks also add their queue directory to
`sandbox_workspace_write.writable_roots`, so the resumed agent can write acknowledgement markers
instead of stalling in read-only mode. Use `--codex-bin /path/to/codex` or `--path "$PATH"` if
discovery is not correct. Inspect it with:

```bash
systemctl --user status codex-long-task-wakeup.service
journalctl --user -u codex-long-task-wakeup.service -f
```

For proxy-only environment injection, use `setup --proxy-env-file ~/.codex/.env` or
`setup --inherit-proxy`. The installer copies only the standard upper- and lowercase proxy variables
to `${CODEX_HOME:-~/.codex}/long-task-wakeup/service-proxy.env` with mode `0600`, references it with
systemd `EnvironmentFile=`, and never prints its values or writes them into supervisor configuration.
A plain `.env` file is not otherwise inherited by systemd; use `--clear-proxy` to remove saved proxy
variables. Since 0.4.3, `systemctl --user reload codex-long-task-wakeup.service` requests a safe
hot reload: a daemon waits for live delivery workers to exit before re-execing. During a legacy daemon
upgrade, `setup --now` refuses to restart while `running/` callbacks exist; drain those callbacks first.

If `systemctl` is unavailable, as in many Docker containers, `setup --force --enable --now` prefers
supervisor when `supervisorctl` or `supervisord` is installed. It writes
`/etc/supervisor/conf.d/codex-long-task-wakeup.conf` with `autostart=true` and `autorestart=true`.
Inspect `supervisorctl status codex-long-task-wakeup` and
`${CODEX_HOME:-~/.codex}/long-task-wakeup/supervisor.log`. For durable container restarts, pair this
with Docker's `restart: unless-stopped`. If supervisor is missing, setup falls back to a standalone
background daemon; inspect `${CODEX_HOME:-~/.codex}/long-task-wakeup/daemon.pid` and
`${CODEX_HOME:-~/.codex}/long-task-wakeup/daemon.log`.

Use this foreground form only for debugging:

```bash
codex-long-task-wakeup daemon
```

The daemon watches `${CODEX_HOME:-~/.codex}/long-task-wakeup/queue` by default. Use `--queue-dir`
or `CODEX_LONG_TASK_WAKEUP_QUEUE_DIR` when a different queue location is needed.

Daemon callbacks are acknowledged by the resumed agent. The callback prompt includes a command like:

```bash
codex-long-task-wakeup ack --queue-dir <queue-dir> --id <callback-id>
```

After the agent has inspected the long-task result and decided whether to continue, stop, or ask the
user, it must run that acknowledgement command. Acknowledgements are monotonic. An acknowledged
resume may remain in `running/` until its Codex process exits so its per-session delivery lease stays
active; the daemon may deliver callbacks for other sessions, but must not overlap another callback
for the same session. In CLI 0.4.2 or newer this lease is global across queue directories under the
same `CODEX_HOME`; never configure different `CODEX_LONG_TASK_WAKEUP_TARGET_LOCK_DIR` values for
daemons that can target the same session. A dedicated delivery worker owns the per-request and global
target locks and enforces the timeout; Codex and its descendants must not inherit the locks. If the
marker is missing, the daemon retries 3 times by
default with increasing delays; tune this with `codex-long-task-wakeup daemon --retries 3
--retry-delay 30 --retry-backoff 2` or the same flags on `install-systemd`.

`run --via-daemon` must use the durable lifecycle built into CLI 0.4.1 or newer. It persists one
stable callback id in `active/` before starting the wrapped command. If the wrapper disappears, the
daemon recovers that id with `outcome: unknown`; the child may still be running. On every unknown
outcome, inspect the process table, logs, checkpoints, output manifests, and other artifacts before
deciding whether to rerun. Never infer task failure from wrapper loss alone. If the record already
contains a completed exit code, trust the preserved completed outcome.

Treat delivery as at-least-once, not exactly-once. Stable ids, acknowledgements, and delivery locks
reduce duplicates, but a host failure before the acknowledgement is durable can still retry a
received prompt. Make continuation actions idempotent and check whether the next task is already
running or complete before launching it. Reboot recovery requires a persistent queue and a daemon
service that starts after reboot.

Before restarting a managed systemd service or container, drain `running/` callbacks when possible.
Service managers generally terminate delivery workers with the daemon, so an interrupted delivery
without a durable acknowledgement is eligible for at-least-once replay after restart.

If pre-arming emits `UNARMED WARNING`, report the degraded guarantee. Default mode preserves the
wrapped command and falls back to post-exit best effort; use `--strict` only when the task must not
start without durable callback protection. `status --state active` is diagnostic; keep the shell
startup hook limited to `pending` and `running`.

If the user decides a queued callback is no longer needed before it fires or before its retry window
finishes, cancel it instead of deleting queue files manually:

```bash
codex-long-task-wakeup cancel --id <callback-id>
codex-long-task-wakeup cancel --queue-dir <queue-dir> --id <callback-id>
codex-long-task-wakeup cancel --queue-dir <queue-dir> --all --message "no longer needed"
```

`cancel` publishes a tombstone for active requests in `active/`, `pending/`, or `running/`. It does
not kill an already-started wrapped task or Codex resume process, but it suppresses callback
finalization and later retries.

If a callback reaches `running/` but `ack` fails with `OSError: [Errno 30] Read-only file system`,
check the resumed Codex header. If it shows `sandbox: read-only`, `approval: never`, or the queue
directory is missing from writable roots, treat this as an installation/service issue rather than a
task failure. Refresh with `python3 -m pip install --upgrade --force-reinstall
"git+https://github.com/lz59970062/long-task-wakeup.git"` and then
`codex-long-task-wakeup setup --force --enable --now`. Inspect
`systemctl --user status codex-long-task-wakeup.service` and
`journalctl --user -u codex-long-task-wakeup.service -n 100 --no-pager`; if the next step is still
unclear, report the exact callback id, queue path, sandbox header, and last daemon log lines to the
user.

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

## Goal Completion Acknowledgement

Use this workflow for a multi-stage goal that must not silently stop:

1. Create it once: `codex-long-task-wakeup goal start --id <goal-id> --session <session-id> --cwd "$PWD" --task "..."`.
2. Bind each ordinary `run` or `done` callback to it with `--goal-id <goal-id>`. After three hours
   without a newly queued ordinary callback, the daemon asks that same session to continue, ACK
   completion, or state the exact missing condition; it repeats at the same interval until the goal
   is ACKed or blocked.
3. When the whole goal is done, immediately run `goal ack --id <goal-id> --state completed`. This
   is terminal and permanently suppresses further goal reminders; `goal resume` cannot reopen it.
4. If work cannot proceed, run `goal ack --id <goal-id> --state blocked_conditions --condition
   "specific prerequisite"`. This suppresses session reminders. Once the prerequisite is met, use
   `goal resume --id <goal-id>`; do not leave an unexplained blocked state.

Blocked-email escalation is opt-in. Configure one trusted recipient in the daemon environment with
`CODEX_LONG_TASK_WAKEUP_BLOCKED_EMAIL_TO`, then pass that exact value as `--email-to` when recording
the blocked condition. The value must be one mailbox, not a list. After 12 hours the daemon attempts local `sendmail` delivery up to three times;
the email contains only the goal title, blocked condition, and goal id, and records local-MTA
acceptance rather than recipient delivery.
