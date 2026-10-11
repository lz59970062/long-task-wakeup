# Claude Code

Live callback delivery uses the `0.7.1` preview. Follow [preview installation](../installation.md#install-the-preview), then run `ltc setup` to install the LTC skill for Claude Code.

## Submit a task and receive the result

Ask Claude to submit this in the current conversation:

```bash
ltc run --cwd "$PWD" --task "Run the tests" -- python -m pytest tests/
```

The submission output provides an `ltc wait` command, for example:

```bash
ltc wait --queue-dir ~/.ltc/queue --task <task-id>
```

Ask Claude to execute the complete printed command through its Bash tool with `run_in_background: true`. When the result enters the queue, `ltc wait` prints the callback and exits. Claude Code then handles the background command's completion message.

After reading the logs and artifacts, Claude runs the callback's `ltc ack` command. This workflow works in the terminal CLI and local Code sessions in the Claude desktop app.

## Describe the work in plain language

After installing the skill, you can say:

> Run the full test suite through LTC. When the results arrive, read the failed-test logs, identify the causes, and suggest fixes.

The skill guides Claude through submission, the background waiter, reading results, and acknowledgement.

## Configure the CLI

To start Claude child Agents or resume a closed conversation through the CLI, sign into the CLI on this machine:

```bash
claude auth login
claude auth status
```

Check LTC from the current session's tool environment:

```bash
ltc doctor --agent claude --session "$CLAUDE_CODE_SESSION_ID"
```

For SSH work, install LTC and its skill on the remote host where the task runs. See the [Claude Code implementation notes](https://github.com/lz59970062/long-task-wakeup/blob/main/docs/claude-code.md) for executable discovery, session directories, and the channel integration.
