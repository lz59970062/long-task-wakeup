# Harness 支持情况

Harness 指运行 Agent 的外层程序：它管理会话、调用模型、执行工具，并把新消息交给 Agent。LTC 的接入涉及两件事：启动它作为子 Agent，以及把任务结果送回它的原会话。

以下按当前 `main` 的实现和仓库验证记录整理，核对日期为 2026-10-12。快速开始使用正式版 `0.7.0`，新增接入使用 `0.7.1` 预览版。

## 原生接入

| Harness / 客户端 | 作为子 Agent | 原会话接收结果 | 版本与验证情况 |
| --- | --- | --- | --- |
| Codex CLI | `ltc agent codex` | 使用 `CODEX_THREAD_ID` 绑定会话，通过 CLI 恢复 | 0.7.0；正式版与平台验证记录见下文 |
| Claude Code CLI | `ltc agent claude` | 后台 `ltc wait` 实时接收；独立 CLI 可恢复会话 | 子 Agent、CLI 恢复从 0.7.0 起；实时回调为 0.7.1 预览，Linux/macOS 已验证 |
| Claude 桌面应用的 Code 会话 | 子任务通过已登录的独立 Claude CLI 执行 | 后台 Bash 运行 `ltc wait` | 0.7.1 预览；macOS 本地会话、Linux SSH 会话已验证 |
| Pi | `ltc agent pi` | 扩展向当前进程发送 steer 消息；`ltc pi` 增加关闭后的会话恢复 | 0.7.1 预览；Linux、Pi 0.87.1 有验证记录，macOS/Windows 待验证 |
| Codex Desktop | 子任务使用 Codex CLI | 共享 Core 启动器和 `--callback-mode desktop` | 0.7.1 预览；新安装路径的真实 Desktop 重启、接收和 ACK 验收待完成 |

子 Agent 与接收方可以组合。例如在 Codex 中执行 `ltc agent claude`，Claude 执行任务，结果回到 Codex。操作步骤见 [Codex](codex.md)、[Claude](claude.md)、[Pi](pi.md)和 [Desktop](desktop.md)。

Claude 还提供 `ltc claude` channel 研究预览入口，已记录 Linux 上 Claude Code 2.1.296 的实时接收与 ACK。配置步骤见 [channel 说明](https://github.com/lz59970062/long-task-wakeup/blob/main/docs/claude-code.md#ltc-channel-research-preview)。

## 其他 Harness 和编排框架

| 名称 | 当前接入方式 | 后续原生接入工作 |
| --- | --- | --- |
| Gemini CLI、OpenCode、Aider、Goose | 可将已配置的命令行任务放在 `ltc run -- <命令>` 后执行，结果交回现有接收方 | 实现各自的子 Agent 参数、会话识别和回调传输；逐项进行端到端验证 |
| LangGraph、AutoGen、CrewAI、自定义 Agent SDK 程序 | 将工作流保存为脚本，通过 `ltc run` 执行，或在已有任务末尾调用 `ltc done` | 为接收回调的会话实现适配器与唤醒通道 |
| DSH | 仓库路线图中规划了适配器 | 完成实现与验证后加入原生接入表 |

这些框架目前处于通用命令接入层，仓库原生适配器注册表包含 `codex`、`claude`、`pi`。开始接入时，先让脚本打印一行结果并正常退出，再检查日志、回调和 ACK；随后接入实际工作流。脚本示例见[典型使用场景](../guides/use-cases.md#运行自定义-agent-工作流)。

## 执行环境

| 环境 | 任务后端 | 使用条件 |
| --- | --- | --- |
| Linux | systemd 用户服务、GNU screen | 桌面或服务器；容器和 SSH 环境可用 screen |
| macOS | launchd、GNU screen | launchd 使用当前登录用户的 GUI 会话 |
| Windows 10+ | Task Scheduler、Job Object | 当前用户保持登录，任务状态使用本地 NTFS 目录 |

任务执行后端随 `0.7.0` 发布。选择具体 Harness 时，再按上表配置其回调入口。Claude 实时回调的 Windows 验证仍待完成。

## 接入新的 Harness

从仓库的 [`agents/base.py`](https://github.com/lz59970062/long-task-wakeup/blob/main/src/long_task_callback/agents/base.py) 和 [`agents/registry.py`](https://github.com/lz59970062/long-task-wakeup/blob/main/src/long_task_callback/agents/registry.py) 开始：

1. 实现子 Agent 启动：定位程序、构造命令、传入提示词，并约定回答写入文件还是标准输出。
2. 定义会话绑定：读取当前会话标识，保存恢复会话需要的 profile 和目录。
3. 接入回调：为关闭的会话构造恢复命令；为正在运行的会话提供能送入新消息的通道。
4. 在适配器注册表登记，补齐 CLI 选择、安装入口及该 Harness 的操作技能。
5. 使用独立会话验证提交、日志、结果、接收、ACK，并检查忙碌、退出和重试时的行为。

`ChildAgentAdapter` 描述子任务接口，`AgentAdapter` 增加会话识别与恢复。按这两个接口组织实现，可以分别验证任务执行和结果投递。

## 实现与验证依据

- [当前适配器注册表](https://github.com/lz59970062/long-task-wakeup/blob/main/src/long_task_callback/agents/__init__.py)
- [0.7.0 发布验证](https://github.com/lz59970062/long-task-wakeup/blob/main/docs/validation-0.7.0.md)
- [Claude Code 平台记录](https://github.com/lz59970062/long-task-wakeup/blob/main/docs/claude-code.md#platform-status)
- [Pi 接入与验证](https://github.com/lz59970062/long-task-wakeup/blob/main/docs/validation-pi-child.md)
- [Desktop 预览版验收记录](https://github.com/lz59970062/long-task-wakeup/blob/main/docs/validation-0.7.1-preview.md)
