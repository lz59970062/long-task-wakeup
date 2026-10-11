# CLI reference

Inspect the complete options with:

```bash
ltc --help
ltc run --help
ltc agent --help
```

## Install and inspect

| Command | Purpose |
| --- | --- |
| `ltc --version` | Print the current version |
| `ltc setup --enable --now` | Install skills and start the coordinator |
| `ltc setup --keep-skill --force --enable --now` | Update the coordinator and retain skill content |
| `ltc doctor --agent codex --session <id>` | Inspect a session's runtime configuration |

## Submit work

| Command | Purpose |
| --- | --- |
| `ltc run --task <name> -- <command>` | Submit a command |
| `ltc agent codex -- <prompt>` | Start a Codex child Agent |
| `ltc agent claude -- <prompt>` | Start a Claude child Agent |
| `ltc done --task <name> --exit-code <code>` | Report an external task result |

Put submission options before `--`:

| Option | Purpose |
| --- | --- |
| `--cwd <directory>` | Working directory |
| `--task <name>` | Task name shown in the callback |
| `--agent <type>` | Agent type receiving the callback |
| `--session <session>` | Session receiving the callback |
| `--queue-dir <directory>` | Callback queue location |
| `--backend auto` | Select the task execution backend automatically |
| `--goal-id <goal>` | Associate the task with a multi-stage goal |
| `--callback-format full` | Use the full callback text |

## Handle callbacks

| Command | Purpose |
| --- | --- |
| `ltc status` | Inspect outstanding callbacks |
| `ltc status --state failed` | Inspect failed deliveries |
| `ltc ack --id <callback-id>` | Confirm that you have read the result |
| `ltc cancel --id <callback-id>` | Cancel delivery of this callback |
| `ltc prompt-policy` | Inspect reminder intervals |

## Preview commands

These commands use the `0.7.1` preview:

| Command | Purpose |
| --- | --- |
| `ltc wait --task <task-id>` | Wait for and print a callback in Claude Code |
| `ltc retry --id <callback-id>` | Redeliver a saved callback |
| `ltc agent pi -- <prompt>` | Start a Pi child Agent |
| `ltc pi` | Open an LTC-managed Pi session |
| `ltc template list` | List registered templates |
| `ltc desktop status` | Inspect Desktop connection settings |
| `ltc desktop launch` | Start Desktop through the shared Core |

See [Multi-stage goals](../guides/goals.md) for the goal command sequence.
