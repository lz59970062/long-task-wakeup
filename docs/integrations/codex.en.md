# Codex CLI

Follow [Installation](../installation.md), then activate LTC's virtual environment in the terminal where you start Codex.

## Submit from a conversation

Ask Codex to run:

```bash
ltc run --cwd "$PWD" --task "Build the project" -- bash scripts/build.sh
```

LTC reads `CODEX_THREAD_ID` from the tool environment and binds the task to the current session. When it finishes, the coordinator resumes that session and delivers the result.

For everyday use, you can tell Codex:

> Run this build through LTC. When it finishes, read the log, check the output package, and tell me the result.

The skill installed by `ltc setup` covers submission, callback handling, and ACK.

## Specify the receiving session

When submitting from another script, pass the session ID explicitly:

```bash
ltc run --agent codex --session "YOUR_SESSION_ID" --cwd "$PWD" --task "Build the project" -- bash scripts/build.sh
```

Use the original session's `CODEX_HOME` so LTC can find the same session records and configuration. The default configuration directory is `~/.codex`.

## Check the configuration

Run this in the Codex tool environment:

```bash
ltc doctor --agent codex --session "$CODEX_THREAD_ID"
```

Follow the suggested fixes for the installation, coordinator, or session configuration. Then run the [ten-second example](../quickstart.md#submit-your-first-task) to check callback delivery.

For the desktop app, see [Codex Desktop](desktop.md).
