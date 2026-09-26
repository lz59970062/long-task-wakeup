<p align="center">
  <img src="./assets/readme/hero.svg" width="100%" alt="Long Task Callback (ltc): run a task under an independent Linux, macOS or Windows owner and wake the same Codex or Claude Code session when it finishes">
</p>

<p align="center">
  <a href="https://github.com/lz59970062/long-task-wakeup"><img src="https://img.shields.io/badge/python-%E2%89%A53.9-3fb950" alt="Python ≥ 3.9"></a>
  <a href="./pyproject.toml"><img src="https://img.shields.io/badge/license-MIT-58a6ff" alt="MIT license"></a>
  <img src="https://img.shields.io/badge/agents-Codex%20%C2%B7%20Claude%20Code-d29922" alt="Works with Codex and Claude Code">
</p>

**Long Task Callback (ltc)** gives long-running agent work a durable process owner and an explicit
way back to the same conversation.

There are three normal entry points:

- **Run** — `ltc run -- <command>` submits a new task. The daemon requests an independent
  Linux, macOS or Windows task owner, so the task is not owned by the agent turn.
- **Agent** — `ltc agent codex|claude -- <prompt>` starts a fresh child agent with the
  same durable ownership and callback lifecycle.
- **Done** — `ltc done ...` reports completion of a task that is already owned by
  screen, tmux, Slurm, another scheduler, or an existing script.

The daemon is the control and callback-delivery process. It does not become the parent of the
training process. A separate systemd user service on Linux, one-shot launchd job on macOS,
or on-demand Task Scheduler runner on Windows
(or the screen compatibility backend) owns the LTC worker, which owns the task.

[中文说明](#中文说明) · Formerly `codex-long-task-wakeup` (the old command remains an alias)

## 0.7.0 preview

**The 0.7.0 preview includes native execution on Linux, macOS and Windows.** It separates native execution
backends, Agent adapters, private persistence and compact callback rendering.
New Linux tasks prefer an independent systemd user service; screen remains the
compatibility fallback and continues to own existing tasks.
New macOS tasks prefer separate one-shot launchd jobs in the logged-in GUI session.
Windows 10+ uses per-user Task Scheduler owners and Job Objects in a logged-in user
session. See [Windows setup and limits](docs/windows.md) for PowerShell instructions.
Windows original-session delivery to an open Codex Desktop conversation remains
blocked by its active-writer check; that end-to-end acceptance test has not passed.
An opt-in [Windows Desktop bridge](docs/windows-desktop-bridge.md) now provides a
shared-server adapter with native peer verification. Startup against the tested
Windows Store package still fails on the default direct route with native error 5.
The experimental `-PackageContext` route is based on a successful package-context
suspended-creation probe; actual GUI startup, App Tools and original-session
delivery/ACK remain unverified. Its package-context helper uses a Microsoft
diagnostic tool whose token and other app
behavior are not guaranteed to match normal activation; see the linked procedure and limits.

```bash
python3 -m pip install .
ltc run --backend systemd --task "build project" -- make
# Or explicitly select the existing owner:
ltc run --backend screen --task "build project" -- make
```

Use an isolated queue with a 0.7 daemon while evaluating this preview; the installed
0.6 daemon does not understand the new backend. For example, pass the same
`--queue-dir /absolute/path/to/preview-queue` to `ltc daemon`, `ltc run` and `ltc ack`.
The submitting shell must carry the original Agent session ID (or pass `--session`).

Task-completion callbacks now carry a short result envelope and artifact/ACK paths. Full evidence
is saved privately in `details/<callback-id>.md`; custom instructions are marked
for reading before acting. Use `--callback-format full` for full inline callbacks.
The existing system/user reminder cadence still applies. This reduces repeated
prose without truncating saved results or user instructions.

PI/DSH integrations remain future adapters. See
[macOS setup](docs/macos.md), [Windows setup](docs/windows.md) and [architecture and handoff](docs/architecture.md)
for module responsibilities, recovery guarantees, extension points and validation.
Docker and AutoDL use the non-systemd screen/standalone profile; see
[container setup and lifecycle boundaries](docs/containers.md).
See the [preview validation record](docs/validation-0.7.0a1.md) for tested
environments, commands and remaining platform work.
The macOS adapter has a separate [local validation record](docs/validation-macos.md).
Mac development is complete, including a real two-minute original-session callback.
The Windows adapter has its own [validation record](docs/validation-windows.md).
The [Windows development handoff](docs/handoff-macos-to-windows.md) records the earlier implementation plan.

### Configuration recovery owned by the Agent

`run`, `agent` and `done` check environment support, configuration and LTC's runtime
chain automatically: storage, coordinator, recorded task owners, results and
callback handoff. When anything needs attention, stderr contains an `[ltc-status]` JSON block with the issues, repair
and recheck commands, and whether work has already been persisted. Healthy calls
remain quiet. The installed skill tells the calling Agent to configure LTC,
verify the repair and continue the task; routine configuration is not assigned
back to the user. A persisted task is not resubmitted after repair.

For an explicit check, use `ltc doctor --agent codex --session <original-session>`
with the same `--queue-dir` and `--backend` as your task. It prints JSON and exits
0 when local prerequisites pass, 1 when configuration is needed. This check does
not establish Agent authentication or successful original-session delivery.
Repair commands use the current installation and preserve installed skill files
with `setup --keep-skill`; they do not automatically execute themselves.
The report separates configuration gaps from runtime failures. A missing task
owner or unresolved callback requires inspecting its records, so the generic
setup command is omitted. An empty queue needs no screen session, and a completed
task is not a dead-service alert. Native unsupported systems receive no Linux
repair command. Owner queries that fail are reported as unknown, never proof that
work can safely be rerun.
For externally owned work reported with `done`, the repair uses
`setup --callback-only` and does not require a local task execution backend.

## How it works

<p align="center">
  <img src="./assets/readme/workflow.svg" width="100%" alt="The agent submits a task, the daemon requests an independent task service, the task runs independently, completion is queued, and the same agent conversation resumes and acknowledges it">
</p>

```text
systemd user service (Linux) / LaunchAgent (macOS) / login scheduled task (Windows)
  └─ ltc daemon                 control, recovery and callback delivery

Independent per-task service (screen for legacy tasks)
  └─ LTC worker
      └─ training / benchmark / build / child agent
```

1. Codex or Claude Code submits `ltc run` or `ltc agent`. LTC persists the work, environment,
   original agent/session binding, goal binding, execution backend, and log path.
2. The daemon notices the submission and requests a separate task service.
3. The native task service runs independently of the agent turn and coordinator service.
4. On completion, LTC stores the exit result and queues a callback to the original conversation.
5. The resumed agent inspects the result and runs `ltc ack`. If the callback belongs to a
   multi-stage goal, callback ACK and goal ACK remain separate decisions.

No model polling is required. Inspect task logs or the backend owner when needed.

As an execution rule, a command reliably expected to finish within 60 seconds may use one
foreground wait. Use `ltc run` when it may take about a minute or longer, its duration is
uncertain, or another status check might be needed. A few-minute task should use callback delivery
instead of model polling; the worker and daemon wait without spending model turns.

## Install

Linux requires a reachable systemd user manager (v240+) or GNU screen.
macOS uses the built-in launchd manager; the native backend needs no screen.
Windows uses Task Scheduler as the logged-in user, Python 3.9+, and a local
ACL-capable filesystem such as NTFS. Callbacks default to Agent CLI resume;
the experimental Desktop bridge requires explicit setup. [Windows installation](docs/windows.md)
includes the filesystem durability and logout boundaries.
The selected backend is recorded at submission and never silently changed during recovery.

```bash
# Optional compatibility backend:
sudo apt install screen              # Debian/Ubuntu
# sudo dnf install screen            # Fedora/RHEL

python3 -m pip install "git+https://github.com/lz59970062/long-task-wakeup.git"
ltc setup --force --enable --now
```

`setup` installs the bundled skill for Codex and Claude Code. `--service auto`
uses a systemd user service on Linux or a launchd LaunchAgent on macOS when available,
otherwise standalone on those platforms. On Windows it selects `windows-task`; Supervisor is
an explicit `--service supervisor` choice. `--service standalone --now` starts
the coordinator without creating systemd configuration, suitable for AutoDL. It also creates an empty, user-editable callback prompt hook at
`${CODEX_HOME:-~/.codex}/long-task-wakeup/callback-hook.md`. Existing hook content is never
overwritten, including by `setup --force`. The file is read again before each due user-reminder
attempt, so edits apply to the next due callback without restarting the daemon. Non-empty
content is appended to the callback prompt under `[long-task-callback-user-hook]`; a missing,
empty, or temporarily unreadable file does not block callback delivery.

During setup, LTC checks whether the Claude Code CLI is available and runs `claude auth status`
with output suppressed. This is advisory: setup continues when Claude is absent or authentication
cannot be confirmed because command and Codex workflows remain valid. The check never prints
credential values or account details.

Verify:

```bash
ltc --version
# screen --version  # only for the screen backend
# Linux:
systemctl --user status codex-long-task-wakeup.service
# macOS:
launchctl print "gui/$(id -u)/codex-long-task-wakeup"
```

## Run: submit a new long task

```bash
ltc run \
  --cwd "$PWD" \
  --task "train model" \
  -- python train.py --config configs/exp.yaml
```

Submission returns after the task record is durable. It prints:

- the task id;
- the execution backend (and screen session only when using screen);
- the log file, normally
  `~/.codex/long-task-wakeup/tasks/<task-id>/attempt-1.log`.

Inspect the task log when useful. Screen commands apply only to screen-owned tasks:

```bash
# screen -ls
# screen -r ltc-<task-id>
tail -f ~/.codex/long-task-wakeup/tasks/<task-id>/attempt-1.log
```

Detach from screen with `Ctrl-a d`; detaching does not stop the task.

## Agent: submit a fresh child agent (0.6.5)

Agent mode follows the same public design as `run`: LTC options come first, and `--` separates
them from the actual child task.

```bash
ltc agent claude \
  --cwd "$PWD" \
  --task "review parser edge cases" \
  -- "Inspect parser.py and its tests. Report concrete defects and suggested fixes."

ltc agent codex \
  --cwd "$PWD" \
  --task "implement parser fixes" \
  -- "Fix the confirmed parser defects and run the relevant tests."
```

`codex|claude` selects the child process. Callback routing remains independent: LTC auto-detects
the launching conversation, or the existing `--agent` and `--session` options can bind it
explicitly. LTC creates private prompt and result files under its managed task directory and
prints the result path at submission; callers do not supply file paths.

For Claude Code, LTC carries the submission-time configuration, authentication, proxy, and custom
environment into the child while removing `CODEX_THREAD_ID`, `CLAUDE_CODE_SESSION_ID`, and
`CLAUDECODE`. Those values identify a parent conversation or nested Claude process and must not
become the identity of the fresh child. Agent mode does not enable Claude's `--bare` mode.

## Callback reminder intervals (0.6.6)

Repeated standard instructions and the user callback hook have separate intervals:

```bash
ltc prompt-policy --system-every 4 --user-every 3
ltc prompt-policy                       # show effective settings without modifying state
ltc prompt-policy --user-every 1        # change just the user interval; keep the other
```

Both values must be positive integers; `1` restores reminders on every callback.
Settings are saved in `${CODEX_HOME:-~/.codex}/long-task-wakeup/callback-prompts.yaml`:

```yaml
system_every: 4
user_every: 3
```

Each queue counts separately for each agent and bound session. The first callback
shows both reminders. With the defaults, standard reminders appear at #1, #5, #9,
… and user reminders at #1, #4, #7, … . New compact envelopes omit counter prose;
legacy callbacks retain their cadence line. Goal reminders use the same counter.
The number counts distinct callbacks reaching their first
delivery attempt, not successful ACKs; a failed delivery can consume one number.
Retries and daemon restarts reuse the same stored number and decisions. Queuing,
canceling before delivery, and dry-run do not consume numbers. Changing settings
applies to new allocations; retrying an old callback retains its original schedule.

New compact callbacks retain task/result identity, artifact references, bound session
and the exact ACK command. Full command lines, messages and handoff instructions
are saved privately at the Details path; custom instructions and recovery guidance
require reading it before acting. Standard reminders are short and periodic. The
skill's core rules apply even when reminder prose is omitted.

User text in `callback-hook.md` is appended only on due attempts, and is reread on
each such attempt, including retries. Editing it needs no restart. An empty hook
adds no text. Policy changes likewise need no restart once the new delivery worker
is installed. Existing queued callbacks without a compact prompt keep their full
standard text; their user hook can still follow the new cadence. `--last` always
keeps full reminders because its actual conversation identity is not pinned.

The counter and per-callback allocations are atomically saved together under
`<queue>/prompt-counters/`, with a separate file lock. If configuration or counter
state cannot be read/validated/written, LTC warns and sends full reminders instead
of suppressing them or blocking the result. It does not overwrite corrupt history.

## Preset child tasks (0.6.5)

```bash
ltc agent codex --template test --cwd "$PWD" \
  --task "write independent parser tests" \
  -- "Use docs/parser-contract.md to test parser.py, including invalid input and boundaries."

# Override the child model and its reasoning effort:
ltc agent codex --template test --model gpt-5.6-luna --reasoning-effort max \
  --task "write parser tests" -- "Test the documented parser contract."
```

`--template test` expands the bundled independent test-authoring prompt unless a user template
with that name overrides it. The built-in behavior is described below. Supply concrete
requirements or specification paths after `--`; a bare `test` in the prompt is ordinary text.
All LTC flags must precede `--`. Codex defaults to `gpt-5.6-luna` with reasoning effort `max`;
explicit child options override these defaults. The selected model must be available to your
CLI/account and support the selected effort; LTC does not silently substitute another model.
`--model` also works for Claude, which otherwise inherits its CLI model configuration.
`--reasoning-effort` is Codex-only. Without a template or explicit model options, existing
agent commands retain their CLI defaults. `--agent` still selects the **parent callback** agent.

The child derives expected behavior from requirements before examining implementation, writes
tests and test-only fixtures, and returns a requirement-to-test mapping, plausible defects
caught, changed files, exact execution commands, and gaps. It must not execute tests or modify
production code. Missing contracts must be reported, rather than inferred from current outputs.
The parent reviews the handoff, executes the tests, and diagnoses failures without weakening
assertions simply to obtain passing results. Child exit zero does not mean tests passed.

These are prompt-level responsibilities in a shared workspace, not enforced filesystem or
execution isolation. Avoid concurrent edits to the same files. Review the child's actual diff
and report before execution. A failed, empty, blocked, or partial handoff requires diagnosis.

Use `--dry-run` to inspect the expanded prompt and resolved profile without launching a child.
LTC freezes the template name/version, model/effort, command, and expanded prompt at submission;
queued tasks keep that snapshot across upgrades. Inherited CLI defaults are not resolved by
LTC and can change before execution; specify a model/effort to pin them.


### Custom templates

Create `~/.config/ltc/templates/review.yaml` (or `.yml`) with:

```yaml
version: 1
codex:
  model: gpt-5.6-luna
  reasoning_effort: max
prompt: |
  Independently review the requirements and referenced code without editing files.
  Report defects with file locations, triggering inputs, expected behavior,
  and evidence. Distinguish confirmed problems from unresolved questions.
handoff: |
  The parent verifies each finding, implements justified fixes, and runs tests.
```

```bash
ltc agent codex --template review -- "Review parser.py against docs/spec.md"

# Or select a file directly, without copying it into the user directory:
ltc agent codex --template-file ./examples/templates/review.yaml \
  -- "Review parser.py against docs/spec.md"

# Inspect the resolved source, profile, prompt and handoff without starting work:
ltc agent codex --template review --dry-run -- "Review parser.py"
```

A ready-to-copy example is [examples/templates/review.yaml](examples/templates/review.yaml).
The directory is `$LTC_TEMPLATE_DIR` when set, otherwise
`${XDG_CONFIG_HOME:-~/.config}/ltc/templates/`. No reinstall or daemon restart is
needed after creating or editing a template. Template names start with a letter
or digit and contain only letters, digits, `_` and `-`. `--template-file` paths are
relative to the submitting shell's directory, independently of the child's `--cwd`.
Its filename stem is the template name and follows the same naming rule.

`version` (a positive integer revision) and `prompt` (non-empty text) are required.
Optional `codex` accepts `model` and `reasoning_effort`; optional `claude` accepts
`model` only. `handoff` is optional text for the parent callback, not the child's
prompt. For example, add `claude: {model: sonnet}` to configure a Claude child.
Omitted per-agent defaults inherit that CLI's configuration. Explicit `--model`
and `--reasoning-effort` override template defaults. The selected model must support
the chosen effort. Recognized effort values are `minimal`, `low`, `medium`, `high`,
`xhigh`, `max`, and `ultra`; availability depends on the model.

User templates take precedence over same-named built-ins. A user `test.yaml`
**replaces** the built-in prompt, model defaults, and test-specific handoff;
include your desired parent execution instructions in `handoff`. Built-in defaults
are not implicitly merged. Removing the override restores the built-in template.
An invalid override fails rather than silently falling back. Unknown fields,
duplicate YAML keys, empty text, and simultaneous `.yaml`/`.yml` files for a name
are rejected before a task is queued. `--template` and `--template-file` are mutually
exclusive. YAML is safely parsed as configuration; no expressions or placeholders
are evaluated. Requirements after `--` are appended literally to the prompt.

LTC saves the resolved source path, revision, expanded prompt, model/effort, and
handoff at submission. Editing or deleting the file afterward does not change an
already submitted task or its callback instructions. Templates are user-authored
instructions, not an enforcement boundary; inspect results before acting on them.

### Claude Code configuration

Configure Claude Code in the shell that submits `ltc agent claude`, then verify it locally:

```bash
command -v claude
claude auth status
```

No single environment variable is universally required. Claude Code may use OAuth/keychain,
`ANTHROPIC_API_KEY` (and an optional `ANTHROPIC_BASE_URL`), or a supported cloud provider such as
Bedrock, Vertex, or Foundry. LTC also carries `CLAUDE_CONFIG_DIR`, proxy/certificate variables,
and other custom environment values present at submission. Environment captured when the daemon
was installed is not a substitute for the environment present when `ltc agent claude` is called.

## Done: report an externally managed task

Use `done` when LTC should not launch or own the task:

```bash
set +e
python train.py --config configs/exp.yaml
status=$?
ltc done \
  --cwd "$PWD" \
  --task "train model" \
  --command "python train.py --config configs/exp.yaml" \
  --exit-code "$status"
exit "$status"
```

This pattern belongs inside a screen/tmux session, Slurm epilogue, scheduler job, shell trap, or
Python `finally`. `done` only queues the callback; it does not make the preceding process durable.
By default callback failure does not change the task exit code.

## Restart and recovery behavior

### Daemon restart

Native task services are independent of the coordinator service. Recovery reads durable results
and probes the recorded owner; an unavailable manager never authorizes another launch. Legacy
screen tasks keep their named sessions, but stopping their containing service may stop those
processes as well. A completed result can reconstruct a missing callback with the same ID.

### Worker startup handshake

Native launches persist an attempt identity before submission. Ambiguous outcomes are inspected
without automatic resubmission, even if the unit has already disappeared.

Each legacy screen launch must complete a durable handshake by changing the task from `launching` to
`running`. LTC allows a one-second startup window. If screen disappears before that transition,
the daemon records the failed attempt and retries at most three times with exponential backoff.
After the final attempt, the task becomes `launch_failed`, its one-time recovery callback is
queued, and it is never launched again automatically. Worker tokens use an `ltc_` prefix and are
passed as one `--token=value` argument so a token can never be parsed as another command-line
option. A legacy `launching` record without an attempt counter is treated as an unknown prior
launch failure and is never automatically retried during upgrade.

### Same-host reboot

Running processes cannot survive a host reboot. LTC therefore does **not** guess or automatically rerun
the command. After the daemon starts in the new boot, it restores the originally bound Codex or
Claude Code conversation with:

- the task and execution-owner identifiers;
- the local log path;
- the interruption reason;
- instructions to inspect outputs and checkpoints.

That agent then follows the normal task-creation workflow: recreate the run from a valid checkpoint,
supplement the status if artifacts prove it already finished, or record the precise blocking
condition. There is no `--resume-command` interface.

### Cross-host recovery

Cross-host recovery is intentionally unsupported. LTC does not transfer workspaces, datasets,
environments, checkpoints, credentials, or compute allocation to another machine.

## Two acknowledgement layers

Callback delivery ACK confirms that one wakeup was received and inspected:

```bash
ltc ack --queue-dir <queue-dir> --id <callback-id>
```

An unacknowledged callback is retried with backoff. ACK is monotonic.
An ACK marker immediately ends Desktop completion-stream waiting and releases the delivery lease,
even if the App Server completion notification is unavailable.
The resumed agent writes only the queue ACK marker. Global target-lock cleanup is performed by the
daemon, so ACK does not require broader filesystem permissions.

Goal ACK answers a different question: whether the whole multi-stage objective is finished or
cannot proceed:

```yaml
# goal-plan.yaml
version: 1
revision: 1
goal: Finish and publish the report
path:
  - id: draft
    title: Complete the draft
    status: completed
  - id: verify
    title: Verify figures and references
    status: in_progress
  - id: publish
    title: Publish the final report
    status: pending
amendments:
  - revision: 1
    reason: Initial path
```

```bash
ltc goal start --id report-goal --session <session-id> --cwd "$PWD" \
  --task "finish the report" --plan-file goal-plan.yaml
ltc goal check --id report-goal
ltc goal ack --id report-goal --state completed --plan-sha256 <checked-sha256>
ltc goal ack --id report-goal --state blocked_conditions --condition "awaiting dataset access"
ltc goal resume --id report-goal
```

The YAML file is the mutable source of truth. Its ordered `path` uses `pending`, `in_progress`,
`blocked`, and `completed`; completed items must be a continuous prefix. It tells the resumed
agent exactly which item is current and what remains. Users may revise later items, reopen work,
append steps, or change the top-level goal, preferably incrementing `revision` and recording the
reason in `amendments`.

Before reporting completion, the agent must run `goal check` and compare actual work and artifacts
with the latest file. `goal ack --state completed` is rejected unless the supplied digest matches
that check and every path item, including the final one, is `completed`. Any YAML edit invalidates
the previous digest and forces a fresh check, so a remembered obsolete plan cannot finish a goal.
Existing active goals created before 0.6.2 can be migrated with
`ltc goal set-plan --id <goal-id> --plan-file goal-plan.yaml`; attaching or replacing a plan also
clears the previous check.

Use one file per independent goal, preferably `.ltc/goals/<goal-id>.yaml`. A clear, low-risk path
may be drafted by the agent and announced without a blocking approval; strategic choices,
material resource changes, or changed acceptance criteria should be agreed with the user first.
Blocked, resumed, and revised work keeps the same file. A follow-on goal gets a new file. Completed
plans remain at their recorded paths as audit records and are never automatically deleted or
reused. If archival relocation is desired, move and reattach the file with `goal set-plan` before
the final check and completion ACK.

Callback ACK never completes the goal. While an active goal has no newly queued ordinary callback,
the daemon automatically asks the same conversation for its status every three hours by default.
The agent must continue, ACK the goal as completed, or record the exact blocked condition. This
inquiry behavior also applies after reboot recovery.

Bind a submitted task or an external completion callback with `--goal-id <goal-id>`.

## Session binding

Inside an agent conversation, LTC normally binds automatically:

- Codex: `CODEX_THREAD_ID`
- Claude Code: `CLAUDE_CODE_SESSION_ID`

Explicit binding is also supported:

```bash
ltc done --agent claude --session <session-id> \
  --cwd "$PWD" --task "external job" --exit-code 0
```

`--last` is an unsafe fallback and always warns. If no target can be determined, LTC fails rather
than guessing.

## Operations and guarantees

```bash
ltc status
ltc cancel --id <callback-id>
ltc install-shell-hook
```

State lives under `${CODEX_HOME:-~/.codex}/long-task-wakeup/`. The queue uses stable callback ids,
durable ACK markers, and per-session locks. Delivery is at-least-once, not exactly-once: after a
host failure, an already received but not durably ACKed callback may be delivered again. Resumed
agents must inspect existing processes and artifacts before launching follow-up work.

The daemon is normally installed as
`~/.config/systemd/user/codex-long-task-wakeup.service` on Linux or
`~/Library/LaunchAgents/codex-long-task-wakeup.plist` on macOS, or a per-user login
scheduled task on Windows. Windows uses one coordinator queue per Agent profile;
drain and uninstall the coordinator before changing that queue. The daemon may also be run by supervisor
or as a standalone background process in environments without user systemd; the screen backend requires GNU screen. The native Linux backend requires a reachable systemd user manager.

```bash
systemctl --user status codex-long-task-wakeup.service
journalctl --user -u codex-long-task-wakeup.service -f
```

Old scripts may still pass `--via-daemon`. It is accepted as a hidden no-op compatibility flag;
both `run` and `done` already use the daemon by default. There is no direct agent-owned execution
mode and no fallback to one.

## 中文说明

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

当前 0.7.0 预览版支持 Linux、macOS 和 Windows：任务执行、Agent 适配、状态存储和回调格式分别维护。
原生任务服务独立于回调协调器；协调器重启后核对已有结果与进程归属，不重复启动。
旧 screen 任务继续使用原后端，但不承诺停止其所在系统服务后仍然存活。

回调默认只发送任务、状态、产物位置、绑定会话和 ACK 命令。完整命令及交接文字保存在
`details/<id>.md`；自定义指令和恢复说明会提示先读详情。`--callback-format full` 可恢复
完整内联格式。系统和用户提示仍按 4/3 间隔出现，原文不会被自动改写。

Mac 安装方式见 [macOS 指南](docs/macos.md)：`ltc setup --force --enable --now` 自动选择
LaunchAgent 协调器和独立 launchd 任务。任务作业不注册为登录启动项，不会在重启后自动重跑。
Windows 10+ 安装方式见 [Windows 指南](docs/windows.md)：用户登录状态下使用
`ltc setup --service windows-task --force --enable --now`，通过 Agent CLI 回到原会话。
PowerShell ACK 命令支持空格、中文与单引号路径；状态文件在创建时设置当前用户和 SYSTEM 私有 ACL。
Windows 目录创建、重命名和删除不承诺断电持久性；注销或重启可能中断任务，未知结果不会自动重跑。
Windows Desktop transport 尚未验证。每个 Agent profile 使用一个协调器队列，换队列前先排空并卸载。
本机真实原会话回调被 Codex Desktop 的 active-writer 检查阻止，尚未通过端到端验收；
原生任务执行及本地回调生命周期已通过，详见 [Windows 验证记录](docs/validation-windows.md)。
默认直接启动 Store 版 Desktop 仍报错误 5；[实验桥接启动器](docs/windows-desktop-bridge.md)的
`-PackageContext -CheckOnly` 已有挂起创建成功的证据，但实际界面、App Tools 和原会话 ACK 尚未验证。
该选项通过微软诊断工具赋予小型 Python helper 包身份，不修改持久环境或包调试策略，也不使用
`-PreventBreakaway`；它的 token 和其他应用行为不保证等同于正常激活。完整预检还会拒绝运行中的 Desktop。
PI、DSH 适配尚未实现。测试版请使用独立队列和同版本 daemon，避免与已安装版本混用。

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

## License

MIT
