# LTC 0.7.0：Windows 完成后的开发交接

交接日期：2026-09-27。后续统一在 **`codex/0.7.0-preview`** 上继续开发。
Windows 临时分支 `codex/windows-support` 已快进合入此分支；不要再次从旧 Mac
基线开始 Windows 适配。临时分支保留作历史引用，不作为后续开发入口。

## 接手基线

| 项目 | 状态 |
| --- | --- |
| 仓库 | `git@github.com:lz59970062/long-task-wakeup.git` |
| 统一分支 | `codex/0.7.0-preview` |
| Mac 交接基线 | `d2974cd` |
| 已验证的功能提交 | `dccd7ed5f5c9f3989e03ab072337f0f14054f5f5` |
| 包版本 | `0.7.0a1`；未发布新包、未打发布标签 |
| 本次合并方式 | 快进，保留全部 Windows 开发和修复提交 |
| 本地目录 | `D:\long-task-wakeup` |

交接文档提交位于上述功能提交之后。接手时记录实际 `git rev-parse HEAD`，
不能仅凭 `ltc --version` 判断代码是否已更新。

```powershell
git clone --branch codex/0.7.0-preview git@github.com:lz59970062/long-task-wakeup.git
cd long-task-wakeup
py -3 -m venv .venv
& .\.venv\Scripts\python.exe -m pip install -e .
& .\.venv\Scripts\ltc.exe --version
```

已有 checkout：先确认工作区，再 `git fetch origin`、
`git switch codex/0.7.0-preview`、`git pull --ff-only origin codex/0.7.0-preview`。
不要重置或覆盖其他人的未提交工作。当前 Windows checkout 的 origin 已改用 SSH。

## 已交付能力及代码入口

- Windows 原生任务所有者：`platforms/windows.py`、`windows_scheduler.ps1`。
  每次业务尝试由独立 Task Scheduler 作业运行，不依赖 WSL、screen 或当前 Agent 回合。
- 协调器：`windows_service.py`，支持安装、启动、重载、排空后移除，以及独立业务任务存活。
- 文件与进程：`platforms/windows_io.py`、`windows_process.py`、`windows_tcp.py`。
  包括私有 ACL、原子写入、跨进程锁、SID/boot/进程创建时间校验和 Job Object 子进程回收。
- Desktop 实验桥接：`desktop_core.py` 保留 Desktop 注入的工具环境和 Core 参数；
  `desktop_bridge.py` 提供经本机用户身份校验的共享 App Server；
  `desktop_connection.py` 校验连接元数据和 profile。
- 0.7.0 的打包版启动入口：`examples/windows/start-desktop-bridge.ps1` 及同目录辅助脚本。
  0.7.1 本地预览已将实现移入安装包 `desktop_windows_assets/`，由统一的
  `ltc setup` 生成本机 `.cmd`/`.ps1`，通过 `ltc desktop` 管理；examples 保留兼容入口。
  新安装流程仍待 Windows 原生验收，不能沿用旧版本回调结果作为通过证据。
- 共用修复：`cli.py` 保留短轮询超时前的 WebSocket 帧及消息分片，防止丢失完成通知；
  `templates.py` 规范化来源路径，兼容 macOS `/var` 与 `/private/var` 别名。
- CI 收敛修复：兼容 Python 3.9 的 socket 超时异常；桥接启动 JSON 使用 ASCII 转义，
  避免旧 Windows 输出编码遇到中文路径后退出；拒绝将 cmd 内建命令当作可执行转发目标。
  测试按实际身份比较 Windows SID 别名，并规范化短路径。

以上源码路径均相对于 `src/long_task_callback/`，除非另有说明。
根目录 `SKILL.md` 与 `src/long_task_callback/skill/SKILL.md` 要同步维护。

## 验证结果和边界

功能提交 `dccd7ed` 的 [GitHub Actions 验证](https://github.com/lz59970062/long-task-wakeup/actions/runs/36256072908)
**7/7 作业通过**：

| 平台 | 验证内容 |
| --- | --- |
| Linux，Python 3.9 / 3.12 | 全量测试发现 418 项；systemd 用户服务、screen 真实生命周期均通过 |
| macOS，Python 3.9 / 3.12 | 全量测试发现 418 项；launchd、screen 真实生命周期均通过 |
| Windows，Python 3.9 / 3.12 | 全量测试发现 418 项，18 项按条件跳过；另运行 Scheduler 13 项及生命周期 2 项，均通过 |
| 非 root Linux 容器 | 镜像构建通过；禁网、带 init 的容器执行 10 项集成测试，3 通过、7 项其他平台/未启用用例跳过 |

平台专属用例不会在其他平台执行；不能把“418 项发现”写成“418 项全部实际通过”。
CI 不包含真实 AI 模型回调或实际 Desktop GUI 验收。

本机 Windows 10 build 19045、Python 3.14.3 曾完成全量原生检查：
418 项中 406 通过、12 跳过，含真实 Core 协议测试；最后一轮 CI 修复另通过 60 项针对性测试。
本机测试使用独立 profile、队列、锁目录和临时目录，执行身份是普通登录用户。
测试子进程须清除 `CODEX_LONG_TASK_WAKEUP_DESKTOP_BRIDGE_FILE`，并将
`CODEX_LONG_TASK_WAKEUP_DESKTOP_APP_SERVER=0`，避免继承正在使用的 Desktop 桥接。
桥接集成测试会单独启用自己的测试连接。

真实原会话验收：任务 `1aa2ba13` 执行 120.0003 秒、exit=0、业务只启动一次。
Desktop 经显式 `-PackageContext` 重启后，旧任务仅重新投递回调；原会话读取结果后
于 2026-09-27 00:13:29 +08:00 写入 ACK，协调器将回调归档到 `done`。
GUI、一次 App Tools 调用及原会话 receipt/ACK 均已验证。
私有证据在 `.windows-dev/live-demo/`，包括 `callback-received.json`、任务日志及队列记录；
这些运行状态被 Git 忽略，不会随 clone 到达其他机器。

## 本机部署和维护注意事项

- 本机 `.venv`、源码目录和已安装任务使用绝对路径，不能随意移动或删除。
- 本轮使用的协调器名称为 `ltc-windows-validation`，队列为
  `D:\long-task-wakeup\.windows-dev\live-demo\queue`。
  注册配置在当前用户 Codex profile 的 `long-task-wakeup/windows-ltc-windows-validation.json`。
  本次配置使用 `--now`，没有启用登录触发；接手时重新检查实际运行状态。
- Desktop 桥接元数据位于当前用户 Codex profile 的 `long-task-wakeup/desktop-bridge.json`。
  不要把过期 PID 当成所有权证据；必须通过 native identity/profile 校验。
- profile 只使用一个协调器队列。已有部署先检查任务和投递是否排空，再变更队列或重装。
  不要直接用默认 setup 覆盖本机验证队列。
- 文件更新不代表已运行的 Python 协调器或 Desktop Core 已热加载全部新代码。
  需要升级运行进程时，先排空活跃投递，再按对应安装文档重启；不要为核对状态强行关闭 Desktop。
- 私有配置、令牌、Agent 登录资料、完整提示词及会话记录不得提交到 Git。

## 仍保留的限制

1. Windows Desktop 桥接仍是显式启用的实验功能。验证版本为 Desktop 26.915.4065.0、
   Core 0.155.0-alpha.9.2；没有承诺所有版本和所有 App Tools 流程兼容。
2. 测试的 Store 版 Desktop 默认直接启动路线仍报 Win32 error 5。
   `-PackageContext` 使用微软诊断工具的包上下文；其 token 和其他行为不保证等同正常激活。
   先阅读 [桥接文档](windows-desktop-bridge.md)，预检成功且用户保存工作、关闭 Desktop 后再启动。
3. 默认 CLI resume 仍可能受打开的 Desktop 会话的 active-writer 约束。
   不得删除 writer 锁、换成 `--last`、伪造 ACK 或重跑已完成的业务任务掩盖投递失败。
4. Windows 依赖已登录的交互用户；不是注销后仍工作的系统服务。注销、重启可中断任务，
   不自动重跑未知结果；不保证 Windows 目录变更的断电持久性。
5. WSL 部署按独立 Linux 环境管理；不提供跨主机迁移恢复。PI/DSH 尚未实现。

## 可直接交给接手 Agent 的任务说明

> 在 `codex/0.7.0-preview` 继续维护 LTC 0.7.0a1，不要再创建 Windows 专用开发分支。
> 先读本交接、`docs/windows.md`、`docs/windows-desktop-bridge.md`、
> `docs/validation-windows.md`、`docs/architecture.md` 和根目录 `SKILL.md`。
> 核对实际 HEAD、本机协调器队列及 Desktop 桥接状态，保留正在运行的任务和用户配置。
> Windows 原生执行及指定 Desktop 路线的真实回调已完成，不要重复实现或重复跑旧业务任务。
> 后续按用户指定范围开发；共用生命周期修改必须复验 Linux、macOS、Windows 和容器 CI。
> 将实验桥接转为默认支持或发布新版本之前，另行验证安装/升级、不同 Desktop 版本、
> App Tools/审批流程、退出清理和原会话 ACK。当前没有授权自动发布稳定包。
