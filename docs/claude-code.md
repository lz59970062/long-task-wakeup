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

`ltc wait` exists only for Claude Code. It refuses to run without `CLAUDE_CODE_SESSION_ID` (or an
explicit `--session`) and only ever touches callbacks bound to Claude Code; Codex and other agents
keep receiving callbacks from the daemon and are never told about this command.

| Option | Meaning |
| --- | --- |
| `--task <id>` | Only this managed task's callback. |
| *(no `--task`)* | Any unacknowledged callback bound to this session — use with `ltc done`. |
| `--session <id>` | Session to watch (default: `CLAUDE_CODE_SESSION_ID`). |
| `--timeout <s>` | Give up after this many seconds (exit 3). Default: wait forever. |

One rule: a callback keeps coming back until it is ACKed. Exit codes: `0` callback printed (or the
task's callback is already acknowledged), `2` not a Claude session / unknown task, `3` timeout.

### How it coordinates with the daemon

- A waiter holds a lock file under `<target-locks>/live-watchers/<session-hash>/`. While any waiter
  for a session is alive, the daemon does not start a headless resume for that session's Claude
  callbacks. Closing Claude Code kills the waiter, the OS drops the lock, and the daemon takes over.
- The waiter claims a callback with the same atomic `pending → running` move the daemon uses, so a
  callback is never delivered by both at once.
- After printing, the callback returns to `pending` and the daemon waits 30 minutes for the ACK
  before resuming its normal retries. `ltc ack` moves it straight to `done`.
- All of this lives in `src/long_task_callback/claude_code.py`; the shared CLI only calls into it.

## Per-agent skills

Each agent gets its own installed skill, so Claude-specific usage never reaches other agents:

| Agent | Installed to | Contents |
| --- | --- | --- |
| Codex | `${CODEX_HOME:-~/.codex}/skills/long-task-callback/` | `SKILL.md` (full skill, no `ltc wait`) + `agents/openai.yaml` |
| Claude Code | `${CLAUDE_CONFIG_DIR:-~/.claude}/skills/long-task-callback/` | `SKILL.md` (short, built around `ltc wait`) + `REFERENCE.md` (the shared full skill) |

## Finding the Claude binary

The desktop app does not put `claude` on PATH, nvm installs live under a per-Node-version
directory, and launchd/systemd services see a minimal PATH. LTC looks for Claude in this order:

1. `LONG_TASK_WAKEUP_CLAUDE_BIN` / `ltc setup --claude-bin` (an explicit path or name is always honored)
2. `claude` on PATH
3. Standalone installs: `~/.local/bin`, `~/.claude/local`, `/opt/homebrew/bin`, `/usr/local/bin`, `~/.npm-global/bin`, `~/.bun/bin`, the newest `~/.nvm/versions/node/*/bin`
4. `CLAUDE_CODE_EXECPATH` from the launching session
5. On a Linux host used by the desktop app over SSH: the newest CLI under `~/.claude/remote/ccd-cli/`
6. On macOS: the newest desktop-app bundle under `~/Library/Application Support/Claude/claude-code/`

The macOS bundle is found last because it authenticates through the app and cannot run headless on
its own. The Linux SSH CLI is an ordinary binary that shares the host's `~/.claude` sign-in, so it
can run the headless fallback once `claude auth login` has been done on that host. Both are
versioned: a configured path to either that disappears after an app update is rediscovered.

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

| Platform | Status |
| --- | --- |
| macOS | Verified with the Claude desktop app (local sessions) and the CLI. |
| Linux | Verified end to end with the desktop app's SSH sessions (`CLAUDE_CODE_ENTRYPOINT=claude-desktop`, `screen` backend): `ltc run` → background `ltc wait` wakes the live session → daemon defers while the waiter lives and takes over once it exits → `ltc ack`. The terminal CLI uses the same mechanism. |
| Windows | Untested. |

### Linux notes

- Without a systemd user bus (SSH-only hosts, containers) tasks run under GNU `screen`; install it
  with your package manager. Live delivery does not depend on the task backend.
- Run `ltc setup` once on the Linux host so the Claude skill lands in that host's
  `~/.claude/skills/` — the desktop app's SSH sessions read skills from the remote host.
- The `ltc wait` command printed by `ltc run` uses the `ltc` found on PATH; make sure it is the
  same install as the daemon (`ltc --version`), or older installs will reject `wait`.
- If `claude` on PATH is a wrapper that cannot run outside your login shell, pin a working binary
  with `ltc setup --claude-bin <path>` so the headless fallback can resume.
