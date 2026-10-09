# Agent mode, templates and Claude Code configuration

[← Back to README](../README.md)

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


## Custom templates

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

A ready-to-copy example is [examples/templates/review.yaml](../examples/templates/review.yaml).
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

## Claude Code configuration

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
