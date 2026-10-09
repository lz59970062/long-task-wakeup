<p align="center">
  <img src="./assets/readme/hero.svg" width="100%" alt="Long Task Callback (ltc)">
</p>

# Long Task Callback（中文说明）

[English](README.md) · 原名 `codex-long-task-wakeup`（旧命令仍可用）

**Long Task Callback (ltc)** 有三个入口：

- **Run**：`ltc run -- <命令>`。提交新任务，Linux 默认使用独立的 systemd 用户服务，macOS 使用独立的一次性 launchd 作业，Windows 使用独立的按需计划任务与 Job Object。
  Linux/macOS 无可用用户管理器时使用 screen 兼容后端。
- **Agent（预览）**：`ltc agent codex|claude -- <任务>`。用同一套持久化和 callback
  生命周期启动一个全新的 Codex 或 Claude Code 子代理。
- **Done**：`ltc done ...`。任务已经由 screen、tmux、Slurm 或其他调度器托管时，
  只报告结束并投递 callback。

正确的职责关系是：

```text
systemd 用户服务 / macOS LaunchAgent / Windows 用户登录计划任务
  └─ ltc daemon                 负责控制、恢复和 callback 投递

独立任务服务（旧任务保留 screen）
  └─ LTC worker
      └─ 训练任务
```

当前 0.7.0 正式版支持 Linux、macOS 和 Windows：任务执行、Agent 适配、状态存储和回调格式分别维护。
原生任务服务独立于回调协调器；协调器重启后核对已有结果与进程归属，不重复启动。
旧 screen 任务继续使用原后端，但不承诺停止其所在系统服务后仍然存活。

回调默认只发送任务、状态、产物位置、绑定会话和 ACK 命令。完整命令及交接文字保存在
`details/<id>.md`；自定义指令和恢复说明会提示先读详情。`--callback-format full` 可恢复
完整内联格式。系统和用户提示仍按 4/3 间隔出现，原文不会被自动改写。

Mac 安装方式见 [macOS 指南](docs/macos.md)：`ltc setup --force --enable --now` 自动选择
LaunchAgent 协调器和独立 launchd 任务。任务作业不注册为登录启动项，不会在重启后自动重跑。
`codex/macos-desktop-callback` 分支的 0.7.1 预览会在 Mac 和 Windows 按当前用户与 Python 安装位置自动生成 Desktop 启动器；
资源随安装包分发，不依赖源码仓库或 `examples`。Mac 生成 `.command`，Windows 生成 `.cmd` 和 `.ps1`，
统一使用 `ltc desktop prepare|launch|status`。详见 [Mac](docs/macos-desktop-bridge.md) 和
[Windows](docs/windows-desktop-bridge.md) 共享 Core 安装说明。正式版 `v0.7.0` 尚无该生成功能。
Windows 10+ 安装方式见 [Windows 指南](docs/windows.md)：用户登录状态下使用
`ltc setup --service windows-task --force --enable --now`，通过 Agent CLI 回到原会话。
PowerShell ACK 命令支持空格、中文与单引号路径；状态文件在创建时设置当前用户和 SYSTEM 私有 ACL。
Windows 目录创建、重命名和删除不承诺断电持久性；注销或重启可能中断任务，未知结果不会自动重跑。
每个 Agent profile 使用一个协调器队列，换队列前先排空并卸载。
默认 CLI 回调仍可能被打开的 Desktop 会话的 active-writer 检查阻止。
2026-09-27，[实验桥接启动器](docs/windows-desktop-bridge.md)的 `-PackageContext` 路径
已在本机通过实际界面启动、App Tools 调用，以及两分钟任务的原会话回调和 ACK；业务任务没有重跑。
详见 [Windows 验证记录](docs/validation-windows.md)。默认直接启动 Store 版 Desktop 仍报错误 5。
该选项通过微软诊断工具赋予小型 Python helper 包身份，不修改持久环境或包调试策略，也不使用
`-PreventBreakaway`；它的 token 和其他应用行为不保证等同于正常激活。完整预检还会拒绝运行中的 Desktop。
Windows Desktop 包上下文桥接仍属于实验功能，核心正式发布不代表所有 Desktop 投递方式均已验证。
新预览可用 `setup --desktop-launch-mode package-context` 保存该选择；默认直接启动不会失败后自动切换。
这次安装流程改动在 macOS 开发，尚未完成 Windows 原生安装验收，历史回调成功不能替代这项验收。
PI、DSH 适配尚未实现。从 0.6 升级时，先排空已有任务并升级协调器，再提交原生任务；
并行安装请使用独立 Agent profile、队列和对应版本 daemon，避免混用。

旧版 screen worker 启动时必须把任务从 `launching` 持久化为 `running`，这一步就是启动握手。
LTC 等待一秒；若 screen 在握手前消失，则记录失败并按指数退避重试，最多三次。最终失败后任务
进入 `launch_failed`，只生成一次恢复 callback，且不再自动启动。worker token 固定添加
`ltc_` 前缀，并以单个 `--token=value` 参数传递，避免以 `-` 开头的值被误解析为新选项。
升级时，缺少尝试计数的旧版 `launching` 记录会被视为历史启动失败，不会自动重跑。

`ltc setup` 会创建用户可编辑的固定提示词钩子
`${CODEX_HOME:-~/.codex}/long-task-wakeup/callback-hook.md`。该文件已有内容时不会被覆盖，
即使使用 `setup --force` 也是如此。默认首次及其后每 3 条回调显示一次用户提示，
到期投递（包括这条回调的重试）会重新读取文件，修改后无需重启 daemon。非空内容会以
`[long-task-callback-user-hook]` 段落追加到 callback 提示词；文件缺失、为空或暂时不可读时，
原 callback 仍会正常投递。

安装时还会检查 Claude Code CLI，并在隐藏输出的情况下运行 `claude auth status`。该检查只做
提示，不会阻断安装，也不会打印凭据值或账户信息；Claude 未配置时，普通命令和 Codex 功能仍然
可以使用。

使用原则：只有可靠地在 60 秒内结束的命令才允许前台等待一次。预计约一分钟以上、耗时不确定，
或可能需要第二次状态检查时，从一开始就使用 `ltc run`。几分钟任务也默认走 callback，
不要为了维持模型缓存而轮询；执行器和 daemon 的等待不产生模型回合。

```bash
ltc run --cwd "$PWD" --task "train model" \
  -- python train.py --config configs/exp.yaml
```

命令会打印 task id、执行后端和日志路径。以下 screen 命令仅用于兼容后端：

```bash
screen -ls
screen -r ltc-<task-id>
tail -f ~/.codex/long-task-wakeup/tasks/<task-id>/attempt-1.log
```

`0.6.5` 的 Agent 模式沿用 `run` 的命令语言：

```bash
ltc agent claude --cwd "$PWD" --task "review parser" \
  -- "Inspect parser.py and report concrete defects."

ltc agent codex --cwd "$PWD" --task "fix parser" \
  -- "Fix the confirmed defects and run the relevant tests."
```

`codex|claude` 选择被启动的子代理；callback 目标仍由启动现场自动识别，必要时继续使用已有的
`--agent` 和 `--session` 显式绑定。提示词和最终回答文件由 LTC 自动放入私有任务目录，用户
无需传入路径。Claude 子代理继承提交时的认证、配置、代理和自定义环境，但不会继承
`CODEX_THREAD_ID`、`CLAUDE_CODE_SESSION_ID` 和 `CLAUDECODE` 这些父会话标记；默认也不会
启用 `--bare`。

Claude Code 必须在实际提交 `ltc agent claude` 的 shell 中配置好，可先运行
`command -v claude` 和 `claude auth status`。OAuth/keychain 不要求设置 `ANTHROPIC_API_KEY`；
也可以使用 `ANTHROPIC_API_KEY`、可选的 `ANTHROPIC_BASE_URL`，或 Claude Code 支持的云厂商
认证。LTC 会继承提交时存在的 `CLAUDE_CONFIG_DIR`、代理、证书和其他自定义环境；安装 daemon
时的环境不能代替提交子代理时的环境。

主机重启后 screen 不会保留。LTC 不自动重跑，而是恢复最初绑定的 Codex 或 Claude Code
会话，把任务、日志、checkpoint 相关上下文和中断原因交回该 agent。agent 自己检查本地产物，
再按标准流程从有效 checkpoint 重建任务、补充完成状态，或记录明确阻塞。没有
`--resume-command`。跨主机恢复不支持，因为它需要额外传输工程、数据、环境、checkpoint 和资源。

两层 ACK 都保留：

1. `ltc ack` 表示本次 callback 已收到并检查；
2. `ltc goal ack --state completed|blocked_conditions` 表示整个阶段目标完成或满足阻塞条件。

从 0.6.2 开始，goal 必须绑定一个可修改的 YAML 目标计划文件：

```yaml
version: 1
revision: 1
goal: 完成并发布 0.6.2
path:
  - id: implement
    title: 实现功能
    status: completed
  - id: verify
    title: 验证功能和回归测试
    status: in_progress
  - id: publish
    title: 发布版本
    status: pending
amendments:
  - revision: 1
    reason: 初始执行路径
```

`path` 是有顺序的小目标路径，状态可为 `pending`、`in_progress`、`blocked` 或
`completed`；已完成项必须构成连续前缀。用户可以修改后续小目标、重新打开前面的步骤、增加或
删除步骤，也可以修改顶层大目标。建议同时递增 `revision`，并在 `amendments` 记录修改原因。
后续提醒与检查始终读取文件的最新版本，而不是沿用 AI 记忆中的旧目标。

```bash
ltc goal start --id release-goal --session <session-id> --cwd "$PWD" \
  --task "发布 0.6.2" --plan-file goal-plan.yaml
ltc goal check --id release-goal
ltc goal ack --id release-goal --state completed --plan-sha256 <本次检查输出的摘要>
```

AI 在回复“目标已完成”之前必须执行 `goal check`，并把实际工作和产物逐项对照最新 YAML。
只有最后一个项目以及之前所有项目均确认 `completed`，且完成命令携带本次检查的文件摘要时，
CLI 才接受 goal 完成。文件一旦修改，旧摘要立即失效，必须重新检查新路径。
0.6.1 已存在的活跃 goal 可用
`ltc goal set-plan --id <goal-id> --plan-file goal-plan.yaml` 绑定或更换计划文件；绑定后旧检查
记录会被清除。

每个独立 goal 使用一个独立文件，推荐路径为 `.ltc/goals/<goal-id>.yaml`。目标和路线清晰、
风险低时，AI 可以直接起草文件，告知用户文件位置和主要步骤后继续，不必额外停下来等待确认；
如果涉及路线选择、明显的资源变化、验收标准变化或其他重要取舍，应先和用户协商。阻塞、恢复和
修订继续使用同一文件，终态完成后衍生出的任务则创建新 goal 和新文件。完成文件默认留在原路径
作为审计记录，不自动删除或复用；如需移入归档目录，应在 goal 仍活跃时移动文件，使用
`goal set-plan` 更新路径，再执行最后一次检查和完成 ACK。

callback ACK 不会顺带完成 goal。活跃 goal 默认三小时没有新 callback 时，daemon 会自动恢复
原会话问询阶段状态；重启恢复后也遵守同一规则。
普通 ACK 只写 callback queue；全局 target-lock 的清理由 daemon 完成，不需要给恢复后的
agent 扩大文件系统写权限。
ACK marker 一旦存在，completion stream 的等待必须立即结束并释放 delivery lease，
不能继续等待 Desktop App Server 的 `turn/completed` 通知。

旧脚本中的 `--via-daemon` 仍可作为隐藏的无效果兼容参数使用，但新命令不应再写它。`run`
和 `done` 已经固定走 daemon，不存在直接由 agent 回合执行或投递的模式。

## 许可证

MIT
