# 接收与确认回调

回调告诉 Agent 哪项任务结束了、结果放在哪里，以及如何确认收到。Agent 先读取结果，再执行回调给出的 ACK 命令。

## 阅读回调

通常会用到这些信息：

| 信息 | 用途 |
| --- | --- |
| 任务名称和状态 | 找到对应的工作，了解执行情况 |
| 日志路径 | 查看命令输出和错误信息 |
| 回答或产物路径 | 读取子 Agent 回答、报告或结果文件 |
| Details 路径 | 查看完整命令和交接要求 |
| ACK 命令 | 确认这次结果已经收到并检查 |

默认回调使用紧凑格式。需要把详细内容直接放进回调时，在提交命令中加 `--callback-format full`。

## 确认收到

直接执行回调中的完整命令，默认形式是：

```bash
ltc ack --id <callback-id>
```

使用自定义队列时，回调里的命令还会带上 `--queue-dir`。保留这个参数，就会写入对应队列的确认记录。

## 查看待处理回调

```bash
ltc status
ltc status --state pending --state failed
```

`pending` 表示正在等待投递，`failed` 中的回调需要结合错误记录处理。在 `0.7.1` 预览版中，修复连接后可以重新投递已有回调：

```bash
ltc retry --id <callback-id>
```

这条命令再次发送已保存的结果。需要运行新的业务命令时，使用 `ltc run` 提交新任务。

## 添加自己的交接习惯

编辑 `~/.ltc/callback-hook.md`，例如写入：

```text
读取结果后，先告诉我产物路径和主要结论，再继续下一步。
实验任务请同时列出配置文件和关键指标。
```

设置每次回调都带上这段文字：

```bash
ltc prompt-policy --user-every 1
```

查看当前提醒间隔时运行 `ltc prompt-policy`。文件位置和环境变量见[配置参考](../reference/configuration.md)。
