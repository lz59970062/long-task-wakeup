# Codex Desktop

Start Desktop normally. From a Desktop session, `auto` selects `codex-queue`: when the task ends, the coordinator runs the public `codex queue --thread UUID --message TEXT` once with your session's environment and Codex profile. Desktop needs no launcher, shared Core, private pipe or restart.

## Check it with a small task

Install the [preview](../installation.md#install-the-preview), then submit a task from a Desktop session:

```bash
ltc setup --force --enable --now
ltc run --cwd "$PWD" --task "Desktop callback example" -- python -c "print('Desktop callback ready')"
```

The submission prints `ltc: callback mode: codex-queue`. In a PowerShell tool environment, use `--cwd "$($PWD.Path)"` in place of `--cwd "$PWD"`. If Desktop does not mark its sessions, pass `--callback-mode codex-queue`.

Codex holds the message until a busy chat finishes its turn. A closed chat receives it when you open it again.

## What the states mean

A queued message is not a received one. The callback stays pending until the chat runs its ACK command, and `ltc status` shows `in the Codex queue; waiting for the thread to ACK`. A callback waiting for its ACK does not hold back later callbacks for the same chat.

| What happened | What LTC does | What you do |
| --- | --- | --- |
| `codex queue` reported an error, for example no such thread | Moves it to `failed` with the error; nothing was queued | Fix the cause, then run `ltc retry --id ID` |
| `codex` cannot be started | Same, including an executable that Windows cannot resolve | Fix the path or set `CODEX_LONG_TASK_WAKEUP_CODEX_BIN`, then retry |
| The command was killed, timed out, or succeeded with output LTC cannot read | Outcome unknown: `failed`, and later callbacks for this chat wait | Look in the chat, then run `ltc ack --id ID` or `ltc cancel --id ID`; retry is refused |

LTC keeps a receipt in `codex-queue/` in the queue directory and never sends the same callback twice on its own.

## ACK and sandboxes

The ACK runs under the chat's own permissions. If its sandbox cannot write the LTC queue directory (`~/.ltc` by default), the command fails and the callback stays pending. Approve the command, add that directory to the sandbox's writable roots, or run the ACK from a terminal. Nothing is sent again in the meantime.

## Limits

Verified with real Codex 0.166.0 on Linux, using the TUI as the open chat; see the [validation record](https://github.com/lz59970062/long-task-wakeup/blob/main/docs/validation-codex-queue.md). A real Desktop app, macOS and Windows have not been run. macOS prefers the Codex bundled with the app, and `CODEX_LONG_TASK_WAKEUP_CODEX_BIN` overrides it. The executable and profile are frozen at submission. `--thread` needs the thread UUID: a callback bound to a session name goes to `failed` with that error, and `ltc retry --id ID --callback-mode cli` moves it. The message travels on the command line, so other local users can see it in the process list while the command runs, and an unusually long `--callback-format full` message can exceed the system's argument limit; the compact default stays small.

Update the coordinator before using this route: new records are callback version 5 and task version 6, and an older coordinator keeps them unsent instead of resuming the thread.

## Move a callback saved for the old launcher

The launcher and its shared Core are gone. `ltc setup` no longer generates them and `--callback-mode desktop` is refused for new work. A saved `desktop` callback keeps its results. On macOS and Windows it waits in `failed`; on Linux it keeps delivering through the local App Server socket and only waits in `failed` if that socket is unavailable. Move it with:

```bash
ltc retry --id ID --callback-mode codex-queue
```

This does not rerun the workload.

Quit any Desktop opened from the launcher before you upgrade, because it cannot start its Core again once the wrapper's target is gone. Then open Desktop normally.

On Windows, a bridge process can outlive Desktop. If one remains, end it. Its App Server stops with it. This command has not been run on Windows:

```powershell
Get-CimInstance Win32_Process -Filter "CommandLine LIKE '%long_task_callback.desktop_bridge%'" | ForEach-Object { Stop-Process -Id $_.ProcessId }
```

`ltc setup` names any leftover launcher and bridge files, including `desktop.json`. Delete them once Desktop and the bridge have stopped.
