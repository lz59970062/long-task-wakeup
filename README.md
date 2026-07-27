<p align="center">
  <img src="./assets/readme/hero.svg" width="100%" alt="Long Task Callback (ltc): wake Codex or Claude Code back into the same session when an hours-long task finishes">
</p>

<p align="center">
  <a href="https://github.com/lz59970062/long-task-wakeup"><img src="https://img.shields.io/badge/python-%E2%89%A53.9-3fb950" alt="Python ≥ 3.9"></a>
  <a href="./pyproject.toml"><img src="https://img.shields.io/badge/license-MIT-58a6ff" alt="MIT license"></a>
  <img src="https://img.shields.io/badge/agents-Codex%20%C2%B7%20Claude%20Code-d29922" alt="Works with Codex and Claude Code">
</p>

**Long Task Callback (ltc)** turns long-running agent jobs into self-returning workflows.

Coding agents are great at kicking off expensive work: model training, benchmarks, large test
suites, data pipelines, builds, deployments, simulations. The awkward part is what happens hours
later: the command finishes, but the original reasoning loop has gone cold. ltc solves that with
an explicit callback. The agent writes one small command into the task's exit path; when the task
finishes, that command resumes the *same* agent session — Codex or Claude Code — and asks it to
inspect the result, decide whether the goal is complete, and continue when the next step is clear.

By default there is no polling and no daemon. Nothing runs unless your task explicitly calls it.

[中文说明](#中文说明) · Formerly `codex-long-task-wakeup` (renamed in 0.5.0 — the old command still works)

## How it works

<p align="center">
  <img src="./assets/readme/workflow.svg" width="100%" alt="Five steps: the agent launches a long task, it runs unattended, the exit path fires an ltc callback, the daemon queues and delivers it, and the same session resumes to inspect, acknowledge, and continue">
</p>

1. The agent launches a long task through `ltc run`, or writes `ltc done` into a script,
   `finally` block, or job epilogue. The callback is bound to the launching session automatically
   (`CODEX_THREAD_ID` for Codex, `CLAUDE_CODE_SESSION_ID` for Claude Code).
2. The task runs unattended. No polling, no watcher.
3. When the task exits, the callback fires. With `--via-daemon` it lands in a durable on-disk queue
   instead of resuming the agent from inside a restricted tool sandbox.
4. A user-level daemon delivers the callback: `codex exec resume --all` for Codex targets,
   `claude -p --resume` for Claude Code targets — always the originally bound session.
5. The resumed agent inspects artifacts, acknowledges with `ltc ack`, and either continues the goal
   or stops with a clear reason. Unacknowledged callbacks retry with backoff; the loop can repeat
   across stages until a persistent goal is ACKed complete.

## Highlights

- **Explicit by design** — only activates when written into the task command or code.
- **Two agents, one tool** — auto-detects Codex vs Claude Code and resumes each natively.
- **Same-session handoff** — the reasoning that started the work is the reasoning that finishes it.
- **Durable queue** — callbacks survive wrapper kills, daemon restarts, and reboots.
- **Acknowledged delivery** — unacknowledged wakeups retry (default 3×: 30s, 60s, 120s).
- **Persistent goals** — multi-stage work is reminded every 3h until it is ACKed complete or blocked.
- **Non-interfering** — callback failure never changes the task's exit code by default.
- **Tiny surface area** — one Python package, one CLI (`ltc`).

## Install

```bash
python3 -m pip install "git+https://github.com/lz59970062/long-task-wakeup.git"
```

Then install the bundled agent skill (for both Codex and Claude Code) and the wakeup daemon:

```bash
ltc setup --force --enable --now
```

This installs the `long-task-callback` skill into `~/.codex/skills` and `~/.claude/skills`
(control with `--skill-target codex|claude|both`), writes a user-level systemd service, and starts
it. In Docker containers without `systemctl`, setup prefers supervisor and falls back to a
standalone background daemon.

Verify:

```bash
ltc --version
ltc --help
systemctl --user status codex-long-task-wakeup.service
```

> The legacy `codex-long-task-wakeup` command and systemd unit name are kept for backward
> compatibility. If `ltc --help` looks stale after an upgrade, run `hash -r` and check
> `which -a ltc` for older installs earlier on your `PATH`.

## Quick start

### Wrap a long command

```bash
ltc run \
  --via-daemon \
  --cwd "$PWD" \
  --task "train model" \
  -- python train.py --config configs/exp.yaml
```

The wrapper returns the wrapped command's exit code. When the command exits, the bound agent
session is woken to inspect the result.

### Add a callback to existing code

```bash
set +e
python train.py --config configs/exp.yaml
status=$?
ltc done \
  --via-daemon \
  --cwd "$PWD" \
  --task "train model" \
  --command "python train.py --config configs/exp.yaml" \
  --exit-code "$status"
exit "$status"
```

`ltc done` returns `0` by default even if the wakeup fails, so `exit "$status"` remains the single
source of truth for the task result.

### Session binding

Run from inside an agent session and no flags are needed — ltc binds the callback to the current
session automatically. To bind explicitly:

```bash
ltc done --agent claude --session <session-id> --via-daemon --cwd "$PWD" --task "..." --exit-code 0
```

`--last` exists as an unsafe fallback (resume whatever session was most recent) and always warns.
If no session can be determined, ltc fails rather than guessing.

## Persistent goals

For multi-stage work that must keep moving until it is explicitly finished, create one goal and
bind every callback to it:

```bash
ltc goal start --id report-goal --session <session-id> --cwd "$PWD" --task "finish the report"
ltc done --via-daemon --goal-id report-goal --cwd "$PWD" --task "run report checks" \
  --command "python check_report.py" --exit-code 0
```

If no new callback is queued for three hours, the daemon resumes the session and asks it to
continue, ACK completion, or record the exact blocking condition — repeating until the goal is
ACKed:

```bash
ltc goal ack --id report-goal --state completed
ltc goal ack --id report-goal --state blocked_conditions --condition "awaiting dataset access"
ltc goal resume --id report-goal
```

Completion is terminal. An optional email escalation fires after 12h blocked when
`CODEX_LONG_TASK_WAKEUP_BLOCKED_EMAIL_TO` is configured in the daemon environment.

## Managing callbacks

```bash
ltc status                                  # pending and running callbacks
ltc cancel --id <callback-id>               # tombstone a callback before it fires or retries
ltc install-shell-hook                      # terminal-startup reminder for pending callbacks
```

The daemon's state lives under `${CODEX_HOME:-~/.codex}/long-task-wakeup/` (queue, target locks,
logs — the directory name predates the rename and is kept for compatibility).

## Guarantees and limits

- **At-least-once, not exactly-once.** Stable ids, monotonic ACKs, and global per-session locks
  suppress ordinary duplicates, but a host failure between prompt delivery and a durable ACK can
  still cause a retry. Callback code must inspect existing processes and artifacts before
  relaunching work.
- **Unknown outcomes stay unknown.** If the wrapper is killed before recording completion, the
  daemon recovers the callback with `outcome: unknown` and an explicit warning that the child may
  still be running. Never infer task failure from wrapper loss alone.
- **Codex-specific knobs.** `--approvals-reviewer`, `--approval-policy`, `--sandbox-mode`, and the
  Codex Desktop App Server delivery path only apply to Codex targets.
- **Claude Code knobs.** The resume uses `--permission-mode auto` by default; override per callback
  with `--permission-mode` or globally with `LONG_TASK_WAKEUP_CLAUDE_PERMISSION_MODE`. The daemon
  resolves the binary from `LONG_TASK_WAKEUP_CLAUDE_BIN` (recorded by `setup --claude-bin`).
- **Interactive sessions.** Claude Code wakeups run headless (`claude -p --resume`). The injected
  turn becomes part of the session transcript and is visible the next time you open it.

<details>
<summary><strong>Advanced: durable run lifecycle, retries, and locks</strong></summary>

`ltc run --via-daemon` arms one stable callback id in `active/` before launching the wrapped
command, guarded by a close-on-exec owner lock that distinguishes a healthy wrapper from an
orphaned record. Normal completion records the real exit code and moves the same id to `pending/`;
if pre-arming cannot persist, default mode prints an `UNARMED WARNING` and falls back to post-exit
best effort, while `--strict` refuses to launch (exit 125).

Each delivery is owned by a dedicated worker holding the per-request lock, a global per-session
target lock (shared across all queue directories under the same state home), and the timeout — the
resumed agent process never inherits the locks. A contender that loses the cross-queue lease race
returns to `pending/` without consuming a retry. Drain `running/` callbacks before restarting the
service or container when possible; interrupted unacknowledged deliveries replay at-least-once.

Tune retries with:

```bash
ltc install-systemd --retries 3 --retry-delay 30 --retry-backoff 2 --enable --now
```

</details>

<details>
<summary><strong>Advanced: daemon operations (systemd, supervisor, standalone, proxy)</strong></summary>

`ltc setup --force --enable --now` writes `~/.config/systemd/user/codex-long-task-wakeup.service`,
then reloads, enables, and safely activates it. A daemon that advertises the hot-reload protocol
receives HUP and re-execs updated code only after live delivery workers exit. Inspect with:

```bash
systemctl --user status codex-long-task-wakeup.service
journalctl --user -u codex-long-task-wakeup.service -f
```

In Docker, the same command prefers supervisor (`/etc/supervisor/conf.d/codex-long-task-wakeup.conf`,
`autostart=true`, `autorestart=true`) and falls back to a standalone daemon
(`daemon.pid`/`daemon.log` in the state directory). Pair with Docker's `restart: unless-stopped`
for container-level restarts. To keep user services alive across logout: `loginctl enable-linger "$USER"`.

To persist proxy settings for the daemon, copy only proxy variables into a private env file:

```bash
ltc setup --force --enable --now --proxy-env-file ~/.codex/.env   # or --inherit-proxy
```

Only `HTTP_PROXY`, `HTTPS_PROXY`, `ALL_PROXY`, `NO_PROXY` (and lowercase variants) are kept, in
`service-proxy.env` with mode `0600`; credentials never enter the unit, supervisor config, or logs.
Use `--clear-proxy` to remove them.

</details>

<details>
<summary><strong>Advanced: Codex Desktop App Server delivery</strong></summary>

When Codex Desktop's local App Server is available, the daemon first delivers a callback to the
existing visible desktop task, holding the session lease until the turn completes. If `turn/start`
was possibly accepted but its outcome is unknown, the callback moves to `failed` with a retained
global lease rather than risking a duplicate — inspect the task, then `ltc cancel` to release the
lease before scheduling a replacement. The socket is trusted only within the same Unix user account
(peer UID verified on Linux). Set `CODEX_LONG_TASK_WAKEUP_DESKTOP_APP_SERVER=0` in a systemd
user-service override to force the CLI-only path.

</details>

<details>
<summary><strong>Troubleshooting</strong></summary>

Refresh the install and service:

```bash
python3 -m pip install --upgrade --force-reinstall \
  "git+https://github.com/lz59970062/long-task-wakeup.git"
hash -r
ltc setup --force --enable --now
```

Then send a smoke callback:

```bash
ltc done --via-daemon --last \
  --cwd "$PWD" \
  --task "ltc smoke test" \
  --command "manual smoke test" \
  --exit-code 0 \
  --message "Please acknowledge this callback and report whether it reached done."
```

Success means the same callback id appears in both `acks/` and `done/`. If a callback reaches
`running/` but no ACK lands, check the daemon log and (for Codex targets) that the resumed session
shows `sandbox: workspace-write` with the queue directory in its writable roots. Inspect exhausted
callbacks with `ltc status --state failed`.

</details>

## The bundled skill

The repository ships a `long-task-callback` skill for both agents (`SKILL.md`, plus
`agents/openai.yaml` for Codex). It teaches the agent when and how to wire explicit callbacks into
long-running tasks: daemon handoff by default, session binding from the environment, ACK after
inspection, and when to stop and ask instead of continuing.

---

## 中文说明

**Long Task Callback (ltc)** 让长时间运行的 agent 任务在结束后，主动把原来的会话叫回来。

编程 agent 很擅长启动耗时任务：模型训练、benchmark、大型测试、数据处理、构建、部署、仿真。
尴尬的是几小时以后：命令结束了，但原来的推理上下文已经冷掉了。ltc 用一个显式 callback
解决这个问题：agent 在任务的结束路径里写入一条小命令；任务结束时，这条命令恢复*同一个*
agent 会话——Codex 或 Claude Code——让它检查结果、判断目标是否完成，并在下一步明确时继续。

默认没有轮询、没有 daemon。只有任务代码主动调用时才会启用。

### 安装

```bash
python3 -m pip install "git+https://github.com/lz59970062/long-task-wakeup.git"
ltc setup --force --enable --now
```

`setup` 会把 `long-task-callback` 技能同时装进 `~/.codex/skills` 和 `~/.claude/skills`
（可用 `--skill-target codex|claude|both` 控制），并安装用户级 systemd 服务。Docker 里
没有 `systemctl` 时优先用 supervisor，再退化为独立后台 daemon。旧命令
`codex-long-task-wakeup` 仍然保留。

### 用法

包装一个长命令（推荐 `--via-daemon`，由 daemon 在工具沙箱外唤醒）：

```bash
ltc run --via-daemon --cwd "$PWD" --task "train model" \
  -- python train.py --config configs/exp.yaml
```

或把 callback 写进已有脚本、`finally`、Slurm epilogue：

```bash
set +e
python train.py --config configs/exp.yaml
status=$?
ltc done --via-daemon --cwd "$PWD" --task "train model" \
  --command "python train.py --config configs/exp.yaml" --exit-code "$status"
exit "$status"
```

在 agent 会话内运行时无需任何目标参数：ltc 自动绑定当前会话（Codex 读
`CODEX_THREAD_ID`，Claude Code 读 `CLAUDE_CODE_SESSION_ID`）。显式绑定时用
`--agent claude --session <id>`；`--last` 是不安全兜底，总会告警。`ltc done` 默认返回
`0`，唤醒失败不影响任务退出码。

### 持久目标

多阶段任务可以创建一个 goal，并把每条 callback 绑上去：

```bash
ltc goal start --id report-goal --session <session-id> --cwd "$PWD" --task "finish the report"
```

三小时没有新 callback 时，daemon 会恢复会话，要求继续、ACK 完成或记录阻塞条件，直到目标
被 ACK：`ltc goal ack --id report-goal --state completed`。

### 保证与限制

- **at-least-once，不是 exactly-once**：稳定 id、单调 ACK 和全局会话锁能消除常见重复，但
  主机故障仍可能造成重投；恢复后的 agent 必须先检查进程和产物再继续。
- **wrapper 被杀 ≠ 任务失败**：daemon 会以 `outcome: unknown` 恢复该 callback，并明确提示
  子进程可能还在运行。
- **Codex 专属**：`--approval-policy` 等 `-c` 配置和 Desktop App Server 投递只对 Codex 生效。
- **Claude Code 专属**：默认 `--permission-mode auto`，可用 `--permission-mode` 或
  `LONG_TASK_WAKEUP_CLAUDE_PERMISSION_MODE` 调整；唤醒以 headless 方式
  （`claude -p --resume`）进行，注入的回合会留在会话记录里。
- 未 ACK 的 callback 默认重试 3 次（30s、60s、120s）；`ltc cancel --id <id>` 可随时取消。

更多细节（durable 生命周期、daemon 运维、代理持久化、排查步骤）见上方英文版的折叠章节。

## License

MIT
