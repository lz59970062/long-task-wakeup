# 接入 Codex Desktop

Codex Desktop 接入使用 `0.7.1` 预览版的共享 Core 启动器。Desktop 和 LTC 通过同一个 Core 访问会话，任务结束后把回调送回桌面应用。

## 生成启动器

在 macOS 或 Windows 上完成[预览版安装](../installation.md#安装预览版)，然后运行：

```bash
ltc setup --force --enable --now
ltc desktop status
```

macOS 会生成 `Start LTC Desktop.command`；Windows 会生成 `Start LTC Desktop.cmd` 和对应的 PowerShell 脚本。默认位置是用户 Codex 配置目录下的 `long-task-wakeup` 文件夹。

## 启动 Desktop

保存当前工作并退出 Desktop，再双击生成的启动器，或运行：

```bash
ltc desktop launch
```

之后从这个 Desktop 会话提交一个小任务：

```bash
ltc run --callback-mode desktop --cwd "$PWD" --task "Desktop 回调示例" -- python -c "print('Desktop callback ready')"
```

让 Agent 读取结果并执行回调中的 ACK，完成一次接入检查。PowerShell 工具环境中将 `--cwd "$PWD"` 换成 `--cwd "$($PWD.Path)"`。

## 调整应用路径

macOS 同时安装多个应用时，可以明确选择：

```bash
ltc desktop prepare --app /Applications/Codex.app --force
```

Windows 默认使用直接启动方式。Store 包上下文启动是实验选项，配置命令是：

```powershell
ltc setup --desktop-launch-mode package-context --force --enable --now
```

各平台的启动步骤、运行条件和验证记录见 [macOS Desktop](https://github.com/lz59970062/long-task-wakeup/blob/main/docs/macos-desktop-bridge.md)与 [Windows Desktop](https://github.com/lz59970062/long-task-wakeup/blob/main/docs/windows-desktop-bridge.md)。应用升级后，再用上面的示例检查回调。
