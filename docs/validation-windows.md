# Windows validation — 2026-09-26

Baseline: `d2974cd` (Mac handoff), developed on `codex/windows-support`.
Package version remains `0.7.0a1`; no release or remote CI run is claimed here.

**Native execution and local callback lifecycle tests pass. Live delivery to this
already-open Codex Desktop conversation remains blocked by Codex's active-writer
check, so the complete original-session callback acceptance criterion is NOT met.**

## Environment

- Native Windows 10, build 19045, AMD64; `sys.platform == win32`, `os.name == nt`.
- Python 3.14.3, both the system interpreter and the project `.venv` redirector.
- Ordinary logged-in user, not administrator; built-in Task Scheduler running.
- Codex CLI 0.153.4, discovered through a forwarding `.bat` and npm `.cmd` shim.
  Claude's installed native npm launcher and local authentication check were also
  recognized; no real Claude model task was used for this validation.
- Disposable profiles, queues, global target locks and task names for automated
  checks. Restricted Agent sandbox access to Scheduler/private ACL files fails;
  native tests ran as the same ordinary user outside that sandbox, without
  broadening ACLs or requesting administrator elevation.

## Automated result

Full unittest discovery with both `LTC_TEST_WINDOWS_SCHEDULER=1` and
`LTC_TEST_WINDOWS_TASK=1`: **343 tests, 331 passed, 12 skipped, zero failures**,
33.157 seconds. Skips are POSIX-specific or other-platform integration cases.
Shared queue, recovery, cadence, callback and goal behavior continues to run on
Windows; platform fixtures were corrected rather than skipping whole shared suites.

The run used isolated `CODEX_HOME`, `CLAUDE_CONFIG_DIR`,
`CODEX_LONG_TASK_WAKEUP_TARGET_LOCK_DIR`, `TEMP` and `TMP` under the ignored
`.windows-dev/final-validation/` directory. Reproduce with:

```powershell
$env:LTC_TEST_WINDOWS_SCHEDULER = '1'
$env:LTC_TEST_WINDOWS_TASK = '1'
$env:PYTHONIOENCODING = 'utf-8'
& .\.venv\Scripts\python.exe -m unittest discover -s tests -q
```

Use disposable profiles and run as the logged-in user with Scheduler access.
The detailed local test log is `.windows-dev/final-validation/tests.log`.
`git diff --check`, Python compilation, installed CLI help/version and equality
of the two bundled skill texts also passed.

The final wheel built and installed into a separate local target. Importing from
that target verified the packaged PowerShell Scheduler bridge, bundled skill and
real Scheduler availability without importing the checkout's source package.
The current Codex profile's newly installed LTC skill was synchronized afterward.

## Native lifecycle evidence

- A real scheduled task continued after its submitting/control process was
  terminated. Reconciliation produced one workload execution, nonzero exit 7,
  one result and callback with the fixture's original session ID. The exited
  task registration was collected without replay.
- A temporary scheduled coordinator installed, acquired its queue, accepted an
  identity-bound reload, replaced its daemon, then drained and uninstalled.
- Actual COM definitions confirmed InteractiveToken, least privilege, IgnoreNew,
  no business triggers/restart policy, no execution limit and no battery/idle gates.
- Native process tests killed a runner and observed child and grandchild exit.
  Job containment also worked beneath the Agent tool's existing outer Job.
- Delivery tests killed the coordinator while a fake Agent was running: inherited
  callback/session locks remained held, a second queue could not deliver to that
  session, and the locks became available only after the delivery owner exited.
  These tests also passed using the project's venv Python launcher.
- A real PowerShell process executed the generated ACK against a path containing
  Chinese characters, spaces, a single quote and `&`. ACK succeeded when global
  retained-lease cleanup was denied; subsequent daemon reconciliation cleaned it.
- ACL tests checked private creation, permission tightening, atomic replacement
  failure, crash-released locks and parent-to-child handle transfer.

Mocked fault/identity tests cover unknown manager/launch outcomes without replay,
result-before-callback recovery, stale attempts, host/boot mismatches, unknown
boot, PID reuse, invalid venv ancestry, profile/queue conflicts and orphan
already-ACKed delivery locks during coordinator drain.

## Real two-minute workload and callback boundary

The actual original Desktop session was explicitly bound to task **`1aa2ba13`**.
A temporary coordinator named `ltc-windows-validation` used the real local Agent
profile and an isolated `.windows-dev/live-demo/queue`. It had no login trigger.
No previous LTC installation/coordinator existed on this Windows profile.

- Workload start: **2026-09-26 21:24:44.615 +08:00**.
- Workload completion: **2026-09-26 21:26:44.616 +08:00**.
- Measured workload duration: **120.000283 seconds**, exit **0**.
- Task became `completed`; durable result and log exist; owner registration was
  collected. No workload resubmission occurred.
- Four CLI callback attempts failed with:
  `thread-store conflict: thread ... already has an active writer`.
- Callback remains in `failed`; **no ACK or original-session receipt is claimed**.
  Local fake-Agent ACK tests are separate evidence, not a substitute for this test.

Full private records remain under `.windows-dev/live-demo/`; the coordinator log
is in the user's LTC state directory. No credentials, full prompts, session
records, task environment or local service configuration belong in Git.
The temporary coordinator registration was removed after validation; its daemon
and supervisor locks are released. The failed callback is preserved without ACK.

The current Windows implementation uses explicit CLI resume and disables the
unvalidated Desktop socket transport. A supported transport into the running
Desktop owner, or a session that the CLI can validly acquire, must be verified
before this live acceptance item can pass. Do not remove the Desktop writer lock,
change to `--last`, forge an ACK, or rerun the business task to hide this failure.

## Remaining limits

- No actual reboot, logout, sleep/resume or sudden-power-loss test was performed.
  Boot/host recovery was tested with controlled identity fixtures. The Windows
  boot counter must be readable; missing identity fails closed.
- File data is flushed and replacement requests write-through, but Windows has
  no supported POSIX directory-fsync equivalent. Directory-entry durability across
  power loss is not promised. Only local ACL-capable storage is supported here.
- Linux/macOS contract tests remain in discovery and CI. Their native process
  integrations were not rerun on this Windows machine. Remote Actions, Python
  3.9/3.12, ARM64, packaged/frozen executables and logout survival remain unverified.
- The environment did not provide an available Linux/WSL deployment for an extra
  local native regression run; no WSL installation was attempted.
