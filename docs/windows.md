# Native Windows

LTC runs on Windows 10 or later with Python 3.9 or newer. A per-user Task Scheduler
task keeps the callback coordinator available. Each business attempt has its own
separate, on-demand scheduled task, and its runner owns a Job Object containing
the command and its descendants. Screen, systemd and WSL are not prerequisites.
WSL continues to use the Linux deployment separately.

Use an ordinary logged-in Windows user account with access to Task Scheduler and
an ACL-capable local filesystem, preferably NTFS. LTC uses the current interactive
user token and does not request administrator elevation or store a Windows
password. A locked desktop is still logged in; logging out or rebooting can
interrupt work. This preview does not provide an unattended Windows service that
runs while the user is logged out.

## Install and configure

Start from a checkout containing `src/long_task_callback/platforms/windows.py`.
Run these commands in PowerShell at that checkout:

```powershell
py -3 -m venv .venv
& .\.venv\Scripts\python.exe -m pip install .
& .\.venv\Scripts\ltc.exe --version
& .\.venv\Scripts\ltc.exe setup --service windows-task --force --enable --now
```

If the Python launcher is unavailable, use the full path to your Python executable.
No virtual-environment activation or PowerShell execution-policy change is needed.
Keep the virtual environment and installed runtime at their configured locations:
the scheduled tasks pin absolute paths. For an editable development installation,
use `-m pip install -e .` and keep the checkout in place as well.

`--service auto` selects `windows-task` on Windows. `--enable` adds the current
user's login trigger; `--now` starts the coordinator immediately. Business tasks
have no login/periodic trigger, restart-on-failure policy or missed-trigger catch-up.
Their execution time limit is disabled and the multiple-instance policy is
`IgnoreNew`, alongside LTC's per-attempt admission and locks.

Setup installs the bundled Agent skill and preserves any existing callback hook.
It uses the current `CODEX_HOME` (default `%USERPROFILE%\.codex`) and
`CLAUDE_CONFIG_DIR` (default `%USERPROFILE%\.claude`), and pins their paths, Agent
executables and PATH in private coordinator configuration. Authenticate the Agent
CLI using that same account and profile. Setup's local checks do not establish that
a model call or original-session callback will succeed.

The default queue is `<CODEX_HOME>\long-task-wakeup\queue`. To select another one,
pass the same absolute `--queue-dir` to setup, submission, doctor and ACK. A profile
has one coordinator queue; do not point a live profile coordinator at a second
queue. Use a separate profile for concurrent isolated deployments. To change an
existing coordinator's queue, first drain its tasks and callbacks, uninstall it,
then configure the new queue.

## Submit and acknowledge

Run from the original Agent conversation so the session can be detected, or pass
its actual ID with `--agent codex|claude --session <original-session-id>`. Never
substitute `--last` or copy a Mac's session binding to Windows.

```powershell
$ltcPython = (Resolve-Path .\.venv\Scripts\python.exe).Path
$ltcExe = (Resolve-Path .\.venv\Scripts\ltc.exe).Path
& $ltcExe run --backend windows-task --task 'two-minute work' -- $ltcPython -c 'import time; time.sleep(120)'
& $ltcExe agent codex --task 'review changes' -- 'Review this repository and return evidence.'
& $ltcExe done --task 'external job' --exit-code 0
```

These examples assume the invoking shell carries the original session binding.
Use native executables with an argument list. Recognized npm and simple forwarding
`.cmd` launchers are resolved without passing user arguments through `cmd.exe`.
An unrecognized batch wrapper is rejected; configure its underlying executable
with `--codex-bin` / `--claude-bin` or the corresponding Agent binary environment
variable. For deliberate shell syntax, invoke the shell explicitly, for example
`powershell.exe -NoProfile -Command <script>`.

Windows callbacks use the Agent CLI to resume the exact bound session by default.
An [experimental explicit Desktop bridge](windows-desktop-bridge.md) can connect
Desktop and LTC to one shared App Server. The tested Windows Store package still
fails default direct startup with error 5. The launcher's explicit
`-PackageContext -CheckOnly` option is based on a successful suspended-creation
probe under package identity. It reports `native_creation_passed` separately from
running-Desktop and other `launch_blockers` that fail the full preflight. After saving work, closing Desktop
and passing the full preflight, `-PackageContext` requests experimental startup.
It uses a standard-library base-`pythonw.exe` helper under package identity,
without persistent environment changes, package debugging policy changes or
`-PreventBreakaway`. Microsoft's
[tool limitations](https://learn.microsoft.com/en-us/powershell/module/appx/invoke-commandindesktoppackage?view=windowsserver2025-ps)
include a token that differs from normal activation and no guarantee of other app
behavior. GUI execution, one App Tools call, original-session delivery and ACK passed
on the tested installation on 2026-09-27; other Desktop versions and workflows remain unverified. Read the linked bridge procedure before closing Desktop for a test.
LTC does not guess a Desktop socket or silently switch sessions. Each callback includes a PowerShell
ACK command pinned to this installation. Copy that command exactly, including
the leading `&` and single-quoted paths; doubled single quotes inside paths are
intentional. For example:

```powershell
& 'C:\LTC\.venv\Scripts\python.exe' 'C:\LTC\src\long_task_callback\_entry.py' 'ack' '--queue-dir' 'C:\LTC state\queue' '--id' '<callback-id>'
```

The actual emitted runtime path may differ. Inspect the result and requested
details before ACK. ACK records callback receipt; it does not mark a goal complete.
Delivery is at least once, so inspect existing work before acting on a repeated
callback. `cancel` continues to cancel callbacks only, not the business process.

The live test on this machine could not resume its already-open Codex Desktop
session: Codex 0.153.4 reported `already has an active writer`. Native workload
completion and fake-Agent delivery/ACK passed, but that original-session delivery
did not. Ending a turn does not establish that Desktop releases its session owner.
Do not delete Codex's writer lock or change the callback target to work around it;
retain the failed callback for a supported Desktop transport or valid CLI ownership.
The bridge's real App Server protocol test now demonstrates two clients resuming
one owned test thread, without any model call; this is narrower than real Desktop
receipt and ACK.

## Inspect, update and remove

```powershell
& .\.venv\Scripts\ltc.exe install-windows-task --print
& .\.venv\Scripts\ltc.exe doctor --backend windows-task --agent codex --session '<original-session-id>'
Get-ScheduledTask | Where-Object TaskName -Like 'ltc-coordinator-*'
```

Coordinator configuration is
`<CODEX_HOME>\long-task-wakeup\windows-codex-long-task-wakeup.json`; its log is
`<CODEX_HOME>\long-task-wakeup\daemon.log`. The task registration contains the
fixed runtime and private file references, not the full environment or prompts.
`install-windows-task --print` previews that registration without printing the
private configuration payload.

After upgrading the package, update the same profile/queue with:

```powershell
& .\.venv\Scripts\ltc.exe setup --service windows-task --keep-skill --force --enable --now
```

The live coordinator verifies its process identity, waits for active callback
deliveries to drain and exits; its scheduled supervisor starts the replacement
with the updated configuration. Business runners remain independently owned.
An unverifiable existing owner is left running for inspection.

To remove the coordinator's login registration:

```powershell
& .\.venv\Scripts\ltc.exe uninstall-windows-task
# If installed under a custom name, use that same --name here.
```

Uninstall requests a verified, drained coordinator shutdown. It does not delete
task records, ACKs or business-task registrations, or cancel live business work.
Drain tasks before retiring a deployment so completed work can still deliver its
callback. Do not remove lock files manually: an inherited handle may still hold
the lease after its original coordinator exits.

## Guarantees and limits

- Closing the submitting terminal or replacing the coordinator does not own the
  lifetime of an independently scheduled business runner. Runner exit closes its
  Job Object and ends surviving descendants; interrupted work is not called success.
- Missing owners, access errors, Scheduler timeouts, boot changes and ambiguous
  submission responses never authorize automatic business replay. Saved results
  take precedence; missing callbacks are reconstructed with the same ID.
- New private files and directories have protected ACLs granting the current user
  and SYSTEM access at creation. Filesystems without persistent ACL support are
  rejected. A restricted Agent sandbox token may be unable to access those files
  or Task Scheduler; perform authorized installation/testing in an ordinary user
  process without broadening the ACL or switching to an administrator account.
- File data is flushed before same-volume replacement, with a Windows write-through
  request. A reader that denies deletion sharing can make replacement fail while
  leaving the old destination intact. Windows has no supported POSIX-equivalent
  directory fsync: persistence of creates, renames and deletes through sudden power
  loss is not guaranteed. Keep queue state on a local ACL-capable volume.
- Sleep pauses execution; logout, reboot and machine loss can interrupt it. No
  backend preserves a running process across reboot, and LTC does not auto-replay it.

See [Windows validation](validation-windows.md) for actual native test and callback
evidence, including remaining limitations. The [Mac-to-Windows handoff](handoff-macos-to-windows.md)
is the historical implementation plan, not the current support status.

## Isolated developer checks

The normal suite includes real ACL, handle handoff, process-tree and PowerShell ACK
checks. Native Task Scheduler tests are explicit opt-ins and use disposable task
names, profiles and queues, with local fake Agents and no model calls:

```powershell
$env:LTC_TEST_WINDOWS_SCHEDULER = '1'
$env:LTC_TEST_WINDOWS_TASK = '1'
& .\.venv\Scripts\python.exe -m unittest discover -s tests -p test_windows_backend.py -v
& .\.venv\Scripts\python.exe -m unittest discover -s tests -p test_windows_integration.py -v
```

Run as the ordinary logged-in user outside a restricted Agent sandbox. The CI
matrix includes Windows Python 3.9 and 3.12 alongside Linux/macOS; its native jobs
are configured to use the runner's own Task Scheduler session without alternate
credentials. A configured workflow is not evidence of a completed remote CI run.
