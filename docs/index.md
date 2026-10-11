# Long Task Callback

Long Task Callback（LTC）让 Agent 在长任务结束后回到原会话继续工作。你可以让它提交一次模型训练、测试或代码审查，任务在后台执行。结束后，LTC 把结果位置送回会话，Agent 读取日志和产物，接着处理下一步。

[开始使用](quickstart.md){ .md-button .md-button--primary }
[了解架构](overview.md){ .md-button }

## 从一个任务开始

在 Agent 会话中提交训练命令：

```bash
ltc run --cwd "$PWD" --task "训练模型" -- python train.py --config configs/exp.yaml
```

LTC 返回任务 ID 和日志路径。任务结束后，原会话收到回调，Agent 检查输出并执行回调中的 ACK 命令。第一次使用可以先按[快速开始](quickstart.md)跑一个十秒钟的示例。

## 选择入口

| 你要做的事 | 从这里开始 |
| --- | --- |
| 运行训练、测试、构建或数据处理 | [运行命令](guides/run.md) |
| 把一项工作交给另一个 Agent | [启动子 Agent](guides/agents.md) |
| 接收 Slurm、tmux 中已有任务的结果 | [接入已有任务](guides/external-jobs.md) |
| 重复使用一套审查或分析提示词 | [编写任务模板](guides/templates.md) |
| 跟踪训练、评估、报告等多个阶段 | [跟踪多阶段目标](guides/goals.md) |

## 接入你的环境

先看[典型使用场景](guides/use-cases.md)选择工作流程，再通过 [Harness 支持情况](integrations/support.md)确认接入方式和版本。

LTC 支持 Linux、macOS 和 Windows。按[安装指南](installation.md)配置运行环境，再选择你使用的 Agent：[Codex CLI](integrations/codex.md)、[Claude Code](integrations/claude.md)、[Pi](integrations/pi.md)或 [Codex Desktop](integrations/desktop.md)。

快速开始使用正式版 `0.7.0`。Claude 实时回调、Pi 和 Desktop 接入使用 `0.7.1` 预览版，各接入页给出对应步骤。

## 继续阅读

[架构概览](overview.md)介绍一次任务从提交到回调的过程。[命令速查](reference/cli.md)方便随用随查，遇到运行问题时查看[常见问题](troubleshooting.md)。模块说明、平台实现与验证记录收录在[开发资料](reference/engineering.md)。
