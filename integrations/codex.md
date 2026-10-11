# 接入 Codex CLI

先按[安装指南](../installation.md)安装 LTC，并在启动 Codex 的终端中激活 LTC 所在的虚拟环境。

## 在会话中提交

让 Codex 执行：

```bash
ltc run --cwd "$PWD" --task "构建项目" -- bash scripts/build.sh
```

LTC 从 Codex 的工具环境中读取 `CODEX_THREAD_ID`，把这次任务绑定到当前会话。结束后，协调器恢复这个会话，并送入任务结果。

日常使用时，你可以直接对 Codex 说：

> 用 LTC 运行这次构建，结束后读取日志，检查输出的安装包，再告诉我结果。

`ltc setup` 安装的技能包含提交任务、读取回调和 ACK 的操作方式。

## 指定接收会话

从其他脚本提交时，显式传入会话 ID：

```bash
ltc run --agent codex --session "填入原会话ID" --cwd "$PWD" --task "构建项目" -- bash scripts/build.sh
```

使用原会话对应的 `CODEX_HOME`，可以让 LTC 找到同一份会话记录和配置。默认配置目录是 `~/.codex`。

## 检查配置

在 Codex 工具环境中执行：

```bash
ltc doctor --agent codex --session "$CODEX_THREAD_ID"
```

根据报告中的修复命令处理安装、协调器或会话配置，再运行[十秒示例](../quickstart.md#提交第一个任务)确认回调到达。

桌面应用的接入步骤见 [Codex Desktop](desktop.md)。
