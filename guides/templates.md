# Task templates

Save recurring task instructions in YAML. Each submission can then add the files and goals for that particular run.

## Write a review template

Create `review.yaml` in your project:

```yaml
version: 1
prompt: |
  Read the supplied requirements and code. Check for issues that affect runtime behavior.
  For each issue, give the file location, trigger, expected behavior, and actual behavior.
  Deliver a review report.
handoff: |
  Check each finding, confirm the issue, then arrange the changes and tests.
```

`prompt` gives instructions to the child Agent. `handoff` describes what to do when the result returns to the original conversation.

## Use the template

Preview the expanded prompt first:

```bash
ltc agent codex --template-file ./review.yaml --dry-run -- "Review parser.py and tests/test_parser.py."
```

After checking the output, submit the task:

```bash
ltc agent codex --template-file ./review.yaml --cwd "$PWD" --task "Parser review" -- "Review parser.py and tests/test_parser.py."
```

The text after `--` is appended to the template prompt. LTC saves the expanded instructions at submission so you can inspect what the child received.

## Reuse it across projects

Save the file as `~/.config/ltc/templates/review.yaml` to call it by name:

```bash
ltc agent codex --template review --task "Review current changes" -- "Read the current git diff and check behavior changes."
```

## Register an Agent skill

The `0.7.1` preview can register a template as a discoverable Agent skill:

```bash
ltc template register review --file ./review.yaml --worker codex --description "Review code and report reproducible issues"
ltc template list
```

After registration, Codex can invoke `$ltc-review`, and Claude Code can invoke `/ltc-review`. See the [template registration notes](https://github.com/lz59970062/long-task-wakeup/blob/main/docs/template-registration.md) for fields, file locations, and updates.
