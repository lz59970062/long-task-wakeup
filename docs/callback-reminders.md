# Callback format, reminders and the user hook

[← Back to README](../README.md)

## Compact callbacks

Task-completion callbacks now carry a short result envelope and artifact/ACK paths. Full evidence
is saved privately in `details/<callback-id>.md`; custom instructions are marked
for reading before acting. Use `--callback-format full` for full inline callbacks.
The existing system/user reminder cadence still applies. This reduces repeated
prose without truncating saved results or user instructions.

## User callback hook

`ltc setup` also creates an empty, user-editable callback prompt hook at
`${CODEX_HOME:-~/.codex}/long-task-wakeup/callback-hook.md`. Existing hook content is never
overwritten, including by `setup --force`. The file is read again before each due user-reminder
attempt, so edits apply to the next due callback without restarting the daemon. Non-empty
content is appended to the callback prompt under `[long-task-callback-user-hook]`; a missing,
empty, or temporarily unreadable file does not block callback delivery.

## Callback reminder intervals (0.6.6)

Repeated standard instructions and the user callback hook have separate intervals:

```bash
ltc prompt-policy --system-every 4 --user-every 3
ltc prompt-policy                       # show effective settings without modifying state
ltc prompt-policy --user-every 1        # change just the user interval; keep the other
```

Both values must be positive integers; `1` restores reminders on every callback.
Settings are saved in `${CODEX_HOME:-~/.codex}/long-task-wakeup/callback-prompts.yaml`:

```yaml
system_every: 4
user_every: 3
```

Each queue counts separately for each agent and bound session. The first callback
shows both reminders. With the defaults, standard reminders appear at #1, #5, #9,
… and user reminders at #1, #4, #7, … . New compact envelopes omit counter prose;
legacy callbacks retain their cadence line. Goal reminders use the same counter.
The number counts distinct callbacks reaching their first
delivery attempt, not successful ACKs; a failed delivery can consume one number.
Retries and daemon restarts reuse the same stored number and decisions. Queuing,
canceling before delivery, and dry-run do not consume numbers. Changing settings
applies to new allocations; retrying an old callback retains its original schedule.

New compact callbacks retain task/result identity, artifact references, bound session
and the exact ACK command. Full command lines, messages and handoff instructions
are saved privately at the Details path; custom instructions and recovery guidance
require reading it before acting. Standard reminders are short and periodic. The
skill's core rules apply even when reminder prose is omitted.

User text in `callback-hook.md` is appended only on due attempts, and is reread on
each such attempt, including retries. Editing it needs no restart. An empty hook
adds no text. Policy changes likewise need no restart once the new delivery worker
is installed. Existing queued callbacks without a compact prompt keep their full
standard text; their user hook can still follow the new cadence. `--last` always
keeps full reminders because its actual conversation identity is not pinned.

The counter and per-callback allocations are atomically saved together under
`<queue>/prompt-counters/`, with a separate file lock. If configuration or counter
state cannot be read/validated/written, LTC warns and sends full reminders instead
of suppressing them or blocking the result. It does not overwrite corrupt history.
