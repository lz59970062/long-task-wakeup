# Pi Agent integration

[← Back to README](../README.md)

LTC works with the Pi coding agent (`pi`) in both directions: Pi can
run as a **child agent** (`ltc agent pi`), and a Pi session can be the **callback parent** that LTC
wakes when a task finishes. Verified with Pi 0.87.1 on Linux; native Windows/macOS Pi is untested.

## Set up

```bash
ltc setup --with-pi-extension     # daemon + skills for Codex, Claude Code and Pi + Pi extension
# or, for Pi only:
ltc install-pi-extension          # extension + Pi skill in ${PI_CODING_AGENT_DIR:-~/.pi/agent}
```

Then `/reload` or restart any running Pi. The Pi skill (`skills/long-task-callback/`) is a focused
`SKILL.md` about steered callbacks plus the shared reference as `REFERENCE.md`; Codex and Claude
Code skills do not carry Pi-session instructions. If you previously linked the Codex skill into
`~/.agents/skills/`, remove that link so Pi does not see two `long-task-callback` skills.

## Pi as the callback parent

Inside Pi's shell, `ltc run` binds the callback to the absolute `$PI_SESSION_FILE` (partial IDs
and `--last` are rejected):

```bash
ltc run --cwd "$PWD" --task "train model" -- python train.py
```

When the task finishes, the daemon publishes the callback to the session's private mailbox and the
extension hands it to Pi with `pi.sendUserMessage(..., {deliverAs: "steer"})`:

- **Busy Pi** gets it after the current tool-call batch, before the next model request — the run
  does not have to end. Steering never aborts a running tool, so keep foreground commands short.
- **Idle Pi** starts a normal turn.
- It continues the current branch in the same process; no second writer opens the JSONL.

### Delivery does not block the daemon

Publishing to the live mailbox hands the callback to Pi. The daemon returns immediately (worker
result 122, "published; awaiting ACK") and keeps launching tasks and delivering other sessions'
callbacks. Until the ACK, the session's retained target lease keeps that session's later
callbacks in order. `ltc ack` finalizes it directly.

Each daemon loop also checks every published, unacknowledged callback. It is moved to `failed`
for manual recovery — never replayed automatically — when:

| Condition | Meaning |
| --- | --- |
| the receiving Pi exited or closed its mailbox | it can no longer ACK |
| the session was reopened by another process | the published envelope belongs to the old owner |
| `--resume-timeout` (default 1 h) passed since publication | no ACK in time |

`last_error` records whether Pi had already admitted the message. Inspect the session, then
`ltc ack --id <id>` if it was handled, or `ltc retry --id <id>` to deliver it again.

### Ordinary and managed Pi

- **Ordinary Pi** (`pi` with the extension installed) receives callbacks only while it runs.
  Callbacks for a closed session are blocked until it is reopened; then `ltc retry --id <id>`.
- **Managed Pi** adds offline recovery:

  ```bash
  ltc pi --cwd "$PWD"                                   # new persistent managed session
  ltc pi --session /absolute/session.jsonl -- --model provider/model
  ```

  The launcher locks the session file before Pi opens it and holds the lock for Pi's lifetime.
  Only after the verified owner has fully exited may the delivery worker take the same lock and
  run `pi --print --session <file>`. Managed sessions are pinned: restart `ltc pi` to switch, fork
  or reload resources (`/reload` exits managed Pi rather than risk losing the pin). The advisory
  lock coordinates LTC writers; independently launched Pi processes do not honor it.

Callbacks freeze their Pi profile (`PI_CODING_AGENT_DIR`) and channel root
(`LTC_PI_CHANNEL_ROOT`). Check readiness with:

```bash
ltc doctor --agent pi --session "$PI_SESSION_FILE"
```

It reports mailbox registration or recovery eligibility, not verified receipt. Mailbox
publication, admission and session observation never replace the ACK.

## Pi as a child agent

```bash
ltc agent pi --cwd "$PWD" --task "review parser edge cases" \
  -- "Inspect parser.py and its tests. Report concrete defects and suggested fixes."
ltc doctor --operation agent --agent-worker pi
```

LTC runs `pi --print --mode text --no-session` with the prompt on stdin and saves stdout to the
managed private result file; no Pi session is persisted. The child inherits the submission-time Pi
profile, authentication, API, proxy and extension configuration; LTC removes Codex/Claude parent
markers plus `PI_SESSION_ID`, `PI_SESSION_FILE` and `PI_CODING_AGENT`. Tool permissions and project
extension trust follow Pi's own non-interactive configuration; LTC does not add `--approve`.

| Option | Pi behavior |
| --- | --- |
| `--model provider/model` | passed to Pi |
| `--reasoning-effort` | mapped to `--thinking`: `off`, `minimal`, `low`, `medium`, `high`, `xhigh`, `max` |
| `--system-prompt-file FILE` | Pi only; snapshotted privately at submission and passed to `--system-prompt` |
| `LONG_TASK_WAKEUP_PI_BIN` / `setup --pi-bin` | select the Pi executable |

A task template can set Pi defaults:

```yaml
pi:
  model: provider/model
  reasoning_effort: high
  system_prompt_file: prompts/system.md   # relative to the YAML file
```

See [template registration](template-registration.md) to make a template discoverable as an
`ltc-NAME` skill, and [examples/prompts/pi-system.md](../examples/prompts/pi-system.md) for a
sample system prompt.

## Tests

`tests/test_pi_live_integration.py` drives real Pi against an isolated loopback model fixture (no
paid model calls). Run it with `LTC_TEST_REAL_PI=1` and `pi` on PATH.
