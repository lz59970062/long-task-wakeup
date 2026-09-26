# Changelog

## Unreleased — macOS support

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
