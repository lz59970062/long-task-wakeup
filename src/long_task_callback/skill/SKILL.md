---
name: long-task-callback
description: Explicit callback workflow for long-running agent tasks. Use the daemon handoff whenever Codex or Claude Code launches or edits a long-running command, training run, benchmark, test suite, build, deployment, Slurm job, data job, or script and should arrange for that task to resume the same agent session when it finishes. On an LTC configuration status block, the agent repairs configuration and rechecks before continuing. On callback, inspect results, ACK receipt in the bound session, and continue the original goal; periodic reminder text may be omitted but these duties still apply.
---

# Long Task Callback

## Rule

Use an explicit callback when requested or when a command may outlive the current Codex or Claude
Code turn.

There are three public workflows:

- `ltc run -- <command>` submits a new long-running task. The daemon launches it through an independent task owner (systemd on Linux, launchd on macOS, Task Scheduler on Windows; screen fallback on Linux/macOS).
- `ltc agent codex|claude -- <prompt>` submits a fresh child agent through the same durable
  lifecycle. Available since `0.6.5`.
- `ltc done ...` queues a completion callback for work already owned by screen, tmux,
  Slurm, another scheduler, or an existing script.

Daemon handoff is unconditional and requires no opt-in flag. Never add `--via-daemon` to newly
written commands; old scripts may still pass it as a hidden no-op compatibility flag. Do not
invent another public launch flag or a direct-mode flag. Do not use a direct recursive
`codex exec resume` or `claude -p --resume` call from an agent tool sandbox. Do not let callback
behavior change the task's original exit code unless the user explicitly requests `--strict`.

## Callback rules that always apply

On every callback, inspect the result and relevant artifacts, decide whether the
original goal is complete, blocked, or needs another action, and continue when the
next step is clear and safe. Inspect existing work before relaunching it. Then run
the supplied ACK command; ACK confirms receipt, not completion of the overall goal.
Resume only the bound conversation, never redirect to `--last` or another session.

Read the user callback hook at initial LTC use when present. Its instructions still
apply when not repeated. Since LTC 0.6.6, standard reminders appear on the first callback and every
4 distinct callbacks thereafter; user-hook reminders on the first and every 3.
A short callback omits repeated prose, not these responsibilities. Task results,
routing and the ACK command remain present. In 0.7.0a1, full commands, timestamps and handoff text are saved at the supplied Details path. Read that file before acting when the envelope says so; otherwise inspect relevant result artifacts directly. An exit code is process status, not proof of test success.

Configure with `ltc prompt-policy --system-every 4 --user-every 3`; no options shows
the effective policy. `1` means every callback. The file is
`${CODEX_HOME:-~/.codex}/long-task-wakeup/callback-prompts.yaml`. Counters are per
queue, agent and bound session, allocated at first delivery attempt. Retries reuse
the same sequence and reminder decisions across restarts. Queuing/dry-run does not
count. An unsafe `--last` target keeps full reminders because its identity is unknown.

## Ownership invariant

The 0.7 topology separates the coordinator and task owners:

```text
systemd user service / macOS LaunchAgent / Windows login task -> ltc daemon (recovery and callbacks)
independent systemd service / launchd job / Windows scheduled runner -> LTC worker -> command / child agent
```

`--backend auto` prefers an available systemd user manager (v240+) on Linux or
the logged-in GUI launchd domain on macOS, otherwise screen on those platforms.
On Windows it selects `windows-task`, using Task Scheduler in the logged-in user
session and a runner-owned Job Object. `--backend systemd`, `--backend launchd`
or `--backend windows-task` requires that native manager; `--backend screen` explicitly
selects the compatibility backend. Existing screen tasks retain their owner. The
native backend needs no screen. A screen process can remain inside its launching
service's control group: do not assume it survives stopping that service.

After submission, record the task ID, execution backend and artifact paths, then
return control. For long work, avoid polling processes or logs on a timer. Live
monitoring is appropriate when requested or diagnosing callback infrastructure.
Do not change a running task's owner or automatically retry an unknown native
launch. PI/DSH support is not implemented in this preview.

## Duration policy

Choose the execution path before launching the command:

- If the command is reliably expected to finish within 60 seconds, one foreground wait is
  acceptable.
- If it may take about one minute or longer, its duration is uncertain, or a second status check
  might be needed, use `ltc run` from the start.
- Do not keep a prompt cache warm by polling. A few-minute task should normally use callback
  delivery: the daemon and screen wait without model turns, then resume the same conversation
  when work completes; ACK and existing-artifact checks handle possible duplicate delivery.
- If intermediate results genuinely require agent decisions, use sparse milestone callbacks
  rather than timer-based status polling.

If a supposedly short foreground command unexpectedly remains active, keep the current turn
responsible for it; do not abandon it as an agent-owned background process.

The command name is `ltc`; `codex-long-task-wakeup` remains a compatibility alias.

## Install

### Automatic configuration checks

`ltc run`, `ltc agent` and `ltc done` automatically check LTC's supported environment,
configuration and observed runtime chain: storage, coordinator, recorded task
owners, saved results and callback handoff. These checks follow the selected
platform adapter and each task's recorded backend, not a fixed list of software
to install on every host.
Healthy calls add no diagnostic text. A failed check appends a JSON block between
`[ltc-status]` and `[/ltc-status]` on stderr. This is an action request to the
**calling Agent**: diagnose the listed issues, configure LTC in the current
environment, run its `recheck_command`, then continue the user's task. Do not hand
ordinary installation, service startup or configuration commands back to the user.

The block supplies `repair_command` and `recheck_command` as argument arrays pinned
to the current LTC installation and queue. Address missing prerequisites first;
preserve the original Agent session, profile paths and existing custom settings.
Read `environment`, `checks.configuration`, `checks.runtime` and each issue's
`kind`/`code` to distinguish missing setup from a runtime fault or unsupported OS.
`repair_command` can be null: an absent task owner or unresolved delivery needs
artifact/session inspection and lifecycle recovery, not a generic reinstall.
Windows checks Task Scheduler, private state and coordinator identity; macOS checks
launchd/screen readiness. Repair commands follow the selected platform and queue.
The setup repair uses `--keep-skill` to retain an installed skill while installing
one when missing. It can update the LTC coordinator configuration; inspect existing
service customizations before doing so. Never replace a missing binding with an
invented session or `--last`.
For `done`, repair adds `setup --callback-only`: externally owned work needs the
callback coordinator, not a local business-task execution backend.

Check `work.state` before retrying: `task_persisted` or `callback_queued` means
repair the infrastructure around that existing ID, **do not submit it again**.
For `unknown`, inspect that ID's task/queue records before deciding whether a retry
is needed. For `not_submitted`, retry the original LTC invocation only after the
prerequisites pass. A `done` invocation reports work already performed; repair or
retry its callback without rerunning that work.

Use `ltc doctor` for an explicit JSON check (exit 0 for local readiness, 1 for
configuration needed). Pass the same queue, backend and session options; for a
child task include `--operation agent --agent-worker <agent>`. Readiness checks
local state and observed liveness, not task progress, authentication, the
coordinator's environment or end-to-end session delivery. An idle queue needs no
screen session; a durable task result takes precedence over a vanished owner.
An unavailable owner query is unknown, not proof of a dead task. Saved results
without callback handoff after a short grace period and unacknowledged failed
deliveries need attention; acknowledged/canceled history does not. After each
repair, recheck and use the new evidence. If a repair
does not resolve its issue, investigate rather than repeat it unchanged. Ask the
user only for an indispensable login/authorization or unavailable information,
with the specific blocker; do not ask them to perform routine configuration.

Checks do not change command exit semantics, install software or start services
themselves. Help, dry-runs, private workers and ACK/cancel paths remain available
without this automatic check.

```bash
# Install screen only if using the compatibility backend.
python3 -m pip install "git+https://github.com/lz59970062/long-task-wakeup.git"
ltc setup --force --enable --now
```

`setup` installs this skill for Codex and Claude Code and configures the selected coordinator service. It requires a reachable native manager (systemd/launchd/Task Scheduler) or screen for the Linux/macOS compatibility backend. It creates the user-editable fixed prompt hook at
`${CODEX_HOME:-~/.codex}/long-task-wakeup/callback-hook.md` without overwriting existing content.
The delivery worker reads that UTF-8 file on attempts whose saved user-reminder decision is
due, so edits apply on the next due attempt without a restart. Non-empty content is appended
under `[long-task-callback-user-hook]`; hook read failures warn and continue with the callback.

`setup` also checks for the Claude Code CLI and runs `claude auth status` with output suppressed.
This check is advisory and must not block Codex-only installation or print credentials/account
details. Claude configuration must be present in the shell that later submits `ltc agent claude`.

## Windows

Use Windows 10+, Python 3.9+, an ACL-capable local filesystem such as NTFS, and
the ordinary logged-in user's Task Scheduler session. `--backend auto` and
`--service auto` select `windows-task`; screen and WSL are unnecessary. Install
from a Windows-capable checkout in PowerShell without activating the venv:

```powershell
py -3 -m venv .venv
& .\.venv\Scripts\python.exe -m pip install .
& .\.venv\Scripts\ltc.exe setup --service windows-task --force --enable --now
```

Keep the installed runtime at its fixed path. Setup pins the Agent profiles,
executables, PATH and queue in a private configuration file. One Agent profile
has one coordinator queue. For another queue, use a separate profile, or drain
the current tasks and callbacks, run `ltc uninstall-windows-task`, then reinstall
for the new queue. Do not copy another machine's session binding.

Use native argument lists. Recognized npm/forwarding `.cmd` launchers are resolved
without a shell; unsupported wrappers require their underlying executable.
Windows callbacks use CLI resume of the actual original session; Desktop
transport is disabled pending validation. Execute the callback's emitted
PowerShell ACK verbatim, including its leading `&` and quoted paths. ACK confirms
receipt only, not goal completion. Never replace the target with `--last`.

A live Windows test with Codex 0.153.4 could not resume its open Desktop session:
the CLI reported `already has an active writer`. Ending an Agent turn does not
guarantee that Desktop releases this owner. Preserve the failed callback and
report this transport limitation; do not delete Codex's writer lock, forge an
ACK, change the bound session or rerun the business work. Native workload and
fake-Agent delivery tests do not establish live original-session receipt.

New private files/directories grant only the current user and SYSTEM access.
Do not weaken those ACLs to work around an Agent sandbox's restricted token;
authorized native setup/tests may need an ordinary user process outside that
sandbox. Flushed file data and write-through replacement do not provide a
POSIX directory-fsync guarantee: namespace changes may be lost after sudden
power failure. Login is required; sleep pauses work and logout/reboot can
interrupt it. Missing owners and unknown launches never authorize auto-replay.

Use `ltc install-windows-task --print` for the public registration preview,
`ltc doctor --backend windows-task` with the original session and queue for
diagnosis, and `ltc setup --service windows-task --keep-skill --force --enable --now`
after upgrading. A verified coordinator waits for deliveries to drain before
its supervisor replaces it. `ltc uninstall-windows-task` removes the login
registration and drains the coordinator; it does not stop business tasks or
erase results/ACKs. Do not delete lock files while inherited delivery handles
may still own them. See the repository's `docs/windows.md` and validation record.

## macOS

`ltc setup --force --enable --now` selects a user LaunchAgent in the logged-in GUI
session and independent one-shot launchd jobs for new tasks. Screen is optional.
Use `ltc install-launchd --print` to preview the plist, `--backend launchd` to
require the native task owner, or `--backend screen --service standalone` for
headless sessions without a GUI domain. Apple's bundled screen is supported.

Task plists live in private task directories, never in login startup directories.
They have no KeepAlive/restart policy. A coordinator restart does not restart the
task. Sleep pauses work; logout and reboot can interrupt it. Never replay a saved
task automatically. Setup requests identity-verified reload after deliveries drain;
new LaunchAgent environment/arguments take effect on the next service load.
Native process/service access may require an ordinary terminal outside an Agent
tool sandbox. Preserve the original queue/session and inspect any persisted work
before repeating submission. Desktop socket delivery uses local peer credentials;
CLI delivery is the fallback when the optional socket is unavailable.

```bash
launchctl print "gui/$(id -u)/codex-long-task-wakeup"
tail -f "${CODEX_HOME:-$HOME/.codex}/long-task-wakeup/daemon.log"
```

## Containers (Docker / AutoDL)

Use `--backend screen` where no systemd user manager is available. Install screen
in the container. Run LTC, the Agent CLI and workload as the same user inside the
same container with durable local state/workspace mounts. Agent CLI and credentials
must be available inside that container; the host's desktop socket is not assumed.

`ltc setup --backend screen --service standalone --now` starts a coordinator in an
existing AutoDL/container session without systemd. `--service auto` selects systemd
when reachable, otherwise standalone. Use `--service supervisor` only when explicitly
managing that configuration. Standalone `--enable` cannot register container startup.
For Docker images, run `ltc daemon` in foreground under `docker --init`, or use an
external supervisor. A container CMD must not merely spawn a background daemon and exit.

Terminal disconnection or standalone coordinator exit can leave screen tasks alive
while the container remains running. If the coordinator is the container's main
process, its exit ends the container as well. Container stop/recreation interrupts
work; inspect persisted results/checkpoints and session identity before resubmitting.

## Run: submit new work

Use this when the agent is launching the long command:

```bash
ltc run \
  --cwd "$PWD" \
  --task "train model" \
  -- python train.py --config configs/exp.yaml
```

The submission is durable before the command returns. Record the printed values. Use the reported log/result paths for either backend. For screen-owned tasks, the
inspection commands are:

```bash
screen -ls
screen -r ltc-<task-id>
tail -f ~/.codex/long-task-wakeup/tasks/<task-id>/attempt-1.log
```

Detaching with `Ctrl-a d` leaves the task running.

## Agent: submit a durable fresh child agent

Use `ltc agent` when a fresh Codex or Claude Code process should perform an independent task and
the work needs independent ownership, durable artifacts, or a completion callback. For short work that
fits an in-turn native subagent, LTC is unnecessary.

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

The `codex|claude` subcommand selects the child process. The existing `--agent` option still
selects the parent agent to wake when explicit callback binding is needed. The text after `--` is
the child's task, not a shell command. State the objective, relevant paths or context, expected
deliverable, and real constraints; do not impose a fixed task template when they are unnecessary.

LTC snapshots the launching environment and prepares a fresh child environment. For Claude Code,
use `ltc agent claude` instead of manually wrapping `claude -p` with `ltc run`: agent mode keeps
the launching configuration, authentication, proxy, and custom environment while removing parent
session markers that prevent a clean child launch. Do not put credentials in the task prompt, and
do not add `--bare` by default.

Before the first Claude child, confirm `command -v claude` and `claude auth status`. Do not require
`ANTHROPIC_API_KEY` specifically: OAuth/keychain and supported cloud providers are valid too. When
used, `ANTHROPIC_API_KEY`, `ANTHROPIC_BASE_URL`, `CLAUDE_CONFIG_DIR`, proxy/certificate settings,
and other custom values must be available in the submission environment; never print their values.

LTC owns the private prompt and result files in the managed task directory. The caller does not
choose those paths. On callback, inspect the child result and relevant artifacts, acknowledge the
callback, and continue the original goal when the next action is clear.

## Done: report externally managed work

When editing an existing script, shell trap, Python `finally`, or scheduler epilogue, use:

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

`done` only queues delivery. It does not make the preceding task durable. Confirm that an external
process manager already owns that task.

Python pattern:

```python
import subprocess

status = 1
try:
    status = subprocess.call(["python", "train.py", "--config", "configs/exp.yaml"])
finally:
    subprocess.call([
        "ltc", "done",
        "--cwd", "/path/to/project",
        "--task", "train model",
        "--command", "python train.py --config configs/exp.yaml",
        "--exit-code", str(status),
    ])
```

## Session binding

Run from the agent-owned environment and omit target flags by default. LTC detects
`CODEX_THREAD_ID` or `CLAUDE_CODE_SESSION_ID`.

Use `--agent codex|claude --session <id>` when explicit binding is necessary. Use `--last` only as
an unsafe manual fallback; it always warns. If the target cannot be determined, fail instead of
guessing.

## Restart recovery

### Daemon restart

For native tasks, the independent systemd unit, launchd job or Windows scheduled
runner owns the worker. Restarting the
coordinator should not restart that task. If querying the owner fails, its state
is unknown; never treat a manager connection failure as permission to relaunch.
A saved result takes precedence over owner visibility. Reconstruct a missing
callback with the same ID. Old screen records use their original recovery path.

### Worker startup handshake

For the legacy screen backend, a launched worker must durably move its task from `launching` to `running`. Allow a
one-second handshake window. If screen disappears first, record the failed launch and retry no
more than three total attempts with exponential backoff. After the final failure, set
`launch_failed`, remove the launch credentials, queue exactly one recovery callback, and never
automatically relaunch that task. Generate worker tokens with an `ltc_` prefix and pass them only
as a single `--token=value` argument. Treat a legacy `launching` record without an attempt counter
as an unknown prior launch failure and never automatically retry it during upgrade.

Native systemd/launchd/Windows launches with ambiguous outcomes are not retried automatically,
even if no unit is visible. Inspect the task record and outputs.

### Same-host reboot

No backend preserves the running process across a host reboot. Never automatically rerun the command and do not add a
`--resume-command` mechanism. Restore the originally bound Codex or Claude Code conversation with
the task id, screen name, log path, interruption reason, and available checkpoint/artifact context.

The restored agent must:

1. inspect local logs, outputs, processes, manifests, and checkpoints;
2. supplement the completed status if durable artifacts prove completion;
3. otherwise recreate the task through the normal `ltc run` workflow from a valid
   checkpoint;
4. or record the exact blocking condition.

### Cross-host recovery

Do not attempt it. LTC does not transfer code, data, environments, checkpoints, credentials, or
compute resources between hosts.

## Callback acknowledgement

The callback prompt includes:

```bash
ltc ack --queue-dir <queue-dir> --id <callback-id>
```

After inspecting the result and choosing whether to continue, stop, or ask, run that command.
Acknowledgements are monotonic. Missing ACKs retry with backoff. Delivery is at-least-once, so
always inspect existing processes and artifacts before starting follow-up work.
An ACK marker immediately ends completion-stream waiting and releases the delivery lease; it must
not wait for a separate Desktop App Server `turn/completed` notification.
The resumed agent needs write access only to the callback queue. A normal ACK must not create or
modify global target-lock files; if a rare retained lease needs cleanup, the daemon reconciles it
after the durable ACK marker appears.

Use `--callback-format full` at submission only when full inline detail is needed.
The default compact format archives the complete original callback privately and
omits recurring counter explanations. System/user reminder cadence remains 4/3;
user-hook text is preserved on its scheduled callbacks.

Callback ACK confirms one delivery only. It must never implicitly complete a persistent goal.

## Goal acknowledgement and automatic inquiry

Every multi-stage goal must use a mutable UTF-8 YAML goal plan. Treat that file as the single
source of truth for the current overall goal and its ordered path, not the task text remembered
from an earlier turn. Use this schema:

```yaml
version: 1
revision: 1
goal: Release version 0.6.2
path:
  - id: implement
    title: Implement the change
    status: completed
    evidence: tests/test_feature.py
  - id: verify
    title: Run tests and inspect the result
    status: in_progress
  - id: publish
    title: Publish the release
    status: pending
amendments:
  - revision: 1
    reason: Initial path
```

Statuses are `pending`, `in_progress`, `blocked`, and `completed`. Completed items must form a
continuous prefix of `path`; there may be at most one `in_progress` or `blocked` item, and it must
be the first unfinished item.

The plan is intentionally editable. The user or agent may revise later path items, reopen an
earlier item, append or remove items, or change the overall `goal`. Increment `revision` and record
the reason in `amendments` when the intended path changes. After each item, update its status and
evidence. Always follow the latest valid file; do not preserve an obsolete path from memory.

Workflow:

1. Create it once:

   ```bash
   ltc goal start --id <goal-id> --session <session-id> --cwd "$PWD" \
     --task "..." --plan-file path/to/goal-plan.yaml
   ```

2. Bind each `run` or `done` callback with `--goal-id <goal-id>`.
3. Before saying or implying that the whole goal is complete, check the latest plan:

   ```bash
   ltc goal check --id <goal-id>
   ```

   Inspect the actual work and artifacts against every path item. This command reports the
   current item, remaining path, and the current plan SHA-256.
4. Only if every item, including the final item, is confirmed `completed`:

   ```bash
   ltc goal ack --id <goal-id> --state completed --plan-sha256 <checked-sha256>
   ```

5. If progress requires a specific missing condition:

   ```bash
   ltc goal ack --id <goal-id> --state blocked_conditions \
     --condition "specific prerequisite"
   ```

6. When that condition is met:

   ```bash
   ltc goal resume --id <goal-id>
   ```

The completion gate rejects an unchecked plan, a stale digest, an invalid/nonsequential path, or
any unfinished item. Editing the YAML invalidates an earlier check, so the agent must reread the
new overall goal and path. Completion is terminal. A callback ACK does not count as goal
completion.

For an active goal created before YAML plans were required, or to move a plan file, attach the
latest plan and then check it again:

```bash
ltc goal set-plan --id <goal-id> --plan-file path/to/goal-plan.yaml
ltc goal check --id <goal-id>
```

### Plan file lifecycle and user agreement

- Use one plan file for one independent goal. Prefer a stable project-local path such as
  `.ltc/goals/<goal-id>.yaml`. Never recycle a completed goal's file for another goal.
- Before `goal start`, derive the overall goal, acceptance conditions, and ordered path from the
  user's request, then create the YAML. For a clear, conventional, low-risk path, create it and
  tell the user its location and main items without waiting for another approval. If the path
  requires a strategic choice, materially different resource use, changed acceptance criteria,
  or another consequential tradeoff, agree on those points with the user before starting.
- Keep using the same file when the goal is blocked, resumed, or revised. The user may edit it at
  any time; their latest valid revision overrides the agent's remembered path. A follow-on
  objective after terminal completion is a new goal with a new file.
- After completion, preserve the YAML at its recorded path as the audit record. Do not delete it
  automatically. If the user wants it under an archive directory, move it while the goal is still
  active, run `goal set-plan` with the archive path, and then perform the final `goal check` and
  completion ACK.

After three hours without a newly queued ordinary callback, the daemon automatically restores the
same conversation and asks it to continue, ACK completion, or state the exact blocking condition.
The inquiry repeats until the goal is completed or blocked. Reboot recovery callbacks follow the
same goal rules and do not disable this inquiry.

## Multi-round behavior

After each wakeup, inspect relevant logs, metrics, checkpoints, reports, and outputs. Decide:

```text
current_goal:
last_result:
decision: continue | stop_success | stop_blocked | ask_user
reason:
next_command:
budget_remaining:
```

Continue only when the next action is clear, low-risk, aligned with the same goal, and within the
user's budget and project rules. Ask before expanding resource use, changing the scientific or
product objective, deleting artifacts, or making a strategic tradeoff.

When stopping, summarize what was attempted, what changed, and the smallest decision or resource
needed from the user.

## Daemon operations

The systemd user service (Linux), LaunchAgent (macOS), or login scheduled task
(Windows) keeps `ltc daemon` available
for submissions, recovery, and callbacks. On Linux:

```bash
systemctl --user status codex-long-task-wakeup.service
journalctl --user -u codex-long-task-wakeup.service -f
```

The daemon watches `${CODEX_HOME:-~/.codex}/long-task-wakeup/queue` by default. Use `--queue-dir`
or `CODEX_LONG_TASK_WAKEUP_QUEUE_DIR` for an explicit queue.

Before restarting a service or container, drain live callback deliveries where possible.
Unacknowledged interrupted delivery is eligible for replay.

Cancel callbacks through the CLI rather than deleting state:

```bash
ltc cancel --id <callback-id>
ltc cancel --queue-dir <queue-dir> --all --message "no longer needed"
```

`cancel` cancels callbacks only; it does not terminate a running workload.

## Independent test authoring preset

Use `ltc agent codex --template test --task "write independent tests" -- "Requirements and specification paths"`
when a separate agent should author tests for the parent to execute. The default child is
`gpt-5.6-luna` with `max` reasoning; override using `--model` and `--reasoning-effort`.
These flags affect the child only; `--agent` continues to select the parent callback agent.
Claude supports the template and `--model`, inherits its own model by default, and rejects
`--reasoning-effort`. Put all flags before `--`; provide concrete requirements after it.

The child writes requirement-derived tests and a handoff, without running tests or modifying
production code. The parent reviews the changed files, requirements and commands, then runs
the tests and reports actual results. Child completion is not a passing-test result. Diagnose
failed/empty/partial handoffs before execution; never weaken assertions merely to get green.
The shared workspace is not isolated: these are prompt constraints, so inspect actual changes.
Use `--dry-run` to inspect the expanded prompt and resolved model before submission.

## User-defined child templates

Load `--template NAME` from `${LTC_TEMPLATE_DIR}` when set, otherwise
`${XDG_CONFIG_HOME:-~/.config}/ltc/templates/NAME.yaml` (or `.yml`), or use
`--template-file PATH` for an explicit file. These options are mutually exclusive.
A relative file path uses the submitting shell directory, not the child's `--cwd`.
Template names/filename stems start with a letter or digit and otherwise use only
letters, digits, underscores and hyphens. No reinstall is needed when files change.

YAML requires `version: 1` (positive integer revision) and non-empty `prompt: |` text.
Optional `codex: {model: gpt-5.6-luna, reasoning_effort: max}` and
`claude: {model: sonnet}` set worker-specific defaults; CLI options take precedence.
Optional `handoff: |` text instructs the parent on callback. No variable interpolation
or code evaluation occurs. Unknown/duplicate fields or invalid values fail before
submission. Use `--dry-run` to inspect source, prompt, defaults and handoff.

A same-named user template replaces the entire built-in profile, including its
callback guidance; a custom `test` must supply its own test handoff instructions.
Do not silently fall back from a malformed user file to the built-in. Both `.yaml`
and `.yml` existing for one name is an error. Submission freezes source, revision,
prompt, model/effort and handoff, so edits do not alter queued work.
