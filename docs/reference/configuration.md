# 配置与文件位置

LTC 保存任务命令、日志、执行结果和回调记录。需要排错或继续工作时，可以从这些文件找到上次运行的信息。

## 默认目录

| 位置 | 内容 |
| --- | --- |
| `~/.ltc/tasks/<task-id>/task.json` | 命令、工作目录、后端和会话绑定 |
| `~/.ltc/tasks/<task-id>/attempt-1.log` | 第一次执行的输出日志 |
| `~/.ltc/tasks/<task-id>/result.json` | 退出码与执行结果 |
| `~/.ltc/queue/` | 回调及其投递状态 |
| `~/.ltc/callback-hook.md` | 用户追加的回调提示词 |
| `~/.ltc/callback-prompts.yaml` | 回调提醒间隔 |
| `~/.config/ltc/templates/` | 可复用的任务模板 |

子 Agent 的回答文件和回调详情文件路径会出现在提交输出或回调里。按给出的路径读取即可。

## 常用环境变量

| 变量 | 用途 |
| --- | --- |
| `LTC_HOME` | 设置 LTC 状态目录，默认 `~/.ltc` |
| `CODEX_HOME` | Codex 配置与会话目录，默认 `~/.codex` |
| `CLAUDE_CONFIG_DIR` | Claude Code 配置目录，默认 `~/.claude` |
| `PI_CODING_AGENT_DIR` | Pi 配置目录，默认 `~/.pi/agent` |
| `LTC_TEMPLATE_DIR` | 自定义模板目录 |

`~` 表示当前用户的主目录；Windows 中对应用户配置文件目录。

## 使用自定义队列

先确定一个绝对路径，然后在安装、提交和确认时使用同一个目录：

```bash
ltc setup --queue-dir /absolute/path/to/ltc-queue --enable --now
ltc run --queue-dir /absolute/path/to/ltc-queue --task "测试" -- python -m pytest
ltc status --queue-dir /absolute/path/to/ltc-queue
```

切换已有协调器的队列时，先处理完原队列里的工作，再重新配置。并行运行多套 LTC 时，为每套配置独立的 Agent profile 和队列。

## 修改回调提示词

编辑 `callback-hook.md`，写入每次处理结果时希望 Agent 遵循的习惯。LTC 会在到期的提醒中读取文件最新内容。设置提醒间隔：

```bash
ltc prompt-policy --system-every 4 --user-every 3
```

两个数字分别控制系统提示和用户提示的间隔。设为 `1`，表示每条回调都附带对应提示。
