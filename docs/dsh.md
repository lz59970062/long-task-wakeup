# DeepSeek Harness integration

[← Back to README](../README.md)

LTC works with DeepSeek Harness (`dsh`, the desktop app or the `dsh` CLI) in both directions: DSH
can run as a **child agent** (`ltc agent dsh`), and a live DSH session can be the **callback
parent** that LTC wakes when a task finishes.

## Set up

```bash
ltc setup --with-dsh --force --enable --now
```

`--with-dsh` installs the focused skill under `${DSH_HOME:-~/.dsh}/skills/long-task-callback` and records
the `dsh` launcher for the coordinator. If the desktop app did not put `dsh` on `PATH`, pass it
explicitly:

```bash
ltc setup --with-dsh --dsh-bin "/Applications/DeepSeek Harness.app/Contents/Resources/runtime/cli/bin/dsh" \
  --force --enable --now
```

DSH watches `~/.dsh/skills` (and the project's `.dsh/skills`), so a new session sees the skill
without a restart. Check readiness with:

```bash
ltc doctor --agent dsh --session "$DSH_SESSION_ID"
```

## DSH as the callback parent

Inside DSH's Bash tool, `ltc run` binds the callback to `DSH_SESSION_ID` automatically:

```bash
ltc run --cwd "$PWD" --task "train model" -- python train.py
```

stderr prints the exact wait command:

```text
ltc: DeepSeek Harness live callback: run as a background job (run_in_background: true): ltc wait --queue-dir … --task <id>
```

Run that command with the `bash` tool's `run_in_background: true`. DSH keeps the job alive across
turns and notifies this conversation when it exits. `ltc wait` prints the callback as soon as it is
queued; read the finished job's output with `job_output`, inspect the result, and ACK.

### Why there is no headless fallback

DSH's one-shot runner (`dsh --profile headless --session-id …`) refuses a session created under the
desktop agent preset:

```text
dsh: session "session-…" runs under agent preset "standard", which the one-shot runner does not compose
```

A second writer would also race the live DSH writer lock. LTC therefore **never resumes a DSH
session headlessly**:

- While a `ltc wait` is alive, the daemon leaves the session's callbacks to it.
- With no waiter, a DSH callback stays queued until the bound session runs `ltc wait` again.
  `ltc status` still lists it.
- Wait exit code 4 (callback diverted to a headless resume) never occurs for DSH; exit code 3 means
  the wait timed out and the callback is still queued.

This is the same guarantee as an ordinary Pi session: delivery happens only while the original
process is alive, and LTC never guesses a replacement owner. A one-shot
`dsh --profile headless` run exits after its turn, so do not submit LTC work from it; use a live
desktop or interactive CLI session.

## DSH as a child agent

```bash
ltc agent dsh --cwd "$PWD" --name parser-review -- "Inspect parser.py and report concrete defects."
```

LTC runs `dsh --profile headless` with the prompt on stdin and captures the final answer from
stdout. The headless profile does not use the desktop app's saved sign-in: set `DEEPSEEK_API_KEY`
(or restore stored credentials in the headless profile, as documented by DSH) or the child reports
an authentication error.

| Option | DSH behavior |
| --- | --- |
| `--model` | unsupported: the compose profile selects the model |
| `--reasoning-effort` | unsupported |
| `--system-prompt-file` | unsupported |
| `LONG_TASK_WAKEUP_DSH_BIN` / `setup --dsh-bin` | select the `dsh` launcher |

Child commands keep `DEEPSEEK_API_KEY` and strip `DSH_SESSION_ID`, `DSH_SHELL`, `DSH_WEB_URL`,
`DSH_PROFILE` and `DSH_HOME`, so the child cannot recurse into the parent session.

## Verification

The adapter and live-wait paths have unit coverage in `tests/test_dsh.py`:

```bash
python -m pytest tests/test_dsh.py
```

The end-to-end path is: submit a managed task from a DSH session, start the printed `ltc wait`
as a background job, let the task finish, call `job_output`, then ACK. Do not substitute a headless
resume for that test.
