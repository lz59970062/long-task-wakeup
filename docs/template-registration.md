# Template registration and Agent discovery

`ltc template register` installs a named child-task template and creates a short
`ltc-NAME` routing skill that Codex or Claude can discover across workspaces.
Execution still uses `ltc agent codex|claude|pi`; registration does not add another
execution backend or change the originating callback session.

## Register and invoke

Create a YAML such as `code-audit.yaml`:

```yaml
version: 1
worker: pi
description: Inspect the supplied code and requirements for concrete defects.
prompt: |
  Read the specified requirements and source files. Report defects with locations,
  triggering inputs and supporting evidence. Do not edit files.
handoff: |
  Verify the findings against the requirements before deciding on changes.
```

```bash
ltc template register code-audit --file ./code-audit.yaml
ltc template list
ltc template list --json
```

Registration requires `worker` and `description`, either in the YAML or supplied
with `--worker codex|claude|pi` and `--description TEXT`. CLI values take precedence.
These fields are optional for direct `ltc agent --template`/`--template-file` use.
Registered names use lowercase letters, digits and hyphens, up to 59 characters;
they cannot end in a hyphen or contain consecutive hyphens.

If the effective user template directory already contains the named YAML,
`ltc template register code-audit` can reuse it without `--file`. Registering a
built-in without a user YAML requires an explicit source file. A relative `--file`
path uses the submitting shell directory, independently of a later task's `--cwd`.

The routing skill's description enables implicit selection when relevant. Codex
can explicitly select `$ltc-code-audit`; Claude can use `/ltc-code-audit`. The
generated route passes the installed YAML's absolute `--template-file` path, so a
workspace's `LTC_TEMPLATE_DIR` cannot redirect it to a different same-named template.
Direct invocation remains available under the effective template configuration:

```bash
ltc agent pi --template code-audit --cwd /path/to/workspace \
  -- "Inspect parser.py against docs/spec.md."
```

`--target codex|claude|both` selects the parent skill homes, defaulting to `both`.
It does not select the callback target for a later task. `register` and
`unregister` print short feedback by default and accept `--json` for structured
results. `--dry-run` validates and prints a JSON plan without writing files.

## Stored configuration and namespaces

The effective template directory is `$LTC_TEMPLATE_DIR`, otherwise
`${XDG_CONFIG_HOME:-~/.config}/ltc/templates`. Use absolute override directories
when invoking commands from different working directories. Registration stores:

```text
NAME.yaml
.ltc-assets/NAME/system-prompt-pi.md   # When a Pi system prompt is supplied.
.ltc-registrations.json
```

For Pi, `pi.system_prompt_file` is resolved relative to the source YAML directory,
copied into the managed asset location and rewritten in the installed YAML.
The original source remains unchanged. Missing or invalid UTF-8 system prompt
files fail registration. Optional per-worker model and reasoning settings retain
their existing template meaning; registration does not change CLI defaults.

Parent skill locations honor `CODEX_HOME` and `CLAUDE_CONFIG_DIR`, independently
of the template namespace. Default Unix Codex routes use
`~/.codex/skills/ltc-NAME` with an alias under `~/.agents/skills/`; a custom
`CODEX_HOME` receives no global alias. Default Windows Codex routes use only
`~/.agents/skills/ltc-NAME`. Claude routes use the selected Claude home's `skills`
directory. Within one registry, an existing name cannot be moved to a different
home for the same parent Agent: use the original profile or a separate
`LTC_TEMPLATE_DIR`. A child's own profile configuration remains inherited from
submission; registration does not install its dependencies or authentication.

## Ownership and updates

Registration generates a routing `SKILL.md`, Codex `agents/openai.yaml` metadata
and a delimited index in the installed `long-task-callback` body. If that main
skill is absent, registration supplies the bundled body. Existing text outside
the managed index is preserved. No project or global Agent rule file is edited.

Existing configuration is preserved unless replacement is explicitly requested
with `--force`. Even with `--force`, unrelated skills and conflicting aliases are
preserved. An existing `NAME.yml` must be moved before registering `NAME.yaml`;
registration does not create an ambiguous pair.

Registration data lives outside bundled skill files. Normal skill installation
can restore the managed index from that data; `setup --keep-skill` preserves an
existing skill body without refreshing it. Newly generated routing skills remain
the direct discovery entries. Registered copies do not automatically follow source
edits: apply intended updates explicitly, preserving local changes.

Submitted tasks retain their expanded prompt, system prompt and handoff snapshots
when templates are later changed or removed. External configuration and skill
files read by the child remain live; registration does not freeze their contents.

```bash
ltc template unregister code-audit --dry-run
ltc template unregister code-audit
```

Unregister removes only managed routing files, matching aliases and index entries.
It retains templates, system prompts and extra user files, and refuses changed
routing files or aliases instead of discarding edits. Retained templates can
still be called directly. Unregister does not cancel or rerun submitted tasks.
