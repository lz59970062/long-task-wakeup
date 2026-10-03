<p align="center">
  <img src="./assets/readme/hero.svg" width="100%" alt="Long Task Callback (ltc): run a task under an independent Linux, macOS or Windows owner and wake the same Codex or Claude Code session when it finishes">
</p>

<p align="center">
  <a href="https://github.com/lz59970062/long-task-wakeup"><img src="https://img.shields.io/badge/python-%E2%89%A53.9-3fb950" alt="Python ≥ 3.9"></a>
  <a href="./pyproject.toml"><img src="https://img.shields.io/badge/license-MIT-58a6ff" alt="MIT license"></a>
  <img src="https://img.shields.io/badge/child_agents-Codex%20%C2%B7%20Claude%20Code%20%C2%B7%20Pi-d29922" alt="Codex, Claude Code and Pi child agents">
</p>

**Long Task Callback (ltc)** gives long-running agent work a durable process owner and an explicit
way back to the same conversation.

There are three normal entry points:

- **Run** — `ltc run -- <command>` submits a new task. The daemon requests an independent
  Linux, macOS or Windows task owner, so the task is not owned by the agent turn.
- **Agent** — `ltc agent codex|claude|pi -- <prompt>` starts a fresh child agent with the
  same durable ownership and callback lifecycle.
- **Done** — `ltc done ...` reports completion of a task that is already owned by
  screen, tmux, Slurm, another scheduler, or an existing script.

The daemon is the control and callback-delivery process. It does not become the parent of the
training process. A separate systemd user service on Linux, one-shot launchd job on macOS,
or on-demand Task Scheduler runner on Windows
(or the screen compatibility backend) owns the LTC worker, which owns the task.

[中文说明](#中文说明) · Formerly `codex-long-task-wakeup` (the old command remains an alias)

## 0.7.1 preview: explicit callback modes

This checkout distinguishes **CLI**, **Desktop shared Core**, and **manual receipt**.
`--callback-mode auto` detects the current Codex Desktop origin. New Desktop tasks
are refused before execution when the shared connection is unavailable; users
can explicitly choose `--callback-mode manual` to save results for later handling.
See [modes, readiness and retry](docs/callback-modes.md).

On Mac and Windows, the preview's standard `ltc setup` automatically generates
a launcher for the current user and Python installation. Using it remains
opt-in: quit Desktop at an idle time, then open `Start LTC Desktop.command`
(Mac), `Start LTC Desktop.cmd` (Windows), or use `ltc desktop launch`.
Setup never starts or interrupts Desktop. Mac preparation discovers an app
bundle; Windows launch/preflight discovers the Store package. Use `--desktop-app` to select an app explicitly, or
`--no-desktop-launcher` to skip generation on either platform. The common
`ltc desktop prepare|launch|status` interface uses packaged runtime resources;
it does not require a checkout or its `examples` directory. See
[Mac Desktop setup](docs/macos-desktop-bridge.md) and
[Windows Desktop setup](docs/windows-desktop-bridge.md) for platform limits and
remaining live acceptance checks. Upgrade the coordinator before using this
preview's new task/callback record versions.

**Use branch `codex/macos-desktop-callback` for this preview.** The stable
`v0.7.0` installation below does not include automatic Desktop launcher generation.
This branch remains a preview with the live acceptance checks described above.

**中文：** CLI 与桌面共享 Core 是两条分别验收的完整路线。普通桌面未配置共享
连接时，自动回调不可用；可以明确选择手动接收结果。`doctor` 分别报告运行条件
和回调能力，`ltc retry --id …` 只补投已有结果，不重跑业务任务。
本预览的 Mac 和 Windows 安装流程都会自动生成当前用户的启动器，生成文件中的绝对路径按本次
安装计算；请在每台机器运行安装流程，不要复制另一台机器的启动脚本。普通
`pip install` 后仍需执行标准的 `ltc setup`。本预览分支为 `codex/macos-desktop-callback`，正式版 `v0.7.0`
不包含这项自动生成功能。

## 0.7.0

**LTC 0.7.0 is the stable release with native execution on Linux, macOS and Windows.** It separates native execution
backends, Agent adapters, private persistence and compact callback rendering.
New Linux tasks prefer an independent systemd user service; screen remains the
compatibility fallback and continues to own existing tasks.
New macOS tasks prefer separate one-shot launchd jobs in the logged-in GUI session.
Windows 10+ uses per-user Task Scheduler owners and Job Objects in a logged-in user
session. See [Windows setup and limits](docs/windows.md) for PowerShell instructions.
Default CLI delivery to an open Windows Codex Desktop conversation encounters
its active-writer check. The opt-in [Windows Desktop bridge](docs/windows-desktop-bridge.md)
shares the existing thread owner with native peer verification. On 2026-09-27,
the experimental `-PackageContext` route passed GUI startup, an App Tools call,
and original-session callback receipt/ACK for a completed 120-second task.
Default direct startup of the tested Store package still fails with native error 5. Its package-context helper uses a Microsoft
diagnostic tool whose token and other app
behavior are not guaranteed to match normal activation; see the linked procedure and limits.

```bash
python3 -m pip install "git+https://github.com/lz59970062/long-task-wakeup.git@v0.7.0"
ltc run --backend systemd --task "build project" -- make
# Or explicitly select the existing owner:
ltc run --backend screen --task "build project" -- make
```

When upgrading from 0.6, drain existing work and upgrade the coordinator before
submitting native tasks, or use a separate 0.7 queue and Agent profile. The 0.6
daemon does not understand native task records. Pass the same
`--queue-dir /absolute/path/to/ltc-queue` to `ltc daemon`, `ltc run` and `ltc ack`.
Standalone coordinators also require separate `CODEX_HOME` profiles for isolation.
The submitting shell must carry the original Agent session ID (or pass `--session`).

Task-completion callbacks now carry a short result envelope and artifact/ACK paths. Full evidence
is saved privately in `details/<callback-id>.md`; custom instructions are marked
for reading before acting. Use `--callback-format full` for full inline callbacks.
The existing system/user reminder cadence still applies. This reduces repeated
prose without truncating saved results or user instructions.

Stable 0.7.0 does not include Pi or DSH adapters; this checkout adds Pi as a child
worker with callbacks to Codex or Claude Code. DSH remains a future adapter. See
[macOS setup](docs/macos.md), [Windows setup](docs/windows.md) and [architecture and handoff](docs/architecture.md)
for module responsibilities, recovery guarantees, extension points and validation.
Docker and AutoDL use screen when user systemd is unavailable; see
[container setup, autostart and lifecycle boundaries](docs/containers.md).
For unattended use, configure a verified AutoDL boot hook, an existing Supervisor,
or Docker's `unless-stopped` policy. Standalone setup alone does not register startup.
See the [0.7.0 release validation record](docs/validation-0.7.0.md) for the final
checks and evidence limits. Historical platform evidence is recorded separately:
[Linux preview](docs/validation-0.7.0a1.md), [macOS](docs/validation-macos.md),
and [Windows](docs/validation-windows.md). macOS validation includes a real
two-minute original-session callback. The Windows Desktop package-context bridge
remains experimental; the stable core release does not guarantee every Desktop transport.
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

python3 -m pip install "git+https://github.com/lz59970062/long-task-wakeup.git@v0.7.0"
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

For the **unreleased 0.7.1 preview**, setup also creates platform launchers under
`<CODEX_HOME>/long-task-wakeup`: `Start LTC Desktop.command` on Mac, and
`Start LTC Desktop.cmd` plus `Start LTC Desktop.ps1` on Windows. The private
`desktop.json` records the current profile and installed Python/runtime paths.
All required launcher resources are included in the wheel. An absent Desktop app
does not prevent CLI setup or launcher generation; after installing Desktop,
use `ltc desktop prepare` on Mac if no configuration exists, or
`ltc desktop launch --check-only` on Windows. Generated absolute paths are local
installation settings, not usernames or checkout paths embedded in the
distributed source. Rerun setup after moving or replacing the Python environment.
Windows defaults to direct launch; `setup --desktop-launch-mode package-context`
explicitly selects and saves its experimental Store package route. This Desktop
setup step only generates files; `ltc desktop launch --check-only` runs the Windows suspended
process-creation probe without running GUI code. It never silently changes routes.

The preview's `scripts/install_from_git.sh` runs both pip installation and
`setup --force --enable --now` using the same interpreter. `LTC_PYTHON` selects
that interpreter, and arguments after `--` are passed to setup. This helper must
install branch `codex/macos-desktop-callback` (or a commit from that branch);
running it against `v0.7.0` cannot add the new launcher feature. Direct pip
installation deliberately has no setup hook; follow it with `ltc setup` as above.

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

## Agent: submit a fresh child agent

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

ltc agent pi \
  --cwd "$PWD" \
  --task "review parser edge cases" \
  -- "Inspect parser.py and its tests. Report concrete defects and suggested fixes."
```

`codex|claude|pi` selects the child process. Callback routing remains independent: LTC auto-detects
the launching conversation, or the existing `--agent` and `--session` options can bind it
explicitly. LTC creates private prompt and result files under its managed task directory and
prints the result path at submission; callers do not supply file paths.
Pi is a child worker only: the originating parent and `--agent` callback target remain
`codex|claude`.

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
`--model` also works for Claude and Pi, which otherwise inherit their CLI model configuration.
Pi maps `--reasoning-effort` to `--thinking`; it accepts `off`, `minimal`, `low`, `medium`,
`high`, `xhigh`, and `max`. Claude rejects `--reasoning-effort`. The built-in test template's
Codex model and effort are never imposed on a Pi child. Without a template or explicit model options, existing
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
Optional `worker` and `description` describe a registered child route. Existing
templates without them continue to work with `--template` and `--template-file`.
Optional `codex` and `pi` accept `model` and `reasoning_effort`; optional `claude` accepts
`model` only. `handoff` is optional text for the parent callback, not the child's
prompt. For example, add `claude: {model: sonnet}` to configure a Claude child.
For Pi, add `pi: {model: provider/id, reasoning_effort: high}` using an available model.
Pi also accepts `system_prompt_file: prompts/system.md`, resolved relative to the
template YAML directory; explicit `--system-prompt-file` takes precedence.
Omitted per-agent defaults inherit that CLI's configuration. Explicit `--model`
and `--reasoning-effort` override template defaults. The selected model must support
the chosen effort. Codex effort values are `minimal`, `low`, `medium`, `high`,
`xhigh`, `max`, and `ultra`; Pi accepts `off` through `max` as listed above and rejects
`ultra`. Availability depends on the model.

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

### Register a template for Agent discovery

```bash
ltc template register review --file ./review.yaml --worker codex \
  --description "Review the supplied requirements and code for concrete defects."
ltc template list --json
```

For an existing user template, use `ltc template register review` without `--file`;
`register` and `unregister` accept `--json` for structured results.

Registration copies the YAML and referenced system prompt into the effective user
template directory and generates a short `ltc-review` routing skill with Codex UI
metadata. The route uses an absolute installed `--template-file` path. Its description
allows Codex/Claude to discover the child task from other workspaces; in Codex,
explicitly request `$ltc-review`. A managed index in an installed LTC skill records
the available routes. Registration requires a worker and description;
`--worker` and `--description` override optional YAML metadata.
`--target codex|claude|both` chooses the parent homes receiving routing skills.
Use `--dry-run` to inspect changes. Existing user configuration is preserved unless
replacement is explicitly requested with `--force`.
Unrelated skills and conflicting aliases are preserved even with `--force`.
Registered names use lowercase letters, digits and hyphens.

Registration honors `LTC_TEMPLATE_DIR`, then `XDG_CONFIG_HOME`; parent skill homes
honor `CODEX_HOME` and `CLAUDE_CONFIG_DIR`. Copies live at `NAME.yaml` and, for Pi,
`.ltc-assets/NAME/system-prompt-pi.md`, with registration metadata in
`.ltc-registrations.json`. A missing main LTC skill is installed automatically;
`setup --keep-skill` leaves existing LTC text untouched. `ltc template unregister review`
removes managed routing files and the index entry, preserving templates, prompts
and extra user files; edited routing files or aliases block removal. Unregister
also supports `--dry-run`; it does not cancel queued work. See
[profile paths and registration limits](docs/template-registration.md) for Codex
aliases and separate parent-home registrations.

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

### Pi Agent configuration

Configure Pi in the submitting shell, then check child readiness with:

```bash
command -v pi
ltc doctor --operation agent --agent-worker pi
ltc agent pi --model provider/model --reasoning-effort high --dry-run \
  -- "Review parser.py against docs/spec.md"
```

Use `LONG_TASK_WAKEUP_PI_BIN` to select a specific Pi executable. LTC launches
`pi --print --mode text --no-session`, supplies the prompt through stdin, and saves
stdout to the managed private agent-result artifact. No Pi session is persisted.
The child inherits submission-time Pi profile, authentication, API, proxy and extension
configuration; LTC removes Codex/Claude parent markers plus `PI_SESSION_ID` and
`PI_SESSION_FILE`. Do not put credentials in the prompt. Tool permissions and project
extension trust follow Pi's own noninteractive configuration; LTC does not add
`--approve`. `--sandbox-mode` is for Codex and `--permission-mode` is for Claude.
The command contract was checked with Pi 0.87.1; native Windows/macOS Pi launch
has not been verified in this session.

### Pi system prompt files

Use a separate system prompt file with a Pi child:

```bash
ltc agent pi --system-prompt-file ./prompts/pi-system.md --cwd "$PWD" \
  -- "Review parser.py against docs/spec.md"
```

`--system-prompt-file` is Pi-only and separate from `--template`/`--template-file`,
which build the task prompt sent through stdin. LTC passes the snapshot to Pi's
`--system-prompt`: it replaces the built-in base system prompt while retaining
Pi's normal project context, skills and appended instructions. A relative CLI path is resolved
against the submitting shell's directory, independently of the child's `--cwd`.
LTC reads a nonempty UTF-8 file at submission and copies it to the private
`agent-system-prompt.md` task artifact. Later source changes or deletion do not
affect the queued child. No interpolation or script evaluation occurs.
Use `--dry-run` to inspect the resolved source without creating task files or
starting a child process. A custom task YAML can set the default:

```yaml
pi:
  system_prompt_file: prompts/system.md
```

That path is relative to the YAML file's directory; a CLI override uses the
submitting shell directory. Missing, empty or invalid UTF-8 files fail submission.
An editable sample is [examples/prompts/pi-system.md](examples/prompts/pi-system.md).

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
- **Agent**：`ltc agent codex|claude|pi -- <任务>`。用同一套持久化和 callback
  生命周期启动一个全新的 Codex、Claude Code 或 Pi 子代理；回调目标仍是原 Codex 或 Claude Code 会话。
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
正式版 0.7.0 不含 Pi、DSH 适配；当前源码已添加 Pi 子代理，DSH 尚未实现。从 0.6 升级时，先排空已有任务并升级协调器，再提交原生任务；
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

Agent 模式沿用 `run` 的命令语言：

```bash
ltc agent claude --cwd "$PWD" --task "review parser" \
  -- "Inspect parser.py and report concrete defects."

ltc agent codex --cwd "$PWD" --task "fix parser" \
  -- "Fix the confirmed defects and run the relevant tests."

ltc agent pi --cwd "$PWD" --task "review parser" \
  -- "Inspect parser.py and report concrete defects."
```

`codex|claude|pi` 选择被启动的子代理；callback 目标仍由启动现场自动识别，必要时继续使用已有的
`--agent` 和 `--session` 显式绑定。提示词和最终回答文件由 LTC 自动放入私有任务目录，用户
无需传入路径。Claude 子代理继承提交时的认证、配置、代理和自定义环境，但不会继承
`CODEX_THREAD_ID`、`CLAUDE_CODE_SESSION_ID` 和 `CLAUDECODE` 这些父会话标记；默认也不会
启用 `--bare`。

Pi 仅作为子代理，`--agent` 仍只接受 `codex|claude`。LTC 使用
`pi --print --mode text --no-session`，从 stdin 传入任务并保存文本结果，不持久化 Pi 会话。
Pi 继承提交时的 profile、认证、API、代理与扩展配置，但移除 Codex/Claude 父会话标记及
`PI_SESSION_ID`、`PI_SESSION_FILE`。可用 `LONG_TASK_WAKEUP_PI_BIN` 指定可执行文件，
用 `ltc doctor --operation agent --agent-worker pi` 检查本地条件。
`--model provider/model` 直接传给 Pi，`--reasoning-effort` 映射为 `--thinking`，支持
`off|minimal|low|medium|high|xhigh|max`，不支持 `ultra`。内置 test 模板的 Codex 默认模型和推理级别不套用于 Pi。
Pi 工具权限与项目扩展信任遵循其自身非交互配置；LTC 不添加 `--approve`。
`--sandbox-mode` 只用于 Codex，`--permission-mode` 只用于 Claude。本次未验证 Windows/macOS 原生 Pi 启动。

Pi 支持 `ltc agent pi --system-prompt-file ./prompts/pi-system.md -- <任务>`。
它与生成任务提示词的 `--template`/`--template-file` 分开；文件快照传给 Pi 的
`--system-prompt`，替换内置基础系统提示词，保留 Pi 正常加载的项目上下文、skills 与追加指令。
CLI 相对路径按提交 shell 目录解析，
不受子代理 `--cwd` 影响。提交时读取非空 UTF-8 文件并复制为私有的
`agent-system-prompt.md`，之后源文件修改或删除不影响已提交任务，不做插值或脚本求值。
自定义 YAML 可用 `pi: {system_prompt_file: prompts/system.md}` 设置默认值，该路径相对 YAML 所在目录；
显式 CLI 参数覆盖它。`--dry-run` 显示解析后的源路径，不创建任务文件或启动子代理。

通用模板可用 `ltc template register NAME --file ./task.yaml` 注册；YAML 的 `worker` 与
`description` 或对应 CLI 参数指定子代理和用途。已有用户模板可直接 `register NAME`。
生成的 `ltc-NAME` 技能可被父 Agent 发现，路由固定使用已安装模板的绝对路径。
`ltc template list --json` 查看注册信息；`unregister NAME` 移除受管路由与索引，保留模板、
提示词和额外用户文件。注册与注销支持 `--dry-run` 和 `--json`，保留既有目录与 profile 选择。
详见[模板注册与配置边界](docs/template-registration.md)。

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
