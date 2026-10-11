# 接入 Claude Code

实时接收回调使用 `0.7.1` 预览版。按[安装指南](../installation.md#安装预览版)安装后，运行 `ltc setup`，让 Claude Code 加载 LTC 技能。

## 提交任务并接收结果

让 Claude 在当前会话里提交：

```bash
ltc run --cwd "$PWD" --task "运行测试" -- python -m pytest tests/
```

提交输出会给出一条 `ltc wait` 命令，例如：

```bash
ltc wait --queue-dir ~/.ltc/queue --task <task-id>
```

让 Claude 使用 Bash 工具的 `run_in_background: true` 执行输出中的完整命令。任务结果进入队列后，`ltc wait` 输出回调并退出，Claude Code 接着处理这条后台命令的完成消息。

Claude 读取日志和产物后，执行回调中的 `ltc ack` 命令。这个流程可用于终端 CLI，也可用于 Claude 桌面应用的 Code 会话。

## 用自然语言安排工作

安装技能后，可以直接说：

> 用 LTC 跑完整测试。收到结果后，打开失败测试的日志，定位原因并给出修改建议。

技能会引导 Claude 提交任务、后台运行 waiter、读取结果和确认回调。

## 配置 CLI

如果需要启动 Claude 子 Agent，或在会话关闭后通过 CLI 恢复，先在这台机器上完成登录：

```bash
claude auth login
claude auth status
```

在当前会话的工具环境中检查 LTC：

```bash
ltc doctor --agent claude --session "$CLAUDE_CODE_SESSION_ID"
```

SSH 工作时，在运行任务的远程主机上安装 LTC 和技能。二进制定位、会话目录及 channel 接入详见 [Claude Code 实现资料](https://github.com/lz59970062/long-task-wakeup/blob/main/docs/claude-code.md)。
