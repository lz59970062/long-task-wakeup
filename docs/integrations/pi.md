# 接入 Pi

Pi 接入使用 `0.7.1` 预览版。Pi 既可以接收自己提交的任务结果，也可以作为子 Agent 执行独立工作。

## 安装扩展

按[预览版安装步骤](../installation.md#安装预览版)安装 LTC，然后执行：

```bash
ltc setup --with-pi-extension --enable --now
```

在已经打开的普通 Pi 会话中执行 `/reload`，或重新启动 Pi。扩展和技能会安装到 Pi 配置目录，默认是 `~/.pi/agent`。

## 在 Pi 中提交任务

让 Pi 执行：

```bash
ltc run --cwd "$PWD" --task "整理数据" -- python prepare_data.py
```

LTC 将任务绑定到 `PI_SESSION_FILE` 指向的会话。结果到达后，扩展把回调送入这个 Pi 进程。Pi 空闲时开始处理回调；正在工作时，在当前工具调用结束后接入这条消息。

读完产物后，让 Pi 执行回调中的 ACK 命令。

## 使用 LTC 启动 Pi

需要在 Pi 关闭后恢复会话时，用 LTC 启动一个受托管的会话：

```bash
ltc pi --cwd "$PWD"
```

继续已有会话时，给出完整路径：

```bash
ltc pi --session /absolute/path/to/session.jsonl
```

## 把工作交给 Pi

在原 Agent 会话中提交：

```bash
ltc agent pi --cwd "$PWD" --task "检查数据处理" -- "阅读 prepare_data.py，检查缺失值、重复记录和输出格式，报告具体问题。"
```

子任务回答会保存在 LTC 任务目录中，路径随提交输出和回调一起提供。模型参数、系统提示词和会话恢复细节见 [Pi 实现资料](https://github.com/lz59970062/long-task-wakeup/blob/main/docs/pi.md)。
