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

Install the bundled Codex skill into `~/.codex/skills`:

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
codex-long-task-wakeup install-systemd --enable --now
```

This writes:

```text
~/.config/systemd/user/codex-long-task-wakeup.service
```

and runs `systemctl --user daemon-reload`, `enable`, and `restart`. The service keeps
`codex-long-task-wakeup daemon` alive outside Codex tool sandboxes and restarts it if it exits.
The installer also records the resolved `codex` executable path in
`CODEX_LONG_TASK_WAKEUP_CODEX_BIN` and the current `PATH`, so the daemon can find `codex` and its
runtime dependencies such as Node/NVM outside an interactive shell.

Inspect or manage it with:

```bash
systemctl --user status codex-long-task-wakeup.service
journalctl --user -u codex-long-task-wakeup.service -f
systemctl --user restart codex-long-task-wakeup.service
systemctl --user stop codex-long-task-wakeup.service
```

To review the generated unit before installing:

```bash
codex-long-task-wakeup install-systemd --print
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

Use `--last` instead of `--session <session-id>` only when resuming the most recent Codex session
is acceptable:

```bash
codex-long-task-wakeup done --last --cwd "$PWD" --task "long eval" --exit-code "$status"
```

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

把内置 Codex skill 安装到 `~/.codex/skills`：

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
codex-long-task-wakeup install-systemd --enable --now
```

它会写入：

```text
~/.config/systemd/user/codex-long-task-wakeup.service
```

并执行 `systemctl --user daemon-reload`、`enable` 和 `restart`。这个 service 会在 Codex
工具 sandbox 外部保持 `codex-long-task-wakeup daemon` 常驻，并在异常退出后自动重启。
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

安装前预览 unit：

```bash
codex-long-task-wakeup install-systemd --print
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
