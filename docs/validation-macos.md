# macOS validation — 2026-09-26

Baseline: `7269b93` on `codex/0.7.0-preview`, plus the macOS adaptation delivered with this record.
The package version remains `0.7.0a1`; this record does not describe a published release.

## Environment and result

- macOS 26.6.2 / Darwin 25.6.0, Apple Silicon (`arm64`).
- Python 3.14.6 in the project's `.venv`, installed with `pip install -e .`.
- Native `/bin/launchctl` and Apple's `/usr/bin/screen`.
- Disposable Agent profiles, queues and global target-lock directories under `/private/tmp`.
- Full unittest discovery with `LTC_TEST_LAUNCHD=1` and `LTC_TEST_SCREEN=1`:
  **282 tests, 280 passed, 2 skipped, no failures**, 6.095 seconds.
- The skipped tests are the opt-in Linux systemd integration and Linux standalone
  diagnostics repair. Linux adapter contracts still run with mocked platform managers.
- `ltc install-launchd --print` produced a parseable plist with pinned queue and
  profile paths; `ltc run --backend launchd --dry-run` left no task record.
- `git diff --check` passed; the repository and packaged skill texts match.

## Native lifecycle evidence

The launchd integration suite used disposable service labels and no model calls:

1. A temporary coordinator launched a real LTC task through a separate launchd job.
2. Removing the coordinator left the task alive; recovery did not duplicate it.
3. Releasing the task produced the expected nonzero exit status, private log,
   durable result and exactly one callback bound to the fixture's original session.
4. Recovery collected the exited job registration without replaying its command.
5. A temporary LaunchAgent installed against an isolated profile passed `doctor`.
   A subsequent setup requested a safe reload; the coordinator kept its PID and
   published a new runtime timestamp.

The screen integration suite also ran against Apple's bundled screen. It verified
task survival after standalone coordinator termination and recovery with one result
and callback, plus safe startup in the presence of a stale PID file.

Unit coverage includes unknown launch outcomes, unavailable managers, literal
paths, private plist contents, stale worker attempts, host/boot mismatches, process
start identity, reload identity checks and platform-specific service selection.
The reboot boundary includes a submitter that cannot read the boot UUID: the
coordinator's separately saved launch boot still prevents replay after reboot.

## Live original-session callback

After the isolated test suite, the user requested a real two-minute callback demo.
The existing idle `0.6.5a2` LaunchAgent was backed up and replaced with the local
`0.7.0a1` installation. Existing callback hooks and installed skills were preserved.
The coordinator now uses this checkout's `.venv/bin/python` and source entry point.

- Task: `9e4dabc9`, native `launchd` backend.
- Started: `2026-09-26T14:17:39.758581+08:00`.
- Completed: `2026-09-26T14:19:39.834270+08:00`.
- Measured sleep workload duration: **120.076 seconds**; saved result exit code: **0**.
- The original Codex conversation received the callback, inspected the log/result,
  and successfully ran the supplied ACK command.
- Follow-up inspection found the task `completed`, one delivery attempt, its
  callback in `queue/done`, a matching durable ACK, and `owner_collected_at` set.
- Codex CLI on this Mac: `0.157.1`. The optional Desktop control socket was absent
  before the demo; the existing CLI fallback path was used.

Evidence remains in this user's LTC state directory: `tasks/9e4dabc9/`,
`queue/details/9e4dabc9.md`, `queue/done/9e4dabc9.json` and `queue/acks/9e4dabc9.json`.
Only this summary is included in the repository; private session records, full
callback payloads and user configuration are not copied into version control.

## Limits

Tests ran outside the tool sandbox because Darwin process identity interfaces are
restricted inside it. No actual reboot, logout, sleep/wake cycle, Intel Mac,
live Claude callback or Desktop-socket-specific delivery was tested. Live Codex
original-session delivery and ACK were verified as recorded above.
The added Linux/macOS GitHub Actions matrix has not been executed remotely in this
session. Temporary integration jobs were removed by their fixtures; the later
user-requested demonstration upgraded the idle production coordinator as described above.

Reproduction commands and installation instructions are in [macOS setup](macos.md).
The next development stage is documented in the [Windows handoff](handoff-macos-to-windows.md).
