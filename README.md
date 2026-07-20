# Long Task Wakeup

**Long Task Wakeup turns long-running Codex jobs into self-returning workflows.**

Codex is great at kicking off expensive work: model training, benchmarks, large test suites,
data pipelines, builds, deployments, simulations. The awkward part is what happens hours later:
the command finishes, but the original reasoning loop has gone cold.

Long Task Wakeup solves that with an explicit callback. Codex writes one small command into the
task's exit path. When the task finishes, that command resumes the original Codex session and asks
it to inspect the result, decide whether the goal is complete, and continue if the next step is
clear.

By default there is no polling and no daemon. Nothing runs unless your task explicitly calls it.
For recursive or multi-stage workflows, the same callback can be queued for a small user-started
daemon so nested Codex tool sandboxes do not have to launch more Codex processes themselves.

## Highlights

- **Explicit by design**: only activates when written into the task command or code.
- **Good for overnight work**: training, evals, benchmarks, deployments, data jobs, long tests.
- **Same-session handoff**: resumes Codex with `codex exec resume`.
- **Daemon handoff when needed**: `--via-daemon` queues the wakeup so an external daemon launches Codex.
- **Acknowledged callbacks**: daemon wakeups require the resumed agent to mark success with `ack`.
- **Bounded retries**: unacknowledged daemon wakeups retry 3 times by default with increasing delays.
- **Cancelable queue items**: queued or retrying daemon callbacks can be moved to `canceled/`.
- **Non-interfering**: callback failure never changes the task exit code by default.
- **Tiny surface area**: one Python package, one CLI command.
- **No logs required**: pass task name, command, exit code, cwd, and optional message.

## Install

Install directly from GitHub over HTTPS:

```bash
python3 -m pip install "git+https://github.com/lz59970062/long-task-wakeup.git"
```

Or clone and install locally:

```bash
git clone https://github.com/lz59970062/long-task-wakeup.git
cd long-task-wakeup
python3 -m pip install .
```

After installation, the global command is:

```bash
codex-long-task-wakeup
```

Verify that the installed command and the installed Python module are the same new package:

```bash
python -m long_task_callback --version
python -m long_task_callback --help
codex-long-task-wakeup --version
codex-long-task-wakeup --help
```

The help output must include:

```text
daemon
install-systemd
install-skill
setup
ack
cancel
```

If `python -m long_task_callback --help` shows `install-systemd` but
`codex-long-task-wakeup --help` does not, your shell is executing an older console script from
another environment or earlier `PATH` entry. Inspect and refresh it with:

```bash
which -a codex-long-task-wakeup
hash -r
python -m pip install --upgrade --force-reinstall --no-cache-dir \
  "git+https://github.com/lz59970062/long-task-wakeup.git"
```

Install the bundled Codex skill and user-level daemon together:

```bash
codex-long-task-wakeup setup --force --enable --now
```

Or install only the bundled Codex skill into `~/.codex/skills`:

```bash
codex-long-task-wakeup install-skill
```

If you use a custom Codex home, set `CODEX_HOME` or pass `--path`:

```bash
CODEX_HOME=/path/to/.codex codex-long-task-wakeup install-skill
codex-long-task-wakeup install-skill --path /path/to/.codex/skills
```

## Usage

### Wrap A Long Command

Use `run` when Codex can launch the long task through the wrapper:

```bash
codex-long-task-wakeup run \
  --session <session-id> \
  --cwd "$PWD" \
  --task "train model" \
  -- python train.py --config configs/exp.yaml
```

The wrapper returns the wrapped command's exit code, then wakes Codex.

The wakeup step is best-effort. If Codex cannot be resumed, the wrapped command's exit code is
still preserved.

For recursive or multi-stage workflows, queue the wakeup for an external daemon:

```bash
codex-long-task-wakeup run \
  --via-daemon \
  --session <session-id> \
  --cwd "$PWD" \
  --task "train model" \
  -- python train.py --config configs/exp.yaml
```

### Add A Callback To Existing Code

Use `done` when Codex writes the callback into a shell script, Python `finally` block,
Slurm epilogue, or other task exit path:

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

The callback command returns `0` by default even if Codex cannot be resumed, so the final
`exit "$status"` remains the source of truth for the task result.

Use `--via-daemon` for callbacks that may be called from inside a resumed Codex turn:

```bash
codex-long-task-wakeup done \
  --via-daemon \
  --session <session-id> \
  --cwd "$PWD" \
  --task "train model" \
  --command "python train.py --config configs/exp.yaml" \
  --exit-code "$status"
```

You can also set `CODEX_LONG_TASK_WAKEUP_VIA_DAEMON=1` instead of passing `--via-daemon`.

### Run The Wakeup Daemon

The standard long-lived setup is a user-level systemd service:

```bash
codex-long-task-wakeup setup --force --enable --now
```

This writes:

```text
~/.config/systemd/user/codex-long-task-wakeup.service
```

and runs `systemctl --user daemon-reload`, `enable`, and `restart`. The service keeps
`codex-long-task-wakeup daemon` alive outside Codex tool sandboxes and restarts it if it exits.
In Docker or minimal containers where `systemctl` is not available, the same `setup --force --enable
--now` command prefers supervisor when `supervisorctl` or `supervisord` is installed. It writes:

```text
/etc/supervisor/conf.d/codex-long-task-wakeup.conf
${CODEX_HOME:-~/.codex}/long-task-wakeup/supervisor.log
```

The supervisor program uses `autostart=true` and `autorestart=true`, which keeps the daemon running
while `supervisord` is alive. Pair this with Docker's own `--restart unless-stopped` or Compose
`restart: unless-stopped` for container-level restart behavior. If supervisor is not installed,
setup falls back to a standalone background daemon. In that fallback mode `--enable` has no effect
because there is no init system; `--now` starts the daemon immediately and writes:

```text
${CODEX_HOME:-~/.codex}/long-task-wakeup/daemon.pid
${CODEX_HOME:-~/.codex}/long-task-wakeup/daemon.log
```

The installer also records the resolved `codex` executable path in
`CODEX_LONG_TASK_WAKEUP_CODEX_BIN` and the current `PATH`, so the daemon can find `codex` and its
runtime dependencies such as Node/NVM outside an interactive shell. Resume calls include
`-c approvals_reviewer="auto_review"`, `-c approval_policy="on-request"`, and
`-c sandbox_mode="workspace-write"` by default. The queued callback also adds its queue directory to
`sandbox_workspace_write.writable_roots`, so the resumed agent can write acknowledgement markers
instead of getting stuck in read-only mode.

Inspect or manage it with:

```bash
systemctl --user status codex-long-task-wakeup.service
journalctl --user -u codex-long-task-wakeup.service -f
systemctl --user restart codex-long-task-wakeup.service
systemctl --user stop codex-long-task-wakeup.service
```

In Docker supervisor mode, inspect supervisor first:

```bash
supervisorctl status codex-long-task-wakeup
tail -f "${CODEX_HOME:-$HOME/.codex}/long-task-wakeup/supervisor.log"
```

If supervisor is not installed and setup used the standalone fallback, inspect:

```bash
cat "${CODEX_HOME:-$HOME/.codex}/long-task-wakeup/daemon.pid"
tail -f "${CODEX_HOME:-$HOME/.codex}/long-task-wakeup/daemon.log"
```

To review the generated unit before installing:

```bash
codex-long-task-wakeup install-systemd --print
```

If the skill is already installed and you only need the daemon service, use:

```bash
codex-long-task-wakeup install-systemd --enable --now
```

If `codex` is installed in a non-standard location, pass it explicitly:

```bash
codex-long-task-wakeup install-systemd --codex-bin /path/to/codex --enable --now
```

If the daemon needs a custom runtime path, pass it explicitly:

```bash
codex-long-task-wakeup install-systemd --path "$PATH" --enable --now
```

You can still run the daemon in the foreground for debugging:

```bash
codex-long-task-wakeup daemon
```

The daemon watches `${CODEX_HOME:-~/.codex}/long-task-wakeup/queue` by default. Override this with
`--queue-dir` or `CODEX_LONG_TASK_WAKEUP_QUEUE_DIR`:

```bash
codex-long-task-wakeup daemon --queue-dir /path/to/queue
```

For tests or batch processing, process currently queued items and exit:

```bash
codex-long-task-wakeup daemon --once
```

When the daemon resumes Codex, the callback prompt includes an acknowledgement command:

```bash
codex-long-task-wakeup ack --queue-dir <queue-dir> --id <callback-id>
```

The resumed agent should run that command after it has inspected the long-task result and decided
what to do next. The acknowledgement marker is monotonic: the daemon never removes it. While the
resumed Codex process is still alive, the request can remain in `running/` as a per-target delivery
lease; callbacks for other sessions may proceed, but another callback for the same session waits.
Since 0.4.2 that target lease is global across every queue directory under the same `CODEX_HOME`, so
independent daemons watching a default and a dedicated queue cannot resume the same session at the
same time. The request moves to `done/` after the acknowledged resume process exits. A dedicated
delivery worker owns both the per-request and global target leases plus the timeout; the Codex
process and its descendants do not inherit the locks. This lets a directly crashed daemon restart
without duplicating a still-live delivery. If Codex
exits without an acknowledgement marker, the daemon retries the callback 3 times by default with
delays of 30s, 60s, and 120s. Tune this with:

```bash
codex-long-task-wakeup daemon --retries 3 --retry-delay 30 --retry-backoff 2
codex-long-task-wakeup install-systemd --retries 3 --retry-delay 30 --retry-backoff 2 --enable --now
```

If a queued callback is no longer needed before it fires or before its retry window finishes, cancel
it instead of deleting queue files manually:

```bash
codex-long-task-wakeup cancel --id <callback-id>
codex-long-task-wakeup cancel --queue-dir <queue-dir> --id <callback-id>
codex-long-task-wakeup cancel --queue-dir <queue-dir> --all --message "no longer needed"
```

Canceling publishes a durable tombstone for requests in `active/`, `pending/`, or `running/`, then
removes dispatchable copies. If the wrapped task or a Codex resume has already started, `cancel`
does not kill that process; it only suppresses callback finalization and later retries.

### Durable `run --via-daemon` lifecycle

Version 0.4.1 arms one stable callback id in `active/` before launching the wrapped command. A
close-on-exec owner lock distinguishes a healthy wrapper from an orphaned record. Normal completion
updates that same id with the real exit code and moves it to `pending/`. If the wrapper is killed,
the daemon recovers the same id with `outcome: unknown` and an explicit warning that the wrapped
child may still be running. Always inspect processes and artifacts before rerunning an
unknown-outcome task.

If completion was recorded but the final state move was interrupted, recovery preserves the known
exit code instead of downgrading it to unknown. File replacement and state transitions sync both the
record and relevant queue directories. Reboot recovery still requires the queue to live on
persistent storage and the daemon service to start after reboot.

Version 0.4.2 stores hashed per-target lock files in
`${CODEX_HOME:-~/.codex}/long-task-wakeup/target-locks`. Override that location with
`CODEX_LONG_TASK_WAKEUP_TARGET_LOCK_DIR` only when every daemon that may target the same session uses
the same override. A contender that loses the cross-queue lease race returns to `pending/` without
consuming a retry.

Drain `running/` callbacks before a managed service or container restart when possible. A service
manager normally terminates the daemon's delivery workers too; an unacknowledged interrupted
delivery is intentionally eligible for at-least-once replay after restart.

This is an at-least-once callback protocol, not exactly-once execution. Stable ids, monotonic
acknowledgements, a singleton daemon lock, and per-delivery locks suppress ordinary duplicates, but
a host failure after Codex receives a prompt and before its acknowledgement is durable can still
cause a retry. Callback code must therefore inspect existing processes and artifacts before
relaunching work.

If pre-arming cannot be persisted, default mode prints a prominent `UNARMED WARNING`, runs the task,
and falls back to legacy post-exit best effort so the wrapped command keeps its historical behavior.
Use `--strict` when the command must not start without durable callback protection; strict mode exits
with code 125 before launch. Inspect healthy wrappers explicitly with
`codex-long-task-wakeup status --state active`; the shell startup hook intentionally continues to
show only `pending/` and `running/`.

If user services should survive logout on your machine, enable linger once:

```bash
loginctl enable-linger "$USER"
```

### Python Finally Example

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

## How It Works

`codex-long-task-wakeup done` builds a small prompt containing:

- task name
- working directory
- command
- exit code
- optional message
- callback routing details, including the bound session id and binding source

When neither `--session` nor `--last` is supplied, the CLI automatically binds the callback to the
launching Codex thread through `CODEX_THREAD_ID`. If that variable is unavailable, setup fails
instead of selecting another conversation. `--session` remains an explicit override. `--last` is an
unsafe compatibility fallback and always emits a warning.

By default it runs:

```bash
codex exec resume --all <session-id> -
```

The prompt is sent to the resumed Codex session through stdin. Codex can then inspect artifacts,
metrics, checkpoints, generated files, or test reports and decide the next step.

With `--via-daemon`, `run` and `done` write the same prompt into an atomic JSON queue item instead.
`codex-long-task-wakeup daemon` later reads that item and runs `codex exec resume --all ... -`
from the daemon's own environment. This avoids recursive `resume -> tool sandbox -> resume`
chains, where nested Codex processes can inherit restricted filesystem or network permissions.

## Non-Interference Guarantee

Task execution and Codex wakeup are intentionally separated:

- `run` mode returns the wrapped task's exit code.
- `done` mode returns `0` by default so callback failure does not break shell epilogues.
- Callback failures and daemon enqueue failures are warnings on stderr, not task failures.
- Use `--strict` only if you explicitly want callback failure to propagate.

Use `--last` only when resuming the most recent Codex session is explicitly acceptable:

```bash
codex-long-task-wakeup done --last --cwd "$PWD" --task "long eval" --exit-code "$status"
```

## Terminal Pending-Task Reminder

Install an idempotent Bash startup hook:

```bash
codex-long-task-wakeup install-shell-hook
```

On interactive terminal startup it checks the local callback queue for `pending` and `running`
items, prints a compact summary only when needed, and stops after two seconds. It never resumes or
changes a callback. Run `codex-long-task-wakeup status` manually for the same summary, or
`codex-long-task-wakeup status --state failed` for exhausted callbacks.

## Health Check And Troubleshooting

After setup, verify the installed CLI, skill, and daemon:

```bash
codex-long-task-wakeup --version
codex-long-task-wakeup --help
systemctl --user status codex-long-task-wakeup.service
journalctl --user -u codex-long-task-wakeup.service -n 100 --no-pager
```

If the callback reaches `running/` but no file appears in `acks/`, inspect the resumed Codex header
and the daemon log. A healthy daemon resume should show `sandbox: workspace-write` and include the
callback queue directory in the writable roots. If it shows `sandbox: read-only` or `approval:
never` and `ack` fails with `OSError: [Errno 30] Read-only file system`, the installed CLI or
service is stale, or the daemon was run from inside a restricted Codex tool sandbox.

If setup printed `systemctl not found`, this is expected in many Docker images. If supervisor is
installed, confirm the daemon with `supervisorctl status codex-long-task-wakeup` and
`${CODEX_HOME:-~/.codex}/long-task-wakeup/supervisor.log`. If supervisor is missing, setup uses the
standalone fallback; check `${CODEX_HOME:-~/.codex}/long-task-wakeup/daemon.pid` and
`${CODEX_HOME:-~/.codex}/long-task-wakeup/daemon.log`. For durable Docker restarts, run the
container with Docker's `restart: unless-stopped` policy and let supervisor manage the daemon inside
the container.

Refresh the install and service:

```bash
python3 -m pip install --upgrade --force-reinstall \
  "git+https://github.com/lz59970062/long-task-wakeup.git"
hash -r
codex-long-task-wakeup setup --force --enable --now
systemctl --user restart codex-long-task-wakeup.service
```

Then retry with a tiny smoke callback:

```bash
codex-long-task-wakeup done --via-daemon --last \
  --cwd "$PWD" \
  --task "long-task-wakeup smoke test" \
  --command "manual smoke test" \
  --exit-code 0 \
  --message "Please acknowledge this callback and report whether it reached done."
```

Success means the same callback id appears in both `acks/` and `done/`. If the service is missing or
stopped, run `codex-long-task-wakeup setup --force --enable --now`. If `codex-long-task-wakeup
--help` does not list `setup` and `ack`, locate stale console scripts with `which -a
codex-long-task-wakeup`.

## Codex Skill

This repository also includes a Codex skill:

```text
SKILL.md
agents/openai.yaml
```

The skill teaches Codex when and how to wire explicit callbacks into long-running tasks. The
important behavior is procedural: Codex should insert a callback only when the task should wake
the same session after completion.

---

# Long Task Wakeup 中文说明

**Long Task Wakeup 让长时间运行的 Codex 任务，在结束后主动把 Codex 叫回来。**

Codex 很适合启动耗时任务：模型训练、benchmark、大型测试、数据处理、构建、部署、仿真。
真正尴尬的是几个小时以后：命令结束了，但原来的 Codex 推理上下文已经冷掉了。

Long Task Wakeup 用一个显式 callback 解决这个问题。Codex 在长任务的结束路径里写入一条
很小的命令。任务结束时，这条命令会恢复原来的 Codex session，让 Codex 检查结果、判断目标
是否完成，并在下一步明确时继续执行。

默认没有轮询、没有 daemon、没有后台监控。只有任务代码主动调用时，它才会启用。
对于递归或多阶段工作流，同一条 callback 可以写入队列，由用户启动的 daemon 在 Codex
工具 sandbox 外部负责恢复 session。

## 特点

- **显式触发**：只有写进任务命令或代码里才会运行。
- **适合过夜任务**：训练、评测、benchmark、部署、数据任务、大型测试。
- **回到同一个 session**：内部使用 `codex exec resume`。
- **需要时使用 daemon 交接**：`--via-daemon` 会把唤醒请求入队，由外部 daemon 启动 Codex。
- **标准守护方式**：`install-systemd --enable --now` 安装用户级 systemd service。
- **可取消队列项**：不再需要的 daemon callback 可以移到 `canceled/`，避免继续重试。
- **不干扰任务逻辑**：默认情况下，唤醒失败不会改变任务退出码。
- **很小的工具面**：一个 Python 包，一个全局 CLI。
- **不依赖日志功能**：传 task、command、exit code、cwd 和可选 message 即可。

## 安装

通过 GitHub HTTPS 直接安装：

```bash
python3 -m pip install "git+https://github.com/lz59970062/long-task-wakeup.git"
```

或者 clone 后本地安装：

```bash
git clone https://github.com/lz59970062/long-task-wakeup.git
cd long-task-wakeup
python3 -m pip install .
```

安装后会得到全局命令：

```bash
codex-long-task-wakeup
```

安装后先验证 Python 模块入口和全局命令是否一致：

```bash
python -m long_task_callback --version
python -m long_task_callback --help
codex-long-task-wakeup --version
codex-long-task-wakeup --help
```

帮助输出必须包含：

```text
daemon
install-systemd
install-skill
setup
ack
cancel
```

如果 `python -m long_task_callback --help` 有 `install-systemd`，但
`codex-long-task-wakeup --help` 没有，说明 shell 正在执行另一个环境或更早 `PATH` 里的旧
console script。用下面命令定位并刷新：

```bash
which -a codex-long-task-wakeup
hash -r
python -m pip install --upgrade --force-reinstall --no-cache-dir \
  "git+https://github.com/lz59970062/long-task-wakeup.git"
```

同时安装内置 Codex skill 和用户级 daemon：

```bash
codex-long-task-wakeup setup --force --enable --now
```

或者只把内置 Codex skill 安装到 `~/.codex/skills`：

```bash
codex-long-task-wakeup install-skill
```

如果你使用自定义 Codex home，可以设置 `CODEX_HOME` 或传 `--path`：

```bash
CODEX_HOME=/path/to/.codex codex-long-task-wakeup install-skill
codex-long-task-wakeup install-skill --path /path/to/.codex/skills
```

## 用法

### 包装一个长命令

当 Codex 可以直接启动长任务时，用 `run`：

```bash
codex-long-task-wakeup run \
  --session <session-id> \
  --cwd "$PWD" \
  --task "train model" \
  -- python train.py --config configs/exp.yaml
```

它会返回被包装命令的退出码，并在命令结束后唤醒 Codex。

唤醒步骤是 best-effort。如果 Codex 没有恢复成功，被包装命令的退出码仍然保持不变。

递归或多阶段工作流建议把唤醒请求交给外部 daemon：

```bash
codex-long-task-wakeup run \
  --via-daemon \
  --session <session-id> \
  --cwd "$PWD" \
  --task "train model" \
  -- python train.py --config configs/exp.yaml
```

### 写入已有任务代码

当 Codex 需要把 callback 写进 shell 脚本、Python `finally`、Slurm epilogue 等结束路径时，
用 `done`：

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

默认情况下，即使 Codex 唤醒失败，callback 命令也会返回 `0`，所以最后的
`exit "$status"` 仍然是任务结果的唯一来源。

如果 callback 可能在恢复后的 Codex 工具 sandbox 里运行，给 `done` 加上 `--via-daemon`：

```bash
codex-long-task-wakeup done \
  --via-daemon \
  --session <session-id> \
  --cwd "$PWD" \
  --task "train model" \
  --command "python train.py --config configs/exp.yaml" \
  --exit-code "$status"
```

也可以设置 `CODEX_LONG_TASK_WAKEUP_VIA_DAEMON=1`，避免每条命令都显式传参。

### 运行 Wakeup Daemon

标准长期运行方式是用户级 systemd service：

```bash
codex-long-task-wakeup setup --force --enable --now
```

它会写入：

```text
~/.config/systemd/user/codex-long-task-wakeup.service
```

并执行 `systemctl --user daemon-reload`、`enable` 和 `restart`。这个 service 会在 Codex
工具 sandbox 外部保持 `codex-long-task-wakeup daemon` 常驻，并在异常退出后自动重启。
在 Docker 或精简容器里如果没有 `systemctl`，同一条 `setup --force --enable --now` 会优先使用
supervisor，前提是已安装 `supervisorctl` 或 `supervisord`。它会写入：

```text
/etc/supervisor/conf.d/codex-long-task-wakeup.conf
${CODEX_HOME:-~/.codex}/long-task-wakeup/supervisor.log
```

supervisor program 使用 `autostart=true` 和 `autorestart=true`，能在 `supervisord` 存活期间保持
daemon 运行。容器级重启语义建议配合 Docker 的 `--restart unless-stopped` 或 Compose 的
`restart: unless-stopped`。如果没有安装 supervisor，setup 才会退化为 standalone 后台 daemon。
这个 fallback 模式下 `--enable` 没有作用，因为容器里没有 init system；`--now` 会立即启动
daemon，并写入：

```text
${CODEX_HOME:-~/.codex}/long-task-wakeup/daemon.pid
${CODEX_HOME:-~/.codex}/long-task-wakeup/daemon.log
```

安装器也会把解析到的 `codex` 可执行文件路径写入 `CODEX_LONG_TASK_WAKEUP_CODEX_BIN`，
并写入当前 `PATH`，确保 daemon 在非交互式 systemd 环境里也能找到 Codex 的 Node/NVM
等运行时依赖。

查看和管理：

```bash
systemctl --user status codex-long-task-wakeup.service
journalctl --user -u codex-long-task-wakeup.service -f
systemctl --user restart codex-long-task-wakeup.service
systemctl --user stop codex-long-task-wakeup.service
```

Docker supervisor 模式下先看 supervisor：

```bash
supervisorctl status codex-long-task-wakeup
tail -f "${CODEX_HOME:-$HOME/.codex}/long-task-wakeup/supervisor.log"
```

如果没有安装 supervisor，setup 使用 standalone fallback，再查看：

```bash
cat "${CODEX_HOME:-$HOME/.codex}/long-task-wakeup/daemon.pid"
tail -f "${CODEX_HOME:-$HOME/.codex}/long-task-wakeup/daemon.log"
```

安装前预览 unit：

```bash
codex-long-task-wakeup install-systemd --print
```

如果 skill 已经装好，只需要 daemon service，可以单独运行：

```bash
codex-long-task-wakeup install-systemd --enable --now
```

如果 `codex` 安装在非标准位置，可以显式指定：

```bash
codex-long-task-wakeup install-systemd --codex-bin /path/to/codex --enable --now
```

调试时也可以前台运行：

```bash
codex-long-task-wakeup daemon
```

默认队列目录是 `${CODEX_HOME:-~/.codex}/long-task-wakeup/queue`。需要自定义时使用
`--queue-dir` 或 `CODEX_LONG_TASK_WAKEUP_QUEUE_DIR`。

如果某个队列里的 callback 在触发前或重试结束前已经不需要了，用 `cancel`，不要手动删除队列文件：

```bash
codex-long-task-wakeup cancel --id <callback-id>
codex-long-task-wakeup cancel --queue-dir <queue-dir> --id <callback-id>
codex-long-task-wakeup cancel --queue-dir <queue-dir> --all --message "no longer needed"
```

取消会先为 `active/`、`pending/` 或 `running/` 里的请求持久化 `canceled/` tombstone，再清理
可投递副本。如果被包装任务或 Codex resume 已经启动，`cancel` 不会杀掉那个进程；它只抑制
callback 收尾和后续重试。

### `run --via-daemon` 的耐久生命周期

从 0.4.1 开始，工具会在启动被包装命令前，先把一个稳定 callback id 写入 `active/`，并用
close-on-exec owner lock 区分健康 wrapper 和孤儿记录。正常完成时，同一个 id 会记录真实退出码
并移动到 `pending/`。如果 wrapper 被 SIGKILL，daemon 会把同一个 id 恢复成
`outcome: unknown`，并明确提示子进程可能仍在运行；遇到这种 callback，必须先检查进程和产物，
不能直接重跑。

如果退出码已经落盘、只是最后一次状态移动被中断，恢复会保留已知退出码，不会错误降级为
unknown。记录替换和状态迁移会同步文件及相关队列目录。重启恢复仍要求队列位于持久存储，并且
daemon service 会在重启后启动。

从 0.4.2 开始，工具还会在 `${CODEX_HOME:-~/.codex}/long-task-wakeup/target-locks` 中按目标
session 保存哈希后的全局 lease。即使两个 daemon 分别监听默认队列和专用队列，同一 session
也只能有一个 Codex resume。若必须覆盖目录，可设置 `CODEX_LONG_TASK_WAKEUP_TARGET_LOCK_DIR`，
但所有可能唤醒同一 session 的 daemon 必须使用同一个值。竞争失败的 callback 会退回
`pending/`，且不消耗 retry 次数。

每次投递由独立 delivery worker 同时持有 callback 锁、全局 session 锁和超时，Codex 及其后代
不会继承这些锁。因此 daemon
进程被直接杀死并重启时，仍在运行的投递不会立即重复启动。若要重启 systemd service 或容器，
应尽量先等 `running/` 清空；服务管理器通常也会终止 delivery worker，而尚未 ACK 的中断投递
会按 at-least-once 语义在重启后重新投递。

这是 at-least-once callback 协议，不是 exactly-once 任务执行。稳定 id、单调 ACK、daemon
单例锁和每次投递锁可以消除常见重复，但如果 Codex 已收到 prompt、ACK 尚未耐久落盘时主机故障，
仍可能重试。因此恢复后的 agent 必须先检查现有进程和 artifact，再决定是否重新启动任务。

如果 pre-arm 无法持久化，默认模式会输出醒目的 `UNARMED WARNING`，继续执行任务，并退回旧式
“结束后尽力入队”，以保持被包装命令原有语义。若任务在没有耐久 callback 保护时绝不能启动，
使用 `--strict`；它会在启动前以 125 退出。可用
`codex-long-task-wakeup status --state active` 显式查看健康 wrapper；shell 启动提示仍只展示
`pending/` 和 `running/`。

如果希望用户服务在退出登录后仍然保活，在宿主机上执行一次：

```bash
loginctl enable-linger "$USER"
```

### Python finally 示例

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

## 工作逻辑

`codex-long-task-wakeup done` 会生成一段 prompt，包含：

- 任务名称
- 工作目录
- 原始命令
- 退出码
- 可选 message

默认直接执行：

```bash
codex exec resume --all <session-id> -
```

这段 prompt 会通过 stdin 发给恢复后的 Codex session。Codex 随后可以检查 artifact、metric、
checkpoint、生成文件或测试报告，并判断下一步。

使用 `--via-daemon` 时，`run` 和 `done` 会把同一段 prompt 原子写成 JSON 队列项。
`codex-long-task-wakeup daemon` 随后从自己的外部环境读取队列，并执行
`codex exec resume --all ... -`。这样可以避免 `resume -> tool sandbox -> resume` 的递归链，
也就不会继承嵌套 Codex 工具 sandbox 的文件系统或网络限制。

## 不干扰任务逻辑的保证

任务执行和 Codex 唤醒是分离的：

- `run` 模式返回被包装任务的退出码。
- `done` 模式默认返回 `0`，避免 callback 失败破坏 shell epilogue。
- 唤醒失败和 daemon 入队失败只会写 stderr warning，不会变成任务失败。
- 只有显式传 `--strict` 时，才会传播 callback 失败。

只有当“恢复最近的 Codex session”是可接受行为时，才使用 `--last`：

```bash
codex-long-task-wakeup done --last --cwd "$PWD" --task "long eval" --exit-code "$status"
```

## 健康检查与排查

setup 之后先确认 CLI、skill 和 daemon：

```bash
codex-long-task-wakeup --version
codex-long-task-wakeup --help
systemctl --user status codex-long-task-wakeup.service
journalctl --user -u codex-long-task-wakeup.service -n 100 --no-pager
```

如果 callback 已经进入 `running/`，但 `acks/` 没有 marker，检查恢复后的 Codex header 和
daemon 日志。健康的 daemon resume 应该显示 `sandbox: workspace-write`，并且 writable roots
里包含 callback 队列目录。如果看到 `sandbox: read-only` 或 `approval: never`，并且 `ack`
因为 `OSError: [Errno 30] Read-only file system` 失败，通常说明安装的 CLI 或 systemd
service 还是旧的，或者 daemon 是从受限的 Codex tool sandbox 里直接运行的。

如果 setup 打印 `systemctl not found`，这在很多 Docker 镜像里是正常的。如果安装了
supervisor，用 `supervisorctl status codex-long-task-wakeup` 和
`${CODEX_HOME:-~/.codex}/long-task-wakeup/supervisor.log` 确认 daemon。如果没有 supervisor，
setup 会使用 standalone fallback；检查 `${CODEX_HOME:-~/.codex}/long-task-wakeup/daemon.pid`
和 `${CODEX_HOME:-~/.codex}/long-task-wakeup/daemon.log`。需要持久重启时，容器层用 Docker
的 `restart: unless-stopped`，容器内用 supervisor 管 daemon。

刷新安装和 service：

```bash
python3 -m pip install --upgrade --force-reinstall \
  "git+https://github.com/lz59970062/long-task-wakeup.git"
hash -r
codex-long-task-wakeup setup --force --enable --now
systemctl --user restart codex-long-task-wakeup.service
```

然后用一个很小的 smoke callback 重试：

```bash
codex-long-task-wakeup done --via-daemon --last \
  --cwd "$PWD" \
  --task "long-task-wakeup smoke test" \
  --command "manual smoke test" \
  --exit-code 0 \
  --message "Please acknowledge this callback and report whether it reached done."
```

成功时，同一个 callback id 会同时出现在 `acks/` 和 `done/`。如果 service 不存在或没启动，
运行 `codex-long-task-wakeup setup --force --enable --now`。如果 `codex-long-task-wakeup
--help` 里没有 `setup` 和 `ack`，用 `which -a codex-long-task-wakeup` 找旧 console script。
