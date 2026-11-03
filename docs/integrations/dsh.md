# DeepSeek Harness（DSH）接入

[← 返回 README](../../README.zh-CN.md)

LTC 与 DeepSeek Harness（`dsh`，桌面应用或 CLI）双向配合：DSH 可以作为**子 Agent**
（`ltc agent dsh`），正在运行的 DSH 会话也可以作为**回调接收方**，在任务结束后被唤醒。

## 安装

```bash
ltc setup --with-dsh --force --enable --now
```

`--with-dsh` 会把专用技能安装到 `${DSH_HOME:-~/.dsh}/skills/long-task-callback`，并把 `dsh`
启动器记录到协调服务。桌面应用没有把 `dsh` 放进 `PATH` 时，显式指定：

```bash
ltc setup --with-dsh --dsh-bin "/Applications/DeepSeek Harness.app/Contents/Resources/runtime/cli/bin/dsh" \
  --force --enable --now
```

DSH 会监视 `~/.dsh/skills` 和项目的 `.dsh/skills`，新会话无需重启即可看到技能。检查状态：

```bash
ltc doctor --agent dsh --session "$DSH_SESSION_ID"
```

## DSH 作为回调接收方

在 DSH 的 Bash 工具中，`ltc run` 会从 `DSH_SESSION_ID` 自动绑定当前会话：

```bash
ltc run --cwd "$PWD" --task "train model" -- python train.py
```

stderr 会打印等待命令：

```text
ltc: DeepSeek Harness live callback: run as a background job (run_in_background: true): ltc wait --queue-dir … --task <id>
```

用 Bash 工具的 `run_in_background: true` 运行它。DSH 会在跨轮次保留该后台任务，并在它退出时
通知本会话；`ltc wait` 在回调入队后立即打印回调。用 `job_output` 读取已完成任务的输出，检查
结果后执行回调中的 ACK 命令。

### 为什么没有 headless 兜底

DSH 的一次性运行器（`dsh --profile headless --session-id …`）会拒绝桌面 preset 创建的会话：

```text
dsh: session "session-…" runs under agent preset "standard", which the one-shot runner does not compose
```

同时，第二个 writer 会与正在运行的 DSH 会话锁冲突。因此 LTC **不会 headless 恢复 DSH 会话**：

- 有 `ltc wait` 存活时，守护进程把该会话的回调交给它；
- 没有 waiter 时，回调保持排队，直到原会话再次运行 `ltc wait`；`ltc status` 仍能看到它；
- DSH 不会出现 wait 退出码 4；退出码 3 表示等待超时，回调仍在队列中。

这与普通 Pi 会话的保证一致：只在原进程存活时投递，且 LTC 不会猜测替代 owner。一次性
`dsh --profile headless` 运行在回答后即退出，请勿在其中提交 LTC 任务；请使用存活的桌面会话或
交互式 CLI 会话。

## DSH 作为子 Agent

```bash
ltc agent dsh --cwd "$PWD" --task parser-review -- "Inspect parser.py and report concrete defects."
```

LTC 以 stdin 传入提示词、运行 `dsh --profile headless`，并从 stdout 读取最终回答。headless
profile 不使用桌面应用保存的登录状态：需要设置 `DEEPSEEK_API_KEY`，或按 DSH 文档在 headless
profile 中恢复凭据存储，否则子任务会报告认证错误。

子任务运行在 DSH 默认的 `workspace-write` 沙箱里，也没有审批通道，所以只能写 `--cwd` 内的文件。
它的结果就是最终回答（`agent-result.txt`）。

要让 DSH 使用自己的端点，在 `~/.dsh/cordis.patch.yml` 中声明路由。它对所有 profile 生效，`acp`
需要单独选择模型：

```yaml
- id: llm-pi-ai
  config:
    providers:
      my-gateway:
        apiKeyEnv: MY_GATEWAY_API_KEY
        api: openai-completions
        baseURL: https://gateway.example/v1
        models:
          - id: my-model
- id: agent-default-model
  config: {provider: my-gateway, model: my-model}
- id: acp
  config: {provider: my-gateway, model: my-model}
```

| 选项 | DSH 行为 |
| --- | --- |
| `--model` | 不支持；模型由 compose profile 选择 |
| `--reasoning-effort` | 不支持 |
| `--system-prompt-file` | 不支持 |
| `LONG_TASK_WAKEUP_DSH_BIN` / `setup --dsh-bin` | 指定 `dsh` 启动器 |

子任务移除 `DSH_SESSION_ID`、`DSH_SHELL` 和 `DSH_WEB_URL`，避免子进程绑定到父会话；保留
`DEEPSEEK_API_KEY`、`DSH_HOME` 和 `DSH_PROFILE`，它们决定子进程自己的配置和凭据。

在 DSH 默认的 `workspace-write` 模式下，`~/.ltc` 对会话只读。`ltc run`、`wait` 和 `ack` 会被拒绝，
模型需要带 `sandbox_permissions` 重试。技能里写了这条规则，每次重试都需要审批一次。

真实 DSH 会话的提交、作业通知和 ACK 已在 macOS 桌面端和 Linux（`dsh` 0.2.1-rc.5，通过 ACP，含子 Agent）验证。Windows 尚未对接真实 DSH。

## 验证

适配器和实时等待路径的单元测试位于 `tests/test_dsh.py`：

```bash
python -m pytest tests/test_dsh.py
```

端到端路径是：在 DSH 会话中提交托管任务，以后台任务运行打印出的 `ltc wait`，等待任务结束，
调用 `job_output` 读取回调，然后 ACK。不要用 headless 恢复代替这项验证。
