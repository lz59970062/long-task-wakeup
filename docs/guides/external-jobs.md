# 接入已有任务

作业已经由 Slurm、tmux 或其他运行环境启动时，在结束处调用 `ltc done`，就能把结果送回 Agent 会话。

## 报告一次运行结果

在原 Agent 会话中，下面的命令会创建一条回调：

```bash
ltc done --cwd "$PWD" --task "生成数据索引" --exit-code 0 --message "索引保存在 outputs/index.json"
```

`--exit-code` 填实际退出码，`--message` 写产物位置或交接说明。Agent 收到回调后就可以打开文件，继续使用结果。

## 放进作业脚本

把原会话 ID 填入脚本，在业务命令结束后保存退出码，再报告给 LTC。下面是 Bash 示例：

```bash
#!/usr/bin/env bash
set +e
python train.py --config configs/exp.yaml
job_exit=$?
ltc done --agent codex --session "填入原会话ID" --cwd "$PWD" \
  --task "Slurm 训练" --exit-code "$job_exit" \
  --message "日志和 checkpoint 保存在 outputs/exp/"
exit "$job_exit"
```

会话 ID 可在发起作业的 Codex 工具环境中通过 `CODEX_THREAD_ID` 获取。Claude Code 使用 `CLAUDE_CODE_SESSION_ID`。把它保存进提交脚本，回调就有明确的接收位置。

在 Slurm 中，LTC、协调器和接收回调的 Agent 需要能访问同一份队列与会话文件。使用共享目录时，在 `setup`、`done` 和 `ack` 中传入相同的 `--queue-dir`。

## 只配置回调服务

如果这台机器只负责上报外部任务，可以这样初始化：

```bash
ltc setup --callback-only --enable --now
```

接收结果后的操作与其他任务一致，参阅[接收与确认回调](callbacks.md)。
