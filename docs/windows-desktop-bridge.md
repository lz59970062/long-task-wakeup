# Experimental Windows Desktop callback bridge

This opt-in adapter is intended to let Codex Desktop and LTC use **one App
Server**, so callback delivery reaches the process that already owns the thread.
It is not enabled by ordinary Windows setup. The existing Desktop conversation's
end-to-end callback acceptance test remains pending. The installed Windows Store
Desktop still fails the launcher's default direct process creation with Win32
error 5 (access denied). The explicit experimental `-PackageContext` option is
based on a successful suspended process-creation probe under package identity;
actual GUI execution, App Tools and original-session receipt/ACK through that
option remain unverified.

## Findings and sources

On this machine, Codex Desktop 26.915.4065 starts its Core with the default stdio
transport. Its process had no TCP listener, its default control socket did not
exist, and read-only `app-server daemon version` / `app-server proxy` probes failed
outside the Agent sandbox. The Windows AF_UNIX driver was running; those failures
are not proof that every Windows Unix-socket implementation is unavailable.

The installed Desktop code accepts a `CODEX_CLI_PATH` executable override and
`CODEX_APP_SERVER_FORCE_CLI=1`. These were verified in this installed build's
implementation, **not documented as stable public Desktop configuration APIs**.
An already connected Desktop does not adopt environment changes in another shell.
Its direct WebSocket override has no local Bearer-header setting, and starting a
server before Desktop loses the fresh App Tools pipe environment supplied by
Desktop. The launcher therefore uses a Desktop-owned stdio adapter. Desktop
starts that adapter with its original Core flags and fresh environment, which
the adapter passes to the real Core while changing only its transport.

The official [App Server protocol](https://learn.chatgpt.com/docs/app-server)
documents multiple client transports, initialization, `thread/resume`,
`turn/start`, and authentication before JSON-RPC initialization. It labels
WebSocket transport experimental and unsupported for production workloads.
The [CLI changelog](https://learn.chatgpt.com/docs/changelog) describes a newer
read-only transcript/retry behavior for conversations already open elsewhere;
that is not evidence that upgrading removes the single-writer requirement.

An isolated real Codex 0.153.4 server was tested with a disposable empty profile:
an anonymous WebSocket handshake returned **401**, a capability-token handshake
returned **101**, and initialization plus `thread/loaded/list` succeeded. No model
turn or existing conversation was used in this probe.

A second real-server integration test connected **two independent clients** to
the gateway. The first created a disposable test thread and persisted a fixture
with the documented `thread/inject_items` operation (no model generation); the
second resumed the same thread while the first remained connected. It passed,
and drained stop removed the bridge metadata and private token. The test uses a
separate empty profile, not the current Desktop session. Reproduce with:

```powershell
$env:LTC_TEST_CODEX_BRIDGE = '1'
& .\.venv\Scripts\python.exe -m unittest discover -s tests -p test_desktop_bridge_integration.py -v
```

The test requires an installed Codex CLI exposing those protocol methods and
ordinary user socket/process access. Its empty profile needs no credentials.
It also passed with this Desktop's exact Core version, `0.155.0-alpha.9.2`, using
the executable cache copy whose SHA-256 matches the installed package. Set
`LTC_TEST_CODEX_BRIDGE_BIN` to select that executable for the integration test.
With that native executable selected, a second test uses Desktop's stdio
protocol through the adapter and LTC's separate WebSocket connection. It verifies
that both access one owned thread and that closing stdin ends both connections.
Set `LTC_TEST_DESKTOP_CORE_WRAPPER` to the absolute installed
`ltc-desktop-core.exe` path to exercise the installed console launcher too.

## Connection and trust model

```mermaid
flowchart LR
    Desktop[Codex Desktop] -->|stdio and original tool environment| Adapter[Desktop-owned adapter]
    Adapter --> Gate[Same-user Windows loopback gateway]
    LTC[LTC delivery worker] --> Gate
    Gate -->|Private Bearer token| Server[One shared Codex App Server]
    Server --> Thread[Original thread owner]
```

The gateway authenticates the Windows process on every connected TCP socket by
its reverse connection tuple, PID, SID and creation identity. Only literal IPv4
loopback is accepted. Before sending its HTTP handshake, LTC also verifies that
the server matches the private bridge metadata's PID, identity and Codex profile.

The gateway rejects browser Origin headers and invalid/ambiguous upgrades before
opening an upstream connection. It verifies the upstream belongs to its launched
process tree before inserting the private token. The upstream listener itself
requires the token; it is not an anonymously accessible alternate entrance.
No token is stored in the Desktop URL, process arguments, metadata or ordinary
status output. Private files use the existing Windows ACL implementation.

This permits native processes belonging to the current Windows user; it does not
claim to distinguish trusted applications from other code running as that user.
Administrator/SYSTEM access remains outside this isolation boundary. OS peer
identity is checked on connection; an authenticated owner can delegate a socket.

The stdio adapter preserves JSON-RPC IDs, responses, notifications and
server-initiated tool/approval requests. It does not initialize on Desktop's
behalf. Diagnostics go to stderr, never the protocol stream. Desktop's stdin EOF
ends the adapter, gateway and owned Core even if LTC still has a connection.
The real Desktop tool/approval routing for a turn initiated by the second LTC
client remains part of the live acceptance test; byte forwarding alone does not
prove that routing. Read-only inspection of the packaged App Tools client found
the normal pipe environment path and no direct-parent PID requirement there.
The native service-side process ancestry rules could not be established from
that code, so only a normal App Tools call after restart can settle compatibility.

LTC does not guess ports or silently switch to CLI delivery after an explicitly
configured bridge fails. Before `turn/start`, it persists submission intent.
If the worker dies or a response is lost, the retained lease prevents another
callback attempt until the outcome is resolved. ACK or a confirmed completion
can clear that intent; a missing response cannot.

## Packaged Desktop startup evidence and limits

The user tried the launcher on Windows and it failed before Desktop or a bridge
was started. Native `CreateProcessW` probes reproduced error 5 for this package's
`app\ChatGPT.exe` and `app\Codex.exe`; the matching cached Core could be created
suspended and immediately cleaned up. The probes never resumed GUI code. Tool
and scheduled-task probes still ran inside Windows Jobs, so their error code did
not establish that every external launch context fails for the same reason.

A subsequent standard-library Python probe ran under the installed Codex
package's identity and successfully created `app\ChatGPT.exe` suspended: native
error **0**, `probe_resumed: false`, and `cleanup_complete: true`. The probe still
had an outer Windows Job. It used `Invoke-CommandInDesktopPackage` without
`-PreventBreakaway`; the change in package context was sufficient for this
process-creation test. It did not run Desktop, the wrapper or any App Tools call.
The completed launcher preflight passed this same check in PowerShell 5.1 and 7.
It also queried the suspended GUI child's own package identity and verified an
exact match, which the package helper now requires before actual startup.

The old `-CheckOnly` checked paths and wrapper versions but did **not** test GUI
process creation. It could therefore report a successful preflight before this
failure. It now tries native creation with the initial thread suspended, immediately
terminates and waits for only that newly created process, and reports the native
error plus `preflight_passed: false` with exit code 1 when creation fails. It does
not run GUI code, stop existing Desktop instances, or create bridge state.
The separate `native_creation_passed` field isolates this capability check;
`launch_blockers` also reports running/uninspectable Desktop processes and an
existing live or unreadable bridge. Those conditions also fail the full preflight.

The launcher now offers **explicit experimental `-PackageContext`**, based on
that narrower result. It runs a small standard-library helper using the base
Python installation's `pythonw.exe` under the selected package identity. That
helper builds the GUI child's environment and invokes its executable directly.
The option does not set persistent user/global environment variables, alter
package debug policy, modify package files, or use `-PreventBreakaway` to force
the whole descendant tree to retain package context. It is never an automatic
fallback from a failed direct launch.

Microsoft describes
[Invoke-CommandInDesktopPackage](https://learn.microsoft.com/en-us/powershell/module/appx/invoke-commandindesktoppackage?view=windowsserver2025-ps)
as a debugging tool: the helper receives package identity and access to virtualized
resources, but its token is **not identical** to a normally activated app's token.
Privacy controls, app settings and other behavior are not guaranteed. A successful
native creation check therefore does not establish a supported replacement for
normal Desktop activation. Actual GUI startup, override inheritance and App Tools
compatibility must still be tested before this route can be relied on.

The installed package has no GUI execution alias. Ordinary
[application activation](https://learn.microsoft.com/en-us/windows/win32/api/shobjidl_core/nf-shobjidl_core-iapplicationactivationmanager-activateapplication)
does not expose an environment argument; merely opening a window does not prove
that the Core override arrived. Do not repeatedly restart Desktop or elevate the
shell as a presumed fix for the default route's error 5.

## Explicit opt-in and acceptance checks

Changing the App Server requires closing **all** Codex Desktop windows/processes.
Save or finish active work first. The supplied launcher refuses to stop a running
Desktop and does not rewrite installed app files or global environment settings.
The default route requires a Desktop executable that permits direct startup;
`-PackageContext` explicitly selects the experimental route described above.
Both pass overrides only in the new GUI process's environment. Desktop
then starts `ltc-desktop-core.exe`, which owns the gateway and real Core. The
script does not change the caller's environment or prestart a separate server.

From this checkout, first install the updated package so its new console entry
point exists in `.venv`:

```powershell
& .\.venv\Scripts\python.exe -m pip install -e .
# This never resumes the probe's GUI thread; Desktop may remain open for this check:
& .\examples\windows\start-desktop-bridge.ps1 -PackageContext -CheckOnly
# native_creation_passed may be true while Desktop is still a launch_blocker.
# Save work and close Desktop yourself, then repeat the complete preflight:
& .\examples\windows\start-desktop-bridge.ps1 -PackageContext -CheckOnly
# Start only when preflight_passed is true and launch_blockers is empty:
& .\examples\windows\start-desktop-bridge.ps1 -PackageContext
```

Omit `-PackageContext` for the default direct route; on the tested Store package,
that route's `-CheckOnly` still reports native error 5 and exits nonzero. Neither
successful preflight nor a launch request confirms a working Desktop bridge.
After a package-context launch request, the script waits for live bridge metadata
for this profile and at least one connected client. It reports failure if that
state is absent; a successful observation still requires the App Tools and
original-session callback checks below.

Other launcher arguments are `-Python`, `-CodexHome`, `-CodexBin`,
`-DesktopExe` and `-CoreWrapper`. Use the actual existing Codex profile; do not copy authentication
files into a test profile. If the launcher fails, inspect the private bridge log
and do not start a second unverified server against the same conversation.
The launcher discovers the GUI through its package manifest, checks both its
launcher and resident process, and selects a runnable Core cache copy only when
its SHA-256 matches the installed Desktop bundle. It does not default to an older
global npm CLI. It verifies that the adapter's `--version` passthrough returns
the selected Core's version. GUI execution through the selected route and
inheritance of the temporary CLI override remain part of explicit restart
validation. A successful suspended probe is only a process-creation check, not
bridge or callback acceptance.

The metadata is `<CODEX_HOME>\long-task-wakeup\desktop-bridge.json` by default.
After reopening the original conversation, configure the existing LTC coordinator
with that explicit metadata file, preserving its original name and queue:

```powershell
$ltcProfile = if ($env:CODEX_HOME) { $env:CODEX_HOME } else { Join-Path $env:USERPROFILE '.codex' }
$env:CODEX_LONG_TASK_WAKEUP_DESKTOP_BRIDGE_FILE = Join-Path $ltcProfile 'long-task-wakeup\desktop-bridge.json'
& .\.venv\Scripts\ltc.exe setup --service windows-task --keep-skill --force --now
```

For a custom deployment, include its existing `--name` and `--queue-dir`. The
Windows coordinator pins the bridge metadata path in its private configuration.
Each delivery reloads that metadata and rechecks the connected peer.

First verify that Desktop actually connected to the bridge and opens the exact
original thread, and that a harmless Desktop App Tools operation works. Then
validate one completion callback, inspect its artifacts using the resumed
conversation's tools and ACK from that conversation. A protocol probe or a test Agent's ACK is not that
acceptance test. Keep task `1aa2ba13` and its failed callback as prior evidence;
recover its callback only after checking that no previous delivery arrived. Do not
rerun its already completed business command or manually manufacture an ACK.

## Inspect and stop

```powershell
& .\.venv\Scripts\python.exe -m long_task_callback.desktop_bridge --status
# Only if a standalone gateway remains after its clients close:
& .\.venv\Scripts\python.exe -m long_task_callback.desktop_bridge --stop
```

Use the same `--codex-home` and `--metadata-file` if customized. Normally closing
Desktop also ends its adapter and removes the metadata; `--stop` is for a
remaining standalone gateway with no clients. Stop is bound to the recorded
live process and refuses active clients. The bridge does not install an automatic
login task or restart itself. Its Job owns the App Server process tree; exiting
the bridge ends that tree. Normal shutdown verifies Job membership through native
process handles and waits for its children to exit before releasing private state.
The kill-on-close Job also contains abnormal exits. Independently scheduled LTC
business tasks keep their separate ownership.

To return to ordinary Desktop startup, close Desktop and any remaining bridge, remove
`CODEX_LONG_TASK_WAKEUP_DESKTOP_BRIDGE_FILE` from the shell used for LTC setup and
update the same coordinator. Start Desktop normally. CLI delivery again has the
original active-writer limitation while Desktop owns the thread.
