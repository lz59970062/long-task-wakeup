# 0.7 architecture and platform handoff

For the completed Mac work, live callback evidence and the next implementation
steps, start with [Mac-to-Windows handoff](handoff-macos-to-windows.md).

The 0.7 preview supports Linux, macOS and native Windows. PI and DSH integrations
are not implemented. The public commands and legacy screen records remain compatible
with 0.6 tasks. Native tasks use record version 2, so older daemons reject them
rather than accidentally running them through screen. Do not run an older daemon against newly submitted
`systemd-user`, `launchd` or `windows-task` records: use a separate preview queue, or drain and upgrade its
coordinator before submitting new tasks.

## Responsibilities

| Module | Responsibility | Must not do |
| --- | --- | --- |
| `cli.py` | Argument parsing, existing queue/goal lifecycle, admission and reconciliation | Infer completion from an unavailable process manager |
| `platforms/base.py` | Execution-owner contract and explicit uncertain outcomes | Own agent prompts or resume commands |
| `platforms/screen.py` | GNU screen owner for Linux/containers and macOS | Infer no process from a failed query |
| `platforms/linux.py` | Independent systemd user units, native identity | Restart arbitrary user commands |
| `platforms/macos.py` | Independent one-shot launchd jobs, native host/boot/process identity | Register business commands for login or automatically restart them |
| `launchd_service.py` | macOS coordinator LaunchAgent installation and safe activation | Stop a live coordinator to apply configuration |
| `platforms/windows.py`, `windows_scheduler.ps1` | Per-attempt Task Scheduler owners, host/boot/process identity and admission | Replay ambiguous submissions or infer absence from RPC failure |
| `platforms/windows_io.py` | Protected ACLs, atomic replacement, transferable exclusive file handles | Claim POSIX directory-fsync or power-loss guarantees |
| `platforms/windows_process.py` | Runner-owned Jobs, shell-free executable resolution, explicit handle inheritance | Launch an uncontained workload or pass prompts through cmd.exe |
| `windows_service.py` | Per-user scheduled coordinator supervisor and drain/replacement | Stop active business jobs during coordinator updates |
| `platforms/posix.py` | OS-held file locks and directory syncing | Pretend these primitives work on Windows |
| `storage.py` | Atomic private JSON/text persistence | Store state in installation directories |
| `agents/` | Agent metadata, session discovery, child/resume commands and capabilities | Launch tasks or change queue lifecycle |
| `callbacks.py` | Compact, transport-independent envelopes | Drop custom instructions without an explicit details-read requirement |
| `diagnostics.py` | Environment, configuration and lifecycle observations with Agent-owned recovery guidance | Execute repairs, change producer exit codes, or equate local checks with authenticated delivery |
| `runtime.py`, `_entry.py` | Launch workers from this exact installation | Rediscover another LTC on PATH |
| `templates.py` | Validated user and built-in task templates | Execute template text as a shell command |

The existing queue and goal coordinator remains in `cli.py` during this migration.
Its compatibility wrappers preserve existing Python callers and test interception.
Do not add OS-specific branches throughout that file: add a backend and a small
admission/selection integration. Extract further lifecycle modules when they can
own their data and operations without circular imports or large callback bundles.

## Linux ownership and recovery

```
CLI -> private task.json + environment.json -> coordinator
                                              |
                         systemd-run --user -> separate per-task service
                                              -> LTC worker -> user command
                                              -> result.json -> callback queue
coordinator -> agent adapter / optional Codex Desktop transport -> original session
```

`--backend auto` selects an available systemd user manager (v240+) and otherwise
uses screen. `--backend systemd` requires that manager; `--backend screen` selects
the compatibility backend. The choice is frozen in `execution_backend` at
submission. Missing backend in an old record means screen. Existing processes
are never migrated between owners. Screen fallback does not promise survival of
stopping its containing systemd service; detachment is not cgroup isolation.

A native owner name includes queue identity, task ID and attempt number. Services
use `Type=exec`, `Restart=no`, separate control groups, private log files and
collection after exit. Unit properties contain a worker entry point and private
task-file reference, not the task environment, prompt or launch token.

Execution states are `submitted -> launching -> running -> completed`.
`interrupted`/unknown and `launch_failed` are distinct from a nonzero command exit.
A worker rechecks its attempt under the task lock, records running before the
command, and writes result before callback creation. Recovery first checks the
result identity. An unreachable user manager is `UNKNOWN`, never proof of death.
A timed-out/nonzero native submission is reconciled without automatic resubmission.
Even a vanished native unit can have performed work: an ambiguous attempt is
reported for inspection, not retried. Missing boot identity is not evidence of a
reboot. Boot changes interrupt unfinished attempts; checkpoints require an explicit
new task chosen by the resumed agent.

Task results and callback delivery are separate: stable callback IDs and ACK
markers provide at-least-once delivery. ACK is receipt, not success of the task or
completion of the goal. Existing `cancel` cancels callbacks, not running workloads.

## Containers without systemd

Docker/AutoDL use the screen backend and a foreground, standalone or explicitly
Supervisor-managed coordinator. `setup --service auto` selects systemd only when
the user manager is reachable, otherwise standalone; it does not modify a hosting
provider's Supervisor automatically. Task ownership (`--backend`) and coordinator
hosting (`--service`) are separate. No privileged container or host daemon socket
is required. See [container deployment](containers.md).

A standalone coordinator can exit without ending the screen task, provided the
container remains running. If that coordinator is the container main process,
its exit ends the container; use a long-lived outer supervisor for coordinator-only
restarts. Container stop/recreation is interruption, not process persistence.
Persist state, workspace and agent profiles, and do not promise automatic binding
recovery on another container/host. Real screen tests force the user bus unavailable
and run both on the host and in a non-root Docker container.

Standalone startup checks the OS-held queue lock rather than trusting a saved PID.
Reload requires the matching queue and process identity (boot, machine, PID
namespace and start ticks), and uses a Linux pidfd to avoid signaling a reused PID.
If identity or pidfd support is unavailable, the existing coordinator remains
untouched. Persistent PID/runtime files are scoped to `CODEX_HOME`; use separate
profiles for separate standalone coordinators.

## macOS ownership and recovery

The macOS adapter uses a LaunchAgent coordinator and independently registered
one-shot launchd jobs in `gui/<uid>`. `--backend auto` selects launchd when that
domain is reachable; `--backend launchd` requires it. `--service auto` selects
the LaunchAgent in that session, otherwise standalone. Headless sessions can
explicitly use screen and a standalone coordinator. See [macOS setup](macos.md).

Task labels include queue, task and attempt identity. Plists live in private task
directories, outside `Library/LaunchAgents`, with `RunAtLoad=true`,
`KeepAlive=false` and `AbandonProcessGroup=false`. Only the coordinator uses
`KeepAlive=true`. Workers verify their launchd label/environment, parent and
manager-reported PID, then recheck the saved attempt, queue, host and launch boot
under the task lock. launchd properties contain only worker arguments and file
references; the saved environment and prompt remain in private task files.

The coordinator records the launch boot separately because a sandboxed submitter
may not be permitted to read `kern.bootsessionuuid`. Host identity uses
`gethostuuid`; process identity uses `proc_pidinfo` with microsecond start time.
macOS has no Linux pidfd signal primitive. Reload writes an identity-bound request
into the queue; only the matching coordinator consumes it, drains deliveries and
execs itself. An old PID cannot authorize signaling an unrelated process.

`launchctl print` is diagnostic text, not a stable API. The adapter recognizes
only specific top-level PID/state observations; unknown formats and failures
remain UNKNOWN. Missing-service responses require a reachable GUI domain before
absence is accepted. A loaded job that has never run is not treated as dead.
Completed/terminal tasks have exited registrations collected without stopping a
live worker. Plists remain as evidence and are never automatically loaded again.

Apple's bundled screen is supported without its missing `-Logfile` option.
Local Desktop socket peers are checked with Darwin `getpeereid`; CLI delivery
remains the fallback when the optional Desktop socket is unavailable.

Terminal closure and coordinator replacement do not stop native tasks while the
user GUI session remains alive. Logout/reboot can interrupt execution and sleep
pauses compute. No automatic task replay or unattended logout survival is promised.

## Windows ownership and recovery

The [original handoff](handoff-macos-to-windows.md) records the acceptance contract;
[Windows setup](windows.md) describes the implementation. `windows-task` is both
the native backend and a coordinator service choice. Each attempt registers a
separate, on-demand Task Scheduler action for the logged-in user. Business jobs
have no triggers, restart policy, battery/idle constraints or execution time limit.
IgnoreNew and the durable attempt check jointly prevent concurrent/replayed work.
COM control uses a fixed PowerShell script and JSON input/output, not localized
schtasks text or interpolated shell commands. Lost replies remain UNKNOWN.

Worker admission verifies the saved queue, task, attempt, owner, machine and boot,
then the Scheduler instance PID against a live process identity. Windows Python
venv redirectors need a verified direct parent chain: registered executable,
actual parent/child executable images, SID, creation times, host and boot must
match. Identity queries use native handles; `os.kill(pid, 0)` is never a Windows
liveness test. Boot identity uses Windows' documented successful-boot counter;
an unavailable counter is unknown and cannot authorize a replay or native start.

Before creating any workload, the runner assigns itself to an unnamed,
non-inherited KILL_ON_JOB_CLOSE Job. Future child processes inherit containment
atomically from their parent, avoiding a create-suspended/assign crash window.
The runner retains the sole Job handle until process exit. The coordinator never
owns it; runner death collects its process tree and recovery reports interruption.

Private state requires an ACL-capable filesystem. Files are created with a
protected current-user/SYSTEM DACL, flushed, then replaced with write-through
MoveFileEx. Windows has no supported POSIX directory-fsync equivalent; namespace
durability under sudden power failure is explicitly unverified. Locks use exclusive
CreateFile sharing, whose lifetime follows the open file object. Explicit inherited
handle copies let delivery workers retain both per-callback and cross-queue session
leases after their coordinator dies. Handles never pass into the Agent workload.

A scheduled coordinator supervisor reads private pinned configuration and replaces
the daemon after identity-verified reload requests drain active deliveries. During
drain, new work is not admitted. Windows does not use POSIX execv replacement.
Service removal deletes the login registration and drains the coordinator without
stopping independent business owners. Changing a live coordinator's queue requires
draining it first; each profile owns one coordinator/runtime record.

Windows callbacks use the bound Agent CLI session. Native executables and recognized
npm/forwarding batch shims are resolved without a shell; unknown batch scripts fail
explicitly. Generated ACK/goal commands are literal PowerShell argument arrays.
The optional Windows Desktop bridge is an experimental explicit transport.
Desktop launches `desktop_core.py` through an explicit CLI override. It relays
Desktop's stdio protocol and preserves the environment and configuration that
Desktop supplies for its tools and approvals. `desktop_bridge.py` owns one App
Server and a gateway that verifies connected Windows peers before adding a
private upstream Bearer token. Desktop and LTC must use that same server.
`desktop_connection.py` validates metadata/profile,
and `platforms/windows_tcp.py` authenticates the connected reverse TCP tuple,
PID, SID and creation identity. No unauthenticated upstream is exposed; browser
Origin headers are rejected. See [bridge setup and limits](windows-desktop-bridge.md).
Real Windows Desktop original-session delivery and ACK passed with the explicit
package-context bridge on 2026-09-27; the default CLI active-writer limitation remains. A durable submission
intent precedes `turn/start`, so response loss or a parent timeout preserves the
cross-queue lease instead of authorizing another delivery.

The initial desktop contract covers terminal closure and independent coordinator
restart while the user session exists. Logout, reboot and WSL shutdown can
interrupt execution. Sleep pauses compute. WSL initially keeps LTC, its agent and
work in the same distro, with state on its Linux filesystem. Unattended logout
survival requires a separately specified service/credential setup.

## Adding another Agent

Implement `AgentAdapter` in `agents/` and register it in `agents/__init__.py`.
It supplies the name, display name, session ID environment, environment markers,
child-result mode, supported reasoning options, command builders and detection.
Templates derive their supported adapter sections from that registry. Unknown
persisted agent names fail closed; they never fall back to a different agent.
PI and DSH should be added only after validating their real noninteractive launch,
session routing, result capture and approval semantics. CLI completion is not proof
that the intended session accepted a callback. Codex Desktop transport remains a
separate, Codex-specific path; adapters can use CLI resume without implementing it.

## Compact callback protocol

New `run`, `agent` and `done` callbacks default to `compact-v1`: ID, task, outcome/exit, artifact directory,
bound session and executable ACK. Command lines, timestamps and full handoff prose
are in the private `details/<id>.md` artifact. Envelopes explicitly require reading
it when custom messages/handoffs or recovery instructions apply. `--callback-format
full` preserves inline detail. Legacy queued records retain their original format.
Standard/user reminder decisions still follow the 4/3 policy and are frozen per
callback at first delivery; no new counter prose is added to compact envelopes.
Goal-inactivity reminders retain their existing full plan and completion guidance.
A long user hook remains intact on its due callbacks: no automatic paraphrasing.
Character-budget tests guard against command/message growth; they are not tokenizer
measurements and do not cap arbitrary user-hook content or filesystem path length.

## Validation and distribution

Public task producers run diagnostics after admission (including failed admission).
Healthy invocations add no output; configuration or runtime faults produce a versioned
`[ltc-status]` JSON envelope on stderr. `doctor` provides the same report explicitly.
Reports preserve the actual work state: persisted IDs must not be resubmitted,
and ambiguous writes require inspection before retry. The checker probes existing
queue/lock/runtime state and transient private file writes, never starts a service
or an Agent. Configuration commands are pinned to the current installation and
queue. Callback-only setup skips irrelevant local task-owner requirements; skill
preservation keeps local customizations during coordinator repair.

Checks follow the selected environment and the existing lifecycle contracts,
not a fixed list of required software. Each task is observed through its saved
execution backend. Idle queues need no task owner; completed results take precedence
over process liveness. Unknown owners, lost owners, missing result handoff and
unresolved delivery failures are distinct observations. ACKed/canceled failures
are historical evidence, not current delivery faults. Observations never relaunch
business commands or modify task, callback or lease records.

The report separates support, configuration and runtime readiness. Windows gets
Task Scheduler selection and repair; macOS gets launchd/screen selection and repair.
A null repair command means the Agent must inspect lifecycle evidence or
platform support before choosing an action. A live owner does not prove workload
progress; local readiness does not prove authentication or end-to-end delivery.

Run `PYTHONPATH=src python -m unittest discover -s tests -q`. Socket and process
integration tests require an ordinary local process environment. Set
`CODEX_LONG_TASK_WAKEUP_TARGET_LOCK_DIR` to a test-only directory. Native systemd
integration is opt-in (see tests/test_systemd_integration.py); use only temporary
queues and units. Never restart the production daemon for a test.

Required portability evidence: terminal closure, coordinator crash/service restart,
launch/result persistence failures, unknown owner, original-session callback,
legacy records, duplicate deliveries, reboot recovery and upgrade while tasks run.
Tests written by an independent author are executed and assessed by the maintainer.

Python remains the core. npm can later distribute per-platform bundled executables;
shipping on npm is not proof of platform support. Worker entry points support future
frozen packaging, but no frozen/npm distribution has been tested in this preview.
Keep one coordinator per queue, stable runtime paths, and old worker files until
live tasks drain. User credentials and callback hooks never belong in the repository.
