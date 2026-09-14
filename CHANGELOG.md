# Changelog

## 0.6.5 — 2026-09-14

- Promote durable `ltc agent codex|claude` execution from preview to stable.
- Add `--template test`: an independent child authors requirement-based tests and
  a reviewable handoff; the parent executes tests and reports actual outcomes.
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
