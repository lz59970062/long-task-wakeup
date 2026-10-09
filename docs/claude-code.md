# Claude Code integration

[← Back to README](../README.md)

LTC works with Claude Code in the CLI and in the Claude desktop app (Code tab). This page explains
how a callback reaches a Claude Code session and what is needed for each route.

## Two delivery routes

| Route | How it works | Needs |
| --- | --- | --- |
| **Live (`ltc wait`)** — recommended | The session runs `ltc wait --task <id>` as a background Bash command. Claude Code wakes the session when a background command exits; `ltc wait` exits as soon as the callback is queued and prints it. | Nothing beyond LTC. Works in the desktop app and the terminal CLI. |
| **Headless fallback** | When no waiter is alive, the daemon runs `claude -p --resume <session>` from the directory where the session started. | A standalone `claude` CLI that is signed in (`claude auth login`). |

Why live delivery matters: a headless resume appends a turn to the session's transcript, but a
session that is already open in the app or terminal does not display it. `ltc wait` instead hands
the callback to the session you are looking at.

## Workflow

```bash
# 1. Submit (returns immediately). stderr prints the exact wait command.
ltc run --cwd "$PWD" --task "train model" -- python train.py

# 2. In Claude Code, run the printed command with run_in_background: true
ltc wait --queue-dir ~/.codex/long-task-wakeup/queue --task <task-id>

# 3. When it exits, the session wakes with the callback. Inspect, then:
ltc ack --queue-dir ~/.codex/long-task-wakeup/queue --id <callback-id>
```

The installed Claude skill (`~/.claude/skills/long-task-callback/SKILL.md`) teaches Claude this
workflow, so in practice you just ask Claude to run something long.

### `ltc wait` reference

| Option | Meaning |
| --- | --- |
| `--task <id>` | Only this managed task's callback. Re-shows it until ACK. |
| `--id <callback-id>` | Only this callback. Re-shows it until ACK. |
| *(no filter)* | The next callback bound to this session that no waiter has shown yet — use with `ltc done`. |
| `--session <id>` | Session to watch (default: `CLAUDE_CODE_SESSION_ID`). |
| `--timeout <s>` | Give up after this many seconds (exit 3). Default: wait forever. |
| `--ack-grace <s>` | After a live delivery, how long the daemon waits for the ACK before the headless fallback (default 1800). |

Exit codes: `0` callback printed (or the requested callback is already acknowledged), `2` invalid
task/id, `3` timeout.

### How it coordinates with the daemon

- A waiter holds a lock file under `<target-locks>/live-watchers/<session-hash>/`. While any waiter
  for a session is alive, the daemon does not start a headless resume for that session.
- The waiter claims a callback with the same atomic `pending → running` move the daemon uses, so a
  callback is never delivered by both at once.
- After printing, the callback goes back to `pending` with `next_attempt_at = now + ack-grace`.
  `ltc ack` moves it straight to `done`. Without an ACK, normal at-least-once retry resumes after
  the grace period.
- If the waiter dies, its lock is released by the OS and the daemon takes over.

## Finding the Claude binary

The desktop app does not put `claude` on PATH, and launchd/systemd services see a minimal PATH.
LTC looks for Claude in this order:

1. `LONG_TASK_WAKEUP_CLAUDE_BIN` / `ltc setup --claude-bin` (an explicit path or name is always honored)
2. `claude` on PATH
3. Standalone installs: `~/.local/bin`, `~/.claude/local`, `/opt/homebrew/bin`, `/usr/local/bin`, `~/.npm-global/bin`, `~/.bun/bin`
4. `CLAUDE_CODE_EXECPATH` from the launching session
5. The newest desktop-app bundle under `~/Library/Application Support/Claude/claude-code/`

The desktop bundle is found last because it authenticates through the app and cannot run headless
on its own. A configured desktop-bundle path that disappears after an app update is rediscovered.

## Resuming from the right directory

Claude Code stores each session under a slug of the directory it started in, and `--resume <id>`
looks the session up from the current directory. A session that later `cd`s elsewhere, or a task
submitted with a different `--cwd`, would otherwise fail to resume. LTC reads the session's first
recorded `cwd` from `~/.claude/projects/*/<session-id>.jsonl` (respecting `CLAUDE_CONFIG_DIR`) and
starts the headless resume there.

## Checking readiness

```bash
ltc doctor --agent claude --session "$CLAUDE_CODE_SESSION_ID"
```

The JSON report contains a `claude_code` block with the binary that was found, whether this is a
desktop session, and whether the headless fallback can work. Only the desktop bundle being present
is not a configuration error — live delivery still works — but delegating to a Claude child with
`ltc agent claude` is reported as `child_agent_desktop_only`, because the child cannot sign in.

## Platform status

Live delivery and binary/session discovery were developed and verified on macOS with the Claude
desktop app. The mechanism is POSIX-generic (flock-based waiter locks), so Linux is expected to
work but has not been verified end to end yet. Windows is untested.
