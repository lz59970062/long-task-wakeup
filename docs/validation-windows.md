# Windows validation — 2026-09-26

Baseline: `d2974cd` (Mac handoff), developed on `codex/windows-support`.
Package version remains `0.7.0a1`; no release or remote CI run is claimed here.

Consolidation on 2026-09-27: **418 tests, 406 passed, 12 skipped, zero failures**
in 46.582 seconds, including native Scheduler/lifecycle and real Core bridge tests.
The isolated run clears `CODEX_LONG_TASK_WAKEUP_DESKTOP_BRIDGE_FILE` and sets
`CODEX_LONG_TASK_WAKEUP_DESKTOP_APP_SERVER=0` in the test process only, so inherited
live Desktop configuration cannot redirect disposable fake-agent fixtures.
Explicit bridge integration tests still enable and exercise their own transport.
The successful suite callback was received and ACKed in the original conversation.
The earlier automated counts below are historical results at each development stage.

**Native execution and local callback lifecycle tests pass. The original-session
callback and ACK subsequently passed on 2026-09-27 with the explicit experimental
package-context Desktop bridge. The initial CLI-only failure below is historical;
the default CLI active-writer limitation remains.**

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

The default Windows implementation uses explicit CLI resume and disables Unix
socket discovery. An experimental [shared-server bridge](windows-desktop-bridge.md)
has since passed the real two-client protocol test; actual Desktop reconnection
and original-session receipt/ACK remain pending. A verified transport into the running
Desktop owner, or a session that the CLI can validly acquire, must be verified
before this live acceptance item can pass. Do not remove the Desktop writer lock,
change to `--last`, forge an ACK, or rerun the business task to hide this failure.

## Follow-up: experimental Desktop bridge

The active-writer investigation and opt-in connection procedure are in
[Windows Desktop bridge](windows-desktop-bridge.md). Local Desktop implementation
inspection found explicit transport overrides but no shared listener in the
currently running stdio configuration. The final opt-in uses a Desktop-owned
CLI wrapper, retaining Desktop's original flags and fresh App Tools environment.
Existing Desktop processes were left alone.

- A real isolated server rejected anonymous clients with HTTP 401 and accepted
  its private capability token with HTTP 101.
- Two independent clients through the same-user gateway initialized and resumed
  the same owned test thread. A documented history injection persisted the fixture
  without invoking a model or touching the real user conversation.
- The real-server test passed with global CLI **0.153.4** and separately with
  Desktop's exact **0.155.0-alpha.9.2** Core cache executable (SHA-256 matched to
  its installed package). WindowsApps package resources cannot be executed
  directly in this environment; the launcher must use a verified executable copy.
- Native gateway tests cover process peer identity, Origin/header rejection,
  refusal to send tokens to unrelated upstreams, active-client stop refusal,
  metadata conflicts, startup rollback and Job cleanup after bridge failure.
- The stdio wrapper relays server-initiated tool/approval requests, notifications,
  string/numeric IDs and messages larger than 1 MiB. Tests cover Desktop EOF
  with an LTC client connected, Core failure while stdin remains open, and
  shutdown during ping/notification traffic.
- A full run exposed a real-Core cleanup race: a child still held the private
  log briefly after its bridge exited. Graceful teardown now drains only the
  owned Job's children using verified native process handles before releasing
  state; abnormal exits retain the kill-on-close fallback.
- Both PowerShell 7 and Windows PowerShell 5.1 passed the launcher's earlier
  path/version checks. The installed wrapper's version passthrough matches Desktop Core
  `0.155.0-alpha.9.2`. No GUI launch or current Desktop restart was performed.
- Desktop submission intent now precedes sending `turn/start`. Regression tests
  reproduce a parent timeout and worker crash after submission and verify that
  the cross-queue lease blocks duplicate dispatch. Confirmed rejection/ACK/completion
  retain their distinct cleanup semantics.
- Full discovery with all three opt-ins (`LTC_TEST_WINDOWS_SCHEDULER`,
  `LTC_TEST_WINDOWS_TASK`, `LTC_TEST_CODEX_BRIDGE`) passed: **404 tests, 392 passed,
  12 skipped**, 55.042 seconds. Log:
  `.windows-dev/final-validation/wrapper-final-tests.log`. This final run selected
  Desktop's exact Core via `LTC_TEST_CODEX_BRIDGE_BIN` and the installed console
  launcher via `LTC_TEST_DESKTOP_CORE_WRAPPER`. Both real-server tests use empty
  disposable profiles with no model calls.
- The final wheel built, installed into a separate target, and imported its
  adapter, bridge and Job-cleanup modules successfully. The packaged
  `ltc-desktop-core` entry point, source compilation and synchronized skill texts
  were verified. Wheel SHA-256:
  `d76aabb2ca049e879eaca20bb6ee665fce275e0774b44fde97e82d318499eff7`.

**Pending:** restart Desktop onto the shared server, reopen the original session,
then validate actual completion delivery and ACK. The old failed callback remains
unacknowledged. These protocol tests do not declare that acceptance item complete.

### Subsequent user restart attempt: launcher blocked

The user ran `examples/windows/start-desktop-bridge.ps1` after closing Desktop.
Its direct packaged-GUI launch failed. Suspended native creation probes reproduced
Win32 error **5** for both packaged GUI executables; the cached matching Core
passed the same probe. No GUI probe thread was resumed, no existing Desktop
process was stopped, and no callback was delivered or acknowledged. Probes made
outside the newly added worker Job and via a temporary least-privilege scheduled
task still had an outer Job; that possible influence has not been ruled out.
The temporary diagnostic task was removed.

The launcher's earlier path/version preflight was insufficient. `-CheckOnly`
now includes suspended process creation with immediate native-handle cleanup,
reports `preflight_passed: false` and a nonzero exit on failure, and retains the
underlying Windows error. The normal launch path also gates on this probe.
Default direct startup remains blocked; the user should not repeat a restart
based only on the earlier path/version result. The package-context experiment
below adds narrower process-creation evidence, not complete Desktop acceptance.

Both Windows PowerShell 5.1 and PowerShell 7 now returned the expected JSON
(`native_error_code: 5`, `preflight_passed: false`, confirmed cleanup) and exit
code 1 against the packaged GUI. A cached-Core positive control returned true
and exit code 0 through the PowerShell launcher. The standalone probe also
reported errors 2 and 216 for missing and non-executable files, respectively;
a simulated cleanup timeout checked its five-second wait and exact-handle
closure. These checks exercised process creation only, never GUI execution.

### Package-context suspended creation

A standard-library base-Python helper was then launched with
`Invoke-CommandInDesktopPackage`, selecting this installed package's `App`
application and **without `-PreventBreakaway`**. `GetCurrentPackageFullName`
reported `OpenAI.Codex_26.915.4065.0_x64__2p2nqsd0c76g0`. In that context the
same `app\ChatGPT.exe` passed native suspended creation with error **0**,
`probe_resumed: false` and `cleanup_complete: true`; the cached Core also passed.
The helper still reported `current_process_in_job: true`, so this success did
not require removing the existing outer Job. The diagnostic record is
`.windows-dev/desktop-research/native-suspended-package-standalone-028b2f0732b0462ca4e248c120ad7a8a.json`.

The launcher adds an explicit experimental `-PackageContext` option using the
base Python installation's `pythonw.exe` and a standard-library helper. Only the
helper is launched through the package-context tool; it constructs the GUI
child's environment and invokes the GUI executable directly. It changes no
persistent user/global environment, package debug policy or package files, and
does not pass `-PreventBreakaway`. The default direct route retains its error 5 result.

The [Microsoft command documentation](https://learn.microsoft.com/en-us/powershell/module/appx/invoke-commandindesktoppackage?view=windowsserver2025-ps)
only guarantees package identity and virtualized resource access. Its token is
similar to, but not identical to, normal app activation; privacy controls,
settings and other behavior are not guaranteed. This probe does not establish
that the actual GUI, wrapper environment or App Tools work under that route.

`-PackageContext -CheckOnly` reports native creation separately from the full
preflight: `native_creation_passed` may be true while the running Desktop appears
in `launch_blockers` and `preflight_passed` is false. The launcher refuses to stop
that Desktop. Only after the user saves work, closes it and passes the full
preflight does `-PackageContext` request a new GUI process. **Actual GUI execution,
App Tools, original-session callback receipt and ACK are still unverified.**

Final launcher checks on 2026-09-27:

- `-PackageContext -CheckOnly` passed native creation in Windows PowerShell 5.1
  and PowerShell 7. The helper and suspended GUI child both reported the exact
  installed Codex package identity; child identity query status was 0. Both
  checks confirmed no resumed GUI thread and complete cleanup. The full
  preflight correctly remained false/exit 1 because the existing Desktop was open.
- The helper now refuses a GUI child with a missing, mismatched or unreadable
  package identity before actual launch. Ten focused regression tests passed
  in 0.082 seconds using ordinary-user execution. An initial sandboxed test run
  could not write its temporary fixtures; rerunning outside that restricted
  context required no source change. GUI launch calls in these tests are mocked.
- Both PowerShell scripts parse and `git diff --check` passes. The unrelated
  404-test bridge suite above was not rerun for these launcher-only changes.
- Actual launch waits for live bridge metadata for the selected profile with
  a connected client; this observation alone still does not establish App Tools
  compatibility or callback receipt/ACK.

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


## Live original-session acceptance — 2026-09-27

After an explicit `-PackageContext` Desktop restart, the shared Core reported the
original session loaded. The GUI and an App Tools `list_artifacts` call worked.
The previously completed native task `1aa2ba13` (120.0003 seconds, exit 0,
one workload launch) was requeued for callback delivery only. Its callback reached
the bound original conversation; the agent inspected the details, result and log,
then wrote the real ACK at 00:13:29 +08:00. The coordinator moved the callback to
`done`, with no running or failed copy. The workload was never relaunched.

Private local evidence: `.windows-dev/live-demo/callback-received.json`, the task
result/log and queue ACK/done records. These are intentionally not published.
Desktop 26.915.4065.0 and Core 0.155.0-alpha.9.2 were used. This validates this
specific bridge route, not default Store process creation, all App Tools or every
Desktop release. No writer lock was removed and no session was substituted.

The completion stream emitted a fragmentation warning while ACK still completed
delivery. Consolidation fixes preserve frame/message state across short polling
timeouts; portable regression tests cover partial headers, masks and payloads,
fragmented UTF-8, interleaved ping, close and message-size limits.
