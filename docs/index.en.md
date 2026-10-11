# Long Task Callback

Long Task Callback (LTC) brings an Agent back to the same conversation when a long task finishes. Submit a training run, test suite, or code review, and let it run in the background. LTC sends the result paths back to the conversation so the Agent can read the logs and artifacts and continue working.

[Get started](quickstart.md){ .md-button .md-button--primary }
[Explore the architecture](overview.md){ .md-button }

## Start with a task

Ask the Agent to submit a training command from its current session:

```bash
ltc run --cwd "$PWD" --task "Train the model" -- python train.py --config configs/exp.yaml
```

LTC returns a task ID and a log path. When the task finishes, the original session receives a callback. The Agent checks the output and runs the ACK command included in the callback. Start with the ten-second example in [Your first task](quickstart.md).

## Choose an entry point

| What you want to do | Where to start |
| --- | --- |
| Run training, tests, builds, or data processing | [Running commands](guides/run.md) |
| Give another Agent a self-contained piece of work | [Running child agents](guides/agents.md) |
| Receive results from jobs already running in Slurm or tmux | [External jobs](guides/external-jobs.md) |
| Reuse review or analysis instructions | [Task templates](guides/templates.md) |
| Track training, evaluation, and reporting as one goal | [Multi-stage goals](guides/goals.md) |

For worked examples, see [Use cases](guides/use-cases.md).

## Set up your environment

LTC runs on Linux, macOS, and Windows. Follow [Installation](installation.md), then choose your Agent: [Codex CLI](integrations/codex.md), [Claude Code](integrations/claude.md), [Pi](integrations/pi.md), or [Codex Desktop](integrations/desktop.md). The [support matrix](integrations/support.md) lists versions, callback routes, and validation records.

The quickstart uses stable version `0.7.0`. Claude live callbacks, Pi, and the Desktop launcher use the `0.7.1` preview, with setup steps on each integration page.

## Keep reading

[Architecture](overview.md) follows a task from submission to callback. Use the [CLI reference](reference/cli.md) for commands and [Troubleshooting](troubleshooting.md) for common problems. Implementation notes and validation records are collected in [Engineering notes](reference/engineering.md).
