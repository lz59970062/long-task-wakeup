# Long Task Wakeup

**Long Task Wakeup turns long-running Codex jobs into self-returning workflows.**

Codex is great at kicking off expensive work: model training, benchmarks, large test suites,
data pipelines, builds, deployments, simulations. The awkward part is what happens hours later:
the command finishes, but the original reasoning loop has gone cold.

Long Task Wakeup solves that with an explicit callback. Codex writes one small command into the
task's exit path. When the task finishes, that command resumes the original Codex session and asks
it to inspect the result, decide whether the goal is complete, and continue if the next step is
clear.

No polling. No daemon. No passive watcher. Nothing runs unless your task explicitly calls it.

## Highlights

- **Explicit by design**: only activates when written into the task command or code.
- **Good for overnight work**: training, evals, benchmarks, deployments, data jobs, long tests.
- **Same-session handoff**: resumes Codex with `codex exec resume`.
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

Then it runs:

```bash
codex exec resume --all <session-id> -
```

The prompt is sent to the resumed Codex session through stdin. Codex can then inspect artifacts,
metrics, checkpoints, generated files, or test reports and decide the next step.

## Non-Interference Guarantee

Task execution and Codex wakeup are intentionally separated:

- `run` mode returns the wrapped task's exit code.
- `done` mode returns `0` by default so callback failure does not break shell epilogues.
- Callback failures are warnings on stderr, not task failures.
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

没有轮询。没有 daemon。没有后台监控。只有任务代码主动调用时，它才会启用。

## 特点

- **显式触发**：只有写进任务命令或代码里才会运行。
- **适合过夜任务**：训练、评测、benchmark、部署、数据任务、大型测试。
- **回到同一个 session**：内部使用 `codex exec resume`。
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

然后执行：

```bash
codex exec resume --all <session-id> -
```

这段 prompt 会通过 stdin 发给恢复后的 Codex session。Codex 随后可以检查 artifact、metric、
checkpoint、生成文件或测试报告，并判断下一步。

## 不干扰任务逻辑的保证

任务执行和 Codex 唤醒是分离的：

- `run` 模式返回被包装任务的退出码。
- `done` 模式默认返回 `0`，避免 callback 失败破坏 shell epilogue。
- 唤醒失败只会写 stderr warning，不会变成任务失败。
- 只有显式传 `--strict` 时，才会传播 callback 失败。

只有当“恢复最近的 Codex session”是可接受行为时，才使用 `--last`：

```bash
codex-long-task-wakeup done --last --cwd "$PWD" --task "long eval" --exit-code "$status"
```
