<p align="center">
  <img src="./assets/readme/hero.svg" width="100%" alt="Long Task Callback (ltc): keep a long task in GNU screen and wake the same Codex or Claude Code session when it finishes">
</p>

<p align="center">
  <a href="https://github.com/lz59970062/long-task-wakeup"><img src="https://img.shields.io/badge/python-%E2%89%A53.9-3fb950" alt="Python ≥ 3.9"></a>
  <a href="./pyproject.toml"><img src="https://img.shields.io/badge/license-MIT-58a6ff" alt="MIT license"></a>
  <img src="https://img.shields.io/badge/agents-Codex%20%C2%B7%20Claude%20Code-d29922" alt="Works with Codex and Claude Code">
</p>

**Long Task Callback (ltc)** gives long-running agent work a durable process owner and an explicit
way back to the same conversation.

There are only two normal entry points:

- **Run** — `ltc run -- <command>` submits a new task. The daemon starts it in GNU
  screen, so it is not owned by the agent turn.
- **Done** — `ltc done ...` reports completion of a task that is already owned by
  screen, tmux, Slurm, another scheduler, or an existing script.

The daemon is the control and callback-delivery process. It does not become the parent of the
training process. GNU screen owns the LTC worker and the worker owns the task.

[中文说明](#中文说明) · Formerly `codex-long-task-wakeup` (the old command remains an alias)

## How it works

<p align="center">
  <img src="./assets/readme/workflow.svg" width="100%" alt="The agent submits a task, the daemon starts a GNU screen session, the task runs independently, completion is queued, and the same agent conversation resumes and acknowledges it">
</p>

```text
systemd user service
  └─ ltc daemon                 control, recovery and callback delivery

GNU screen session
  └─ LTC worker
      └─ training / benchmark / build
```

1. Codex or Claude Code submits `ltc run`. LTC persists the command, environment,
   original agent/session binding, goal binding, screen name, and log path.
2. The systemd-managed daemon notices the submission and starts a detached GNU screen session.
3. The screen-owned task runs independently of the agent turn and of daemon restarts.
4. On completion, LTC stores the exit result and queues a callback to the original conversation.
5. The resumed agent inspects the result and runs `ltc ack`. If the callback belongs to a
   multi-stage goal, callback ACK and goal ACK remain separate decisions.

No polling is required. Live inspection is available through screen and the task log.

## Install

GNU screen is required. LTC deliberately has no silent fallback because a fallback would restore
the unstable agent-owned process path.

```bash
sudo apt install screen              # Debian/Ubuntu
# sudo dnf install screen            # Fedora/RHEL

python3 -m pip install "git+https://github.com/lz59970062/long-task-wakeup.git"
ltc setup --force --enable --now
```

`setup` installs the bundled skill for Codex and Claude Code and installs the callback daemon as a
user service. It refuses to continue when GNU screen is unavailable.

Verify:

```bash
ltc --version
screen --version
systemctl --user status codex-long-task-wakeup.service
```

## Run: submit a new long task

```bash
ltc run \
  --cwd "$PWD" \
  --task "train model" \
  -- python train.py --config configs/exp.yaml
```

Submission returns after the task record is durable. It prints:

- the task id;
- the screen session, `ltc-<task-id>`;
- the log file, normally
  `~/.codex/long-task-wakeup/tasks/<task-id>/attempt-1.log`.

Inspect it when useful:

```bash
screen -ls
screen -r ltc-<task-id>
tail -f ~/.codex/long-task-wakeup/tasks/<task-id>/attempt-1.log
```

Detach from screen with `Ctrl-a d`; detaching does not stop the task.

## Done: report an externally managed task

Use `done` when LTC should not launch or own the task:

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

This pattern belongs inside a screen/tmux session, Slurm epilogue, scheduler job, shell trap, or
Python `finally`. `done` only queues the callback; it does not make the preceding process durable.
By default callback failure does not change the task exit code.

## Restart and recovery behavior

### Daemon restart

GNU screen keeps running. After the daemon returns, it discovers the live named session and does
not launch a duplicate. If the task completed while the daemon was unavailable, its durable result
is turned into the normal callback.

### Same-host reboot

GNU screen cannot survive a host reboot. LTC therefore does **not** guess or automatically rerun
the command. After the daemon starts in the new boot, it restores the originally bound Codex or
Claude Code conversation with:

- the task and screen identifiers;
- the local log path;
- the interruption reason;
- instructions to inspect outputs and checkpoints.

That agent then follows the normal task-creation workflow: recreate the run from a valid checkpoint,
supplement the status if artifacts prove it already finished, or record the precise blocking
condition. There is no `--resume-command` interface.

### Cross-host recovery

Cross-host recovery is intentionally unsupported. LTC does not transfer workspaces, datasets,
environments, checkpoints, credentials, or compute allocation to another machine.

## Two acknowledgement layers

Callback delivery ACK confirms that one wakeup was received and inspected:

```bash
ltc ack --queue-dir <queue-dir> --id <callback-id>
```

An unacknowledged callback is retried with backoff. ACK is monotonic.
The resumed agent writes only the queue ACK marker. Global target-lock cleanup is performed by the
daemon, so ACK does not require broader filesystem permissions.

Goal ACK answers a different question: whether the whole multi-stage objective is finished or
cannot proceed:

```bash
ltc goal start --id report-goal --session <session-id> --cwd "$PWD" --task "finish the report"
ltc goal ack --id report-goal --state completed
ltc goal ack --id report-goal --state blocked_conditions --condition "awaiting dataset access"
ltc goal resume --id report-goal
```

Callback ACK never completes the goal. While an active goal has no newly queued ordinary callback,
the daemon automatically asks the same conversation for its status every three hours by default.
The agent must continue, ACK the goal as completed, or record the exact blocked condition. This
inquiry behavior also applies after reboot recovery.

Bind a submitted task or an external completion callback with `--goal-id <goal-id>`.

## Session binding

Inside an agent conversation, LTC normally binds automatically:

- Codex: `CODEX_THREAD_ID`
- Claude Code: `CLAUDE_CODE_SESSION_ID`

Explicit binding is also supported:

```bash
ltc done --agent claude --session <session-id> \
  --cwd "$PWD" --task "external job" --exit-code 0
```

`--last` is an unsafe fallback and always warns. If no target can be determined, LTC fails rather
than guessing.

## Operations and guarantees

```bash
ltc status
ltc cancel --id <callback-id>
ltc install-shell-hook
```

State lives under `${CODEX_HOME:-~/.codex}/long-task-wakeup/`. The queue uses stable callback ids,
durable ACK markers, and per-session locks. Delivery is at-least-once, not exactly-once: after a
host failure, an already received but not durably ACKed callback may be delivered again. Resumed
agents must inspect existing processes and artifacts before launching follow-up work.

The daemon is normally installed as
`~/.config/systemd/user/codex-long-task-wakeup.service`. The daemon may also be run by supervisor
or as a standalone background process in environments without user systemd; GNU screen remains
mandatory for `Run` task ownership in every case.

```bash
systemctl --user status codex-long-task-wakeup.service
journalctl --user -u codex-long-task-wakeup.service -f
```

Old scripts may still pass `--via-daemon`. It is accepted as a hidden no-op compatibility flag;
both `run` and `done` already use the daemon by default. There is no direct agent-owned execution
mode and no fallback to one.

## 中文说明

**Long Task Callback (ltc)** 只有两个需要记住的入口：

- **Run**：`ltc run -- <命令>`。提交一个新任务；daemon 收到记录后，用 GNU
  screen 启动 LTC worker 和训练任务。
- **Done**：`ltc done ...`。任务已经由 screen、tmux、Slurm 或其他调度器托管时，
  只报告结束并投递 callback。

正确的职责关系是：

```text
systemd 用户服务
  └─ ltc daemon                 负责控制、恢复和 callback 投递

GNU screen 会话
  └─ LTC worker
      └─ 训练任务
```

daemon 不是训练任务的父进程。它重启时，screen 中的任务继续运行；daemon 回来后识别已有
screen，不会重复启动。screen 是必需依赖，缺失时 `setup` 和 `run` 都会拒绝，
不会退回到不稳定的 agent 回合进程。

```bash
ltc run --cwd "$PWD" --task "train model" \
  -- python train.py --config configs/exp.yaml
```

命令会打印 task id、`ltc-<task-id>` screen 名和日志路径。用户可以随时检查：

```bash
screen -ls
screen -r ltc-<task-id>
tail -f ~/.codex/long-task-wakeup/tasks/<task-id>/attempt-1.log
```

主机重启后 screen 不会保留。LTC 不自动重跑，而是恢复最初绑定的 Codex 或 Claude Code
会话，把任务、日志、checkpoint 相关上下文和中断原因交回该 agent。agent 自己检查本地产物，
再按标准流程从有效 checkpoint 重建任务、补充完成状态，或记录明确阻塞。没有
`--resume-command`。跨主机恢复不支持，因为它需要额外传输工程、数据、环境、checkpoint 和资源。

两层 ACK 都保留：

1. `ltc ack` 表示本次 callback 已收到并检查；
2. `ltc goal ack --state completed|blocked_conditions` 表示整个阶段目标完成或满足阻塞条件。

callback ACK 不会顺带完成 goal。活跃 goal 默认三小时没有新 callback 时，daemon 会自动恢复
原会话问询阶段状态；重启恢复后也遵守同一规则。
普通 ACK 只写 callback queue；全局 target-lock 的清理由 daemon 完成，不需要给恢复后的
agent 扩大文件系统写权限。

旧脚本中的 `--via-daemon` 仍可作为隐藏的无效果兼容参数使用，但新命令不应再写它。`run`
和 `done` 已经固定走 daemon，不存在直接由 agent 回合执行或投递的模式。

## License

MIT
