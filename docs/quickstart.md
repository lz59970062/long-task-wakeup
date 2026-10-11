# 快速开始

这次先运行一个十秒钟的小任务。完成后，你会在原来的 Agent 会话里收到结果，再让 Agent 读取日志并确认回调。

以下步骤使用 LTC 0.7.0、Linux 和 Codex CLI。准备好 Python 3.9 或更新版本、Git，以及已登录的 Codex CLI。Linux 任务由 systemd 用户服务或 GNU screen 托管。其他环境的步骤见[安装指南](installation.md)。

## 安装

在终端中执行：

```bash
git clone --branch v0.7.0 --depth 1 https://github.com/lz59970062/long-task-wakeup.git
cd long-task-wakeup
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install .
ltc setup --enable --now
ltc --version
```

`setup` 会安装 Agent 技能并启动后台协调器。最后一条命令应显示 `0.7.0`。

保留这个虚拟环境的位置，后台协调器会使用其中的 Python。接着在当前终端启动 Codex CLI；以后进入自己的项目时，也先激活这个环境，再启动 Codex。

## 提交第一个任务

在 Codex CLI 对话中，让 Codex 执行下面的命令。LTC 会从会话环境读取回调目标。

```bash
ltc run --cwd "$PWD" --task "第一次 LTC 任务" \
  -- python3 -c "import time; time.sleep(10); print('LTC demo finished')"
```

`--task` 是任务名称，`--cwd` 是工作目录，`--` 后面是要执行的命令。提交后，LTC 会打印任务 ID、执行后端和日志路径，然后返回。这个 Python 程序会在后台等待十秒，输出一行文字后结束。

## 读取结果并确认回调

任务结束后，LTC 会把回调送回刚才的会话。回调中包含任务状态、日志路径和 ACK 命令。让 Codex 打开日志，你应当能看到：

```text
LTC demo finished
```

读完结果后，让 Codex 执行回调里给出的完整 ACK 命令。默认队列的命令形式是：

```bash
ltc ack --id <callback-id>
```

把 `<callback-id>` 换成这条回调给出的 ID。ACK 会把回调标记为已收到，结束这条回调的提醒。

需要查看待处理回调时，在终端运行：

```bash
ltc status
```

## 换成自己的任务

现在把示例中的 Python 命令换成项目里的训练命令，让 Agent 在项目目录中执行：

```bash
ltc run --cwd "$PWD" --task "训练模型" \
  -- python3 train.py --config configs/exp.yaml
```

任务名写成自己容易辨认的内容，例如“训练 ResNet 基线”或“构建 Linux 安装包”。Agent 收到结果后，就能结合任务名、日志和输出文件继续工作。

如果要交给子 Agent 做代码审查，可以使用：

```bash
ltc agent codex --cwd "$PWD" --task "检查当前改动" \
  -- "阅读当前 git diff，检查可能导致运行失败的问题，报告文件位置和原因。"
```

LTC 会打印子 Agent 的回答文件路径，并在子任务结束后回调原会话。提示词复用方法见 [编写任务模板](guides/templates.md)。

接下来阅读[架构概览](overview.md)，了解这些任务和回调由谁处理。
