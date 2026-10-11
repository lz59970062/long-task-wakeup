# 常见问题

## 终端找不到 ltc

激活安装 LTC 的虚拟环境，再检查命令：

```bash
source /安装目录/long-task-wakeup/.venv/bin/activate
ltc --version
```

Windows 可以直接调用安装目录下的 `.venv\Scripts\ltc.exe`。后台协调器也使用安装时的 Python 路径，移动虚拟环境后重新运行 `ltc setup`。

## 任务提交时提示会话信息缺失

让 Agent 在当前会话的工具环境中执行 `ltc run`。从独立终端或作业脚本提交时，传入 `--agent` 和 `--session`：

```bash
ltc run --agent codex --session "填入原会话ID" --task "构建" -- bash scripts/build.sh
```

会话绑定方式见 [Codex CLI](integrations/codex.md)和[外部任务](guides/external-jobs.md)。

## 任务结束后还没看到回调

先查看回调队列，再检查当前会话配置：

```bash
ltc status --state pending --state running --state failed
ltc doctor --agent codex --session <原会话ID>
```

按报告中的具体项目处理：

| 情况 | 下一步 |
| --- | --- |
| 协调器尚未启动 | 按报告给出的 `setup` 命令启动它 |
| Claude Code 会话正在打开 | 让 Claude 将提交输出中的 `ltc wait` 命令作为后台 Bash 任务运行 |
| Codex Desktop 连接需要配置 | 从 [Desktop 启动器](integrations/desktop.md)启动应用 |
| 回调进入 `failed` | 读取错误记录，修复后在预览版中使用 `ltc retry --id <回调ID>` |

业务任务的日志路径来自最初的提交输出。先打开日志和产物，可以了解任务本身的运行结果。

## 同一条回调重复出现

读取结果后，执行回调中的完整 ACK 命令。核对回调 ID 和 `--queue-dir`，确认写入的是同一个队列。

## 重启后怎样继续训练

找到任务日志、结果文件和最近的 checkpoint。让 Agent 检查已有产物，再使用训练程序自身的恢复参数提交后续任务，例如：

```bash
ltc run --cwd "$PWD" --task "继续训练" -- python train.py --resume outputs/checkpoint.pt
```

这里的 `--resume` 属于示例训练程序。换成你所用框架的恢复参数和实际 checkpoint 路径。

## 怎样反馈问题

在 [GitHub Issues](https://github.com/lz59970062/long-task-wakeup/issues)中附上操作系统、LTC 版本、提交命令、错误文本和相关日志片段。提供一个小型复现命令，便于定位发生问题的步骤。
