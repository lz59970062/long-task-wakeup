# 命令速查

查看完整参数：

```bash
ltc --help
ltc run --help
ltc agent --help
```

## 安装和检查

| 命令 | 用途 |
| --- | --- |
| `ltc --version` | 查看当前版本 |
| `ltc setup --enable --now` | 安装技能并启动协调器 |
| `ltc setup --keep-skill --force --enable --now` | 更新协调器，保留技能内容 |
| `ltc doctor --agent codex --session <id>` | 检查指定会话的运行配置 |

## 提交工作

| 命令 | 用途 |
| --- | --- |
| `ltc run --task <名称> -- <命令>` | 提交一个命令 |
| `ltc agent codex -- <任务>` | 启动 Codex 子 Agent |
| `ltc agent claude -- <任务>` | 启动 Claude 子 Agent |
| `ltc done --task <名称> --exit-code <退出码>` | 报告外部任务结果 |

提交参数写在 `--` 前面：

| 参数 | 用途 |
| --- | --- |
| `--cwd <目录>` | 工作目录 |
| `--task <名称>` | 回调中显示的任务名称 |
| `--agent <类型>` | 接收回调的 Agent 类型 |
| `--session <会话>` | 接收回调的会话 |
| `--queue-dir <目录>` | 回调队列位置 |
| `--backend auto` | 自动选择任务执行后端 |
| `--goal-id <目标>` | 关联多阶段目标 |
| `--callback-format full` | 使用完整回调文本 |

## 处理回调

| 命令 | 用途 |
| --- | --- |
| `ltc status` | 查看待处理回调 |
| `ltc status --state failed` | 查看投递失败的回调 |
| `ltc ack --id <回调ID>` | 确认已经读取结果 |
| `ltc cancel --id <回调ID>` | 撤销这条回调的投递 |
| `ltc prompt-policy` | 查看提醒间隔 |

## 预览版命令

以下命令使用 `0.7.1` 预览版：

| 命令 | 用途 |
| --- | --- |
| `ltc wait --task <任务ID>` | 在 Claude Code 中等待并输出回调 |
| `ltc retry --id <回调ID>` | 重新投递保存的回调 |
| `ltc agent pi -- <任务>` | 启动 Pi 子 Agent |
| `ltc pi` | 打开 LTC 托管的 Pi 会话 |
| `ltc template list` | 查看已注册模板 |
| `ltc desktop status` | 查看 Desktop 连接配置 |
| `ltc desktop launch` | 通过共享 Core 启动 Desktop |

多阶段目标的命令顺序见[目标指南](../guides/goals.md)。
