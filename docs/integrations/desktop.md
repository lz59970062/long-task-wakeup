# 接入 Codex Desktop

正常启动 Desktop 即可。在 Desktop 会话中，`auto` 会选择 `codex-queue`：任务结束后，协调器使用你所在会话的环境和 Codex 配置，运行一次公开命令 `codex queue --thread UUID --message TEXT`。Desktop 不需要启动器、共享 Core、私有管道，也不需要重启。

## 用小任务检查

完成[预览版安装](../installation.md#安装预览版)，然后从 Desktop 会话提交一个任务：

```bash
ltc setup --force --enable --now
ltc run --cwd "$PWD" --task "Desktop 回调示例" -- python -c "print('Desktop callback ready')"
```

提交时会打印 `ltc: callback mode: codex-queue`。PowerShell 工具环境中将 `--cwd "$PWD"` 换成 `--cwd "$($PWD.Path)"`。如果 Desktop 没有标记会话来源，请加上 `--callback-mode codex-queue`。

会话忙时，Codex 会等当前回合结束再处理这条消息；会话已关闭时，再次打开就会收到。

## 各状态的含义

进入队列不等于已经收到。回调会一直保持待处理，直到会话执行其中的 ACK 命令，`ltc status` 显示 `in the Codex queue; waiting for the thread to ACK`。等待 ACK 的回调不会阻塞同一会话后续的回调。

| 发生的情况 | LTC 的处理 | 你要做的 |
| --- | --- | --- |
| `codex queue` 报错，例如会话不存在 | 连同错误信息移入 `failed`；消息没有进入队列 | 修复原因后运行 `ltc retry --id ID` |
| 无法启动 `codex` | 同上，包括 Windows 无法解析可执行文件 | 修正路径或设置 `CODEX_LONG_TASK_WAKEUP_CODEX_BIN`，然后重试 |
| 命令被杀死、超时，或成功退出但输出无法识别 | 结果未知：移入 `failed`，该会话后续回调等待 | 查看会话后运行 `ltc ack --id ID` 或 `ltc cancel --id ID`；`retry` 会被拒绝 |

LTC 在队列目录的 `codex-queue/` 中保存回执，不会自动把同一个回调发送两次。

## ACK 与沙箱

ACK 按会话自身的权限运行。如果沙箱不能写 LTC 队列目录（默认 `~/.ltc`），命令会失败，回调继续保持待处理。请批准该命令、把目录加入沙箱可写路径，或在终端中执行 ACK。在此期间不会重复发送。

## 限制

已在 Linux 上用真实 Codex 0.166.0 验证，打开的会话使用 TUI，见[验证记录](https://github.com/lz59970062/long-task-wakeup/blob/main/docs/validation-codex-queue.md)。真实 Desktop 应用、macOS 和 Windows 尚未运行过。macOS 优先使用应用内置的 Codex，`CODEX_LONG_TASK_WAKEUP_CODEX_BIN` 可以覆盖。可执行文件和配置目录在提交时固定。`--thread` 需要会话 UUID：绑定到会话名称的回调会连同错误信息移入 `failed`，可用 `ltc retry --id ID --callback-mode cli` 迁移。消息通过命令行传递，命令运行期间本机其他用户可以在进程列表中看到；特别长的 `--callback-format full` 消息可能超过系统参数长度限制，默认的紧凑格式很短。

使用这条路径前先升级协调器：新记录是 callback 版本 5 和 task 版本 6，旧协调器会保留它们而不发送，也不会去恢复该会话。

## 迁移为旧启动器保存的回调

启动器和共享 Core 已退役。`ltc setup` 不再生成它们，新任务使用 `--callback-mode desktop` 会被拒绝。已保存的 `desktop` 回调保留结果和原路径：连接仍可用时照常投递，否则留在 `failed` 中。用以下命令迁移：

```bash
ltc retry --id ID --callback-mode codex-queue
```

这不会重新运行任务。退出 Desktop 后正常打开即可；`ltc setup` 会列出遗留的启动器文件，可以删除。
