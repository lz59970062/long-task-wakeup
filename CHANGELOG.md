# Changelog

## 0.7.1a2 — unreleased

- Automatically deliver new live Pi callbacks through native steering, after
  the current tool-call batch and before the next model request; idle Pi starts
  a normal turn. Keep the existing CLI without delivery flags. Preserve older
  follow-up records, same-process ownership, cancellation and ACK rules; require
  a capable extension and use task v5/callback v4/mailbox v2 to prevent silent
  downgrade by older coordinators. Test receipt during a continuing tool loop
  using real Pi with a local provider, without paid model calls.
- Include measured task runtime in compact callbacks, for example
  `Duration: 2m 5.5s`, using recorded execution timestamps rather than callback
  delivery time. Keep the duration in the callback record across retries.
- Synchronize the MIT license and GitHub issue templates from `main`.

## 0.7.1a1 — unreleased

### Claude Code

- Add `ltc wait [--task ID]` for Claude Code only. Run in the background from a Claude Code session,
  it prints that session's unacknowledged callback when it is queued and exits, so Claude Code wakes
  the conversation that is actually open. While a waiter is alive the daemon leaves that session's
  Claude callbacks alone; after a live delivery it waits 30 minutes for the ACK before its normal
  retries. It refuses to run outside Claude Code and never touches other agents' callbacks. All
  Claude-specific logic lives in `claude_code.py`.
- Find Claude Code when `claude` is not on PATH (Claude desktop app, launchd/systemd services):
  standalone install locations, `CLAUDE_CODE_EXECPATH`, and the newest desktop-app bundle. A
  configured desktop-bundle path that disappears after an app update is rediscovered.
- Start headless `claude -p --resume` in the session's original directory, read from the session
  transcript, instead of the task's `--cwd`.
- Install a separate skill per agent: Claude gets a focused `SKILL.md` built around `ltc wait` plus
  the shared full skill as `REFERENCE.md`; the Codex skill does not mention `ltc wait`.
- `doctor`/health checks no longer report Claude as unavailable when only discovery finds it; the
  report gains a `claude_code` block describing the live and headless routes, and a Claude child
  backed only by the desktop bundle is reported as `child_agent_desktop_only`.
- `ltc run`/`ltc agent` from Claude Code print the exact background `ltc wait` command.
- Fix callback ACK commands when `ltc` is not on the worker's PATH: use the interpreter plus private
  entry point instead of a non-executable script path.
- Add `ltc claude [-- claude args]` (research preview): starts interactive Claude Code with LTC as a
  development channel. The channel server lives as long as the window, follows its current session,
  holds the session's live-watcher lock so the daemon never forks it with a headless resume, and
  pushes each callback into the session as a `<channel source="ltc">` event, even when it is idle.
  `ltc run` then skips the `ltc wait` hint and a stray `ltc wait` exits instead of duplicating the
  callback. Terminal CLI only; the desktop app keeps using `ltc wait`.
- On Linux, also find the newest nvm install (`~/.nvm/versions/node/*/bin/claude`) and the Claude
  desktop app's SSH-session CLI (`~/.claude/remote/ccd-cli/<version>`, source `desktop-remote`).
  The SSH CLI shares the host's `~/.claude` sign-in, so it is not treated as desktop-only; a
  configured path to it that disappears after an app update is rediscovered.
- Verified end to end on macOS with the Claude desktop app (launchd task, live wakeup, late waiter
  takeover from headless retries) and on Linux with the desktop app over SSH (`screen` task, live
  wakeup, daemon deferral while the waiter lives, daemon takeover without one, ACK). Windows not
  yet verified.

### Pi Agent

- Publishing a callback to a live Pi no longer blocks the daemon until the ACK. The delivery worker
  returns "published; awaiting ACK" (122) and the daemon keeps launching tasks and delivering other
  sessions' callbacks; the session's retained lease still orders its own callbacks, and `ltc ack`
  finalizes it. Each loop moves a published, unacknowledged callback to `failed` for manual
  recovery (never replay) when its Pi exits or closes the mailbox, the session is reopened by
  another process, or `--resume-timeout` passes; `last_error` says whether Pi had admitted it.
  Previously a live delivery held the coordinator for up to the resume timeout, delaying unrelated
  task starts.
- Give Pi its own installed skill (`${PI_CODING_AGENT_DIR:-~/.pi/agent}/skills/long-task-callback`):
  a focused `SKILL.md` about steered callbacks plus the shared reference as `REFERENCE.md`. The
  shared Codex/Claude skill no longer carries Pi-session instructions. `install-skill --target`
  and `setup --skill-target` accept `pi` and `all`; `setup --with-pi-extension` installs all three
  and `install-pi-extension` installs the Pi skill too. `doctor` checks the Pi skill and its repair
  command targets it.
- Add `docs/pi.md`.
- Add Pi Agent as a child worker with `ltc agent pi`, fresh nonpersistent text
  execution, submission-time environment/configuration and private result capture.
  Support Pi model/thinking overrides, per-Pi template defaults, executable
  selection via `LONG_TASK_WAKEUP_PI_BIN` and
  `doctor --operation agent --agent-worker pi`. Pi permissions/extensions follow
  its own noninteractive configuration; native Windows/macOS Pi launch remains
  unverified in this session.
- Deliver Pi callbacks to the original live process through a private mailbox and
  native extension message API; busy Pi defers until idle. Add
  `ltc install-pi-extension`, optional `setup --with-pi-extension`, and the managed
  `ltc pi` launcher that leases an absolute session file before startup. Only
  verified terminated managed owners permit print-mode recovery. Pin managed
  sessions; restart the launcher to switch/fork/reload resources. Freeze profile
  routes, reject ID/`--last` fallbacks, and retain unknown publication outcomes
  without automatic replay. Keep the existing ACK protocol. Separate ordinary
  online-only Pi from managed offline recovery and preserve personal settings.
  Ship isolated real Pi/local-provider tests; native Windows/macOS Pi is unverified.
- Add Pi-only `--system-prompt-file` and template `pi.system_prompt_file` defaults.
  Pass the snapshot to Pi's `--system-prompt` to replace its built-in base prompt
  while retaining normal project context, skills and appended instructions.
  Snapshot nonempty UTF-8 contents privately at submission; retain the source
  path for review and keep queued tasks independent of later source changes.
  Resolve CLI paths from the submitting shell and template defaults from their
  YAML directory; dry-run reports the source without creating task files.
- Add `ltc template register|list|unregister` for reusable child-task discovery.
  Accept optional YAML `worker`/`description` metadata and CLI overrides; copy
  templates and system prompts into the user template directory, generate short
  `ltc-NAME` routing skills and maintain an installed LTC skill's managed index.
  Pin each route to its installed absolute template path and emit Codex UI
  metadata. Preserve configuration/home overrides and unrelated skills/aliases;
  use standard Codex aliases only for the default Unix profile. Keep
  `setup --keep-skill` text unchanged. Unregister removes managed discovery files
  while retaining templates, prompts and extra user files, and refuses edited routes.

### Callback modes and Desktop

- Separate CLI, Desktop shared Core and manual callback modes; freeze the mode
  with each task and report callback capability separately from local readiness.
- Refuse new Desktop work before admission when its shared connection is
  unavailable; preserve external `done` results and explicit manual callbacks.
- Retain definite App Server ownership/bridge failures without blind retries;
  add callback-only `retry` with existing target/lease/ACK safeguards.
- Add an opt-in Mac Desktop launcher, private Unix peer PID/UID and creation
  identity verification, and Desktop-owned Core cleanup on stdio EOF.
- Generate Desktop launchers during standard `ltc setup` on both Mac and
  Windows, using the current profile and installed Python runtime. Ship launcher
  resources in the wheel, without a checkout, `.venv` or `examples` dependency.
  Mac produces `.command`; Windows produces `.cmd`, `.ps1` and private
  `desktop.json` settings through the common `desktop prepare|launch|status`
  commands. Retain existing selections and support `--desktop-app` and
  `--no-desktop-launcher` on both platforms. CLI setup and launcher generation
  remain available before Desktop is installed.
- Discover Windows Store Desktop through its manifest and select a matching
  Core dynamically. Save an explicit `--desktop-launch-mode package-context`
  choice; keep direct launch as the default without automatic fallback.
  `desktop launch --check-only` retains the suspended native creation probe;
  setup/prepare generate files without launching Desktop or running that probe.
  Store direct-launch error 5 and the experimental package-context limitations
  remain. This installation change has not received native Windows acceptance;
  earlier Windows callback success does not validate the new setup path.
- Extend the shell Git installation helper to run setup with the same Python
  after pip installation, with `LTC_PYTHON` and setup arguments after `--`.
  Add native Windows `scripts/install_from_git.ps1` with `-RepoUrl`, `-Python`,
  optional `-Subdirectory` and `-SetupArguments` for the same pip-then-setup flow.
  Direct pip installs continue to require an explicit setup command. These
  preview changes are on `codex/macos-desktop-callback` and are absent from stable `v0.7.0`.
- Use task record v3 and callback record v2 for new explicit mode intent, with
  backward reads and deliberate rejection by older coordinators.
- Real Mac two-client protocol/cleanup probe passed with an empty profile and
  no model calls. Live Desktop original-session receipt/ACK remains pending.

## 0.7.0 — 2026-09-27

Release the shared task lifecycle and native Linux, macOS and Windows execution
backends as stable. The optional Windows Desktop package-context bridge remains
experimental and retains its documented installation and transport limitations.
PI/DSH adapters and npm/frozen distribution are not included.

### Shared lifecycle and Linux

- Promote the Linux architecture preview: independent systemd task ownership,
  screen compatibility, and non-systemd container deployment.
- Separate platform backends, Agent adapters, private storage, runtime workers and
  callback rendering while preserving legacy screen task records.
- Default new task callbacks to compact envelopes with private detail artifacts;
  retain full-format callbacks and the existing 4/3 reminder policy.
- Add Agent-owned environment/configuration/runtime checks and repair guidance;
  preserve unknown outcomes and inspect durable results before recovery.
- Keep task ownership independent from coordinator hosting, with no automatic
  replay of ambiguous native attempts. Drain and upgrade 0.6 coordinators before
  submitting native task records, or use an isolated queue and Agent profile.

### Windows

- Add an explicit experimental Desktop bridge with native same-user TCP peer
  verification and a private authenticated upstream; two real App Server clients
  can resume the same owned test thread. The explicit package-context route passed Desktop startup, App Tools and
  original-session receipt/ACK on the tested Windows installation.
- Add a Desktop-launched stdio adapter to retain Desktop's injected tool
  environment and Core arguments while sharing that server with LTC.
- Persist Desktop submission intent before sending a turn so parent timeout or
  worker loss cannot cause duplicate delivery; handle WebSocket pong frames.

- Add independent per-attempt Windows Task Scheduler runners and a per-user
  coordinator supervisor, with no automatic business replay or execution limit.
- Add private Windows ACLs, atomic file publication, transferable process-held
  leases, native process/boot identities and runner-owned Job Object containment.
- Support Windows Python virtual environments, recognized npm/batch CLI shims,
  literal PowerShell ACK commands, safe coordinator drain/replacement and removal.
- Cover Windows lifecycle and shared queue contracts with native process tests;
  add Windows installation guidance and Python 3.9/3.12 CI configuration.
- Record the successful native 120-second workload and subsequent original-session
  callback ACK without restarting the workload; default CLI writer contention remains.
- Preserve partial WebSocket frames and fragmented messages across completion-poll
  timeouts on all platforms, including interleaved ping frames.

### macOS

- Add independent one-shot launchd task jobs and a persistent user LaunchAgent
  coordinator, selected automatically on macOS; add `install-launchd --print`.
- Preserve recovery without replay across ambiguous launch outcomes, coordinator
  replacement and reboot; collect exited task registrations after reconciliation.
- Use native macOS host, boot and process identities, identity-bound reload requests
  and UNIX socket peer credentials; support Apple's bundled screen logging.
- Add macOS setup documentation, platform/lifecycle tests and macOS CI coverage.
- Verify a real 120-second original-session Codex callback and durable ACK on macOS;
  add a Windows development handoff with implementation entry points and acceptance criteria.

## 0.7.0a1 — Linux architecture preview

- Add automatic local configuration checks to task commands and `ltc doctor`.
  Failed checks emit an Agent-owned repair/recheck plan with persisted-work state;
  `setup --keep-skill` preserves existing skill customizations during repair.
- Check the observed task/result/callback chain through saved backend identities;
  distinguish missing owners, unknown observations and unresolved handoffs, with
  unsupported-platform short circuits and separate configuration/runtime results.
- Add an independent systemd user-service execution backend, selected per new task;
  retain legacy screen records and an explicit screen fallback.
- Support non-systemd Linux containers with a screen backend and explicit
  standalone/Supervisor hosting; add Docker and AutoDL deployment examples.
- Verify standalone coordinator identity before reloading, including PID namespace
  and process start time; stale PID files cannot authorize signals or prevent startup.
- Separate Agent adapters, Linux/POSIX primitives, atomic storage, runtime worker
  launching and callback rendering for future platform/Agent handoff.
- Default new task-completion callbacks to short envelopes with private full-detail artifacts;
  retain 4/3 reminder cadence, custom handoff instructions and a full-format option.
- Preserve unknown launch outcomes without automatic duplicate execution, and
  validate persisted result identity before completion recovery.
- Document Linux guarantees and future macOS/Windows/PI/DSH extension requirements.
  These future targets are not implemented or advertised as supported.

## 0.6.6 — 2026-09-21

- Add independent standard/user callback reminder intervals, defaulting to 4/3,
  configured with `ltc prompt-policy`. First reminders are always shown.
- Persist per-conversation callback numbers and reminder decisions atomically;
  retries/restarts do not double-count. Keep task data, routing and ACK instructions
  in compact callbacks, and retain full reminders on invalid state/configuration.
- Move always-applicable callback responsibilities into the skill's core rules.

- Use 8-character random hexadecimal IDs for new managed tasks, with atomic
  directory reservation and retry on collision. Existing task IDs remain valid.

## 0.6.5 — 2026-09-14

- Fix Codex child startup on CLI 0.153.4: use explicit automatic-review and
  approval-policy configuration alongside the requested sandbox, instead of the
  mutually exclusive `--approve-for-me` / `--sandbox` combination.
- Promote durable `ltc agent codex|claude` execution from preview to stable.
- Add `--template test`: an independent child authors requirement-based tests and
  a reviewable handoff; the parent executes tests and reports actual outcomes.
- Add user YAML templates in `~/.config/ltc/templates/` and `--template-file`.
  Configure prompts, per-agent model defaults and parent handoff instructions
  without reinstalling. User templates can override built-ins; submission freezes
  the template source and handoff alongside the expanded prompt and configuration.
- Default Codex test authors to `gpt-5.6-luna` with `max` reasoning. Add child-only
  `--model` and Codex `--reasoning-effort` overrides. Claude inherits its own model
  unless overridden; generic agent commands retain existing defaults.
- Persist template version, resolved model/effort, expanded prompt and child argv
  at submission; include profile metadata and execution responsibilities in callbacks.
- Clarify failed/empty/partial authoring handoffs, requirement gaps, and the limits
  of prompt-level role separation in a shared workspace.

Validation uses independent contract tests plus the existing callback/worker suite
and installed-wheel template loading. Generated test quality still requires parent
review; this release does not enforce a tests-only filesystem or execution sandbox.
