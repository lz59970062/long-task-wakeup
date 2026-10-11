# Configuration

LTC saves task commands, logs, execution results, and callback records. These files let you inspect earlier runs when troubleshooting or continuing work.

## Default locations

| Location | Contents |
| --- | --- |
| `~/.ltc/tasks/<task-id>/task.json` | Command, working directory, backend, and session binding |
| `~/.ltc/tasks/<task-id>/attempt-1.log` | Output from the first execution attempt |
| `~/.ltc/tasks/<task-id>/result.json` | Exit code and execution result |
| `~/.ltc/queue/` | Callbacks and their delivery states |
| `~/.ltc/callback-hook.md` | User-supplied callback instructions |
| `~/.ltc/callback-prompts.yaml` | Callback reminder intervals |
| `~/.config/ltc/templates/` | Reusable task templates |

The child Agent's answer path and callback details path appear in the submission output or callback. Open the files at those paths.

## Environment variables

| Variable | Purpose |
| --- | --- |
| `LTC_HOME` | LTC state directory; defaults to `~/.ltc` |
| `CODEX_HOME` | Codex configuration and sessions; defaults to `~/.codex` |
| `CLAUDE_CONFIG_DIR` | Claude Code configuration; defaults to `~/.claude` |
| `PI_CODING_AGENT_DIR` | Pi configuration; defaults to `~/.pi/agent` |
| `LTC_TEMPLATE_DIR` | Custom template directory |

`~` means the current user's home directory, or the user profile directory on Windows.

## Use a custom queue

Choose an absolute path and use the same directory for setup, submission, and acknowledgement:

```bash
ltc setup --queue-dir /absolute/path/to/ltc-queue --enable --now
ltc run --queue-dir /absolute/path/to/ltc-queue --task "Tests" -- python -m pytest
ltc status --queue-dir /absolute/path/to/ltc-queue
```

Finish the work in the old queue before changing an existing coordinator's queue. To run multiple LTC installations, give each one a separate Agent profile and queue.

## Edit callback instructions

Write your preferred result-handling instructions in `callback-hook.md`. LTC reads the current file content when a reminder is due. Set the intervals with:

```bash
ltc prompt-policy --system-every 4 --user-every 3
```

The two numbers control the system and user prompt intervals. Set an interval to `1` to attach that prompt to every callback.
