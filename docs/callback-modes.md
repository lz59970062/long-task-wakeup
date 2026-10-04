# Callback modes — 0.7.1 preview

Task execution and callback delivery are separate capabilities. Native task
owners still run commands independently on Linux, macOS and Windows. Returning
to a conversation also requires a connection to its current owner. A session ID,
an installed CLI, or an idle Agent turn does not prove that connection exists.

## Supported routes and limits

| Selection | Behavior | Acceptance requirement |
| --- | --- | --- |
| `--callback-mode auto` (default) | Recognizes the current `Codex Desktop` origin marker and selects `desktop`; otherwise selects `cli` | If an integration omits the marker, select the mode explicitly. Historical session `source` is not current ownership. |
| `--callback-mode cli` | Uses the existing local App Server when available, otherwise the bound Agent's CLI resume | Real original-session receipt/ACK must be verified for that CLI configuration; an unrelated Core may still own the session. |
| `--callback-mode desktop` | Requires an explicit shared Core; never falls back to an independent CLI writer | Desktop and LTC must access the same original conversation and its normal tools/approvals. |
| `--callback-mode manual` | Runs the workload and saves a callback for later inspection; never automatically starts an Agent | User or original Agent reads results and ACKs receipt. A file by itself does not wake the Agent. |

PI Agent callbacks always use CLI resume: PI has no shared-Core App Server, so
`auto` never selects `desktop` for `--agent pi`, delivery runs
`pi --print --session <session-file>`, and an explicit `desktop` mode is rejected
before admission.

For a new `run` or `agent`, a blocked Desktop preflight refuses admission before
creating the task. The user can configure the bridge or explicitly choose
`manual`. `done` reports work that already happened, so it preserves the callback
even if the connection is currently unavailable. Do not silently choose manual
or force CLI mode to conceal a Desktop failure.

Mode and bridge metadata path are frozen with the task. Each delivery reloads
the endpoint metadata and checks the live peer. PID reuse, another profile, and
an unavailable metadata file cannot authorize sending a prompt elsewhere.
CLI mode remains independent of a Desktop bridge selected in the coordinator's
environment. Legacy records retain their prior routing behavior.

## Desktop launcher installation

On Mac and Windows, standard `ltc setup` generates a launcher for the current
profile and installed Python runtime. Mac generates `Start LTC Desktop.command`;
Windows generates `Start LTC Desktop.cmd` and `Start LTC Desktop.ps1`, with local
settings in `desktop.json`. The implementation and script resources ship in the
wheel; neither platform requires a source checkout or an `examples` script.
Use `--no-desktop-launcher` to skip this setup step on either platform.

The shared commands are `ltc desktop prepare`, `ltc desktop launch` and
`ltc desktop status`. The Desktop step of setup and `desktop prepare` only
generate configuration and launchers; they do not start or stop Desktop.
Activation remains an explicit launch after
closing the existing Desktop. On Windows, direct launch is the default;
`ltc setup --desktop-launch-mode package-context` or
`ltc desktop prepare --launch-mode package-context --force` saves the explicit
experimental alternative. A failed direct launch does not select it automatically.
`ltc desktop launch --check-only` retains the Windows suspended process-creation
probe, whose success alone does not prove bridge or callback delivery.

## Readiness and recovery

`ltc doctor` reports `checks.callback` and a separate `callback` object:

- `owner_reachable`: a read-only query observed the bound thread in this Core.
- `unverified`: CLI delivery may be available, but no original-session delivery
  was proved. An absent loaded-thread entry does not mean the thread is free.
- `blocked`: Desktop connection or ownership was not verified.
- `manual`: automatic delivery was explicitly disabled.

The probe only initializes a client and reads `thread/loaded/list`; it never
resumes a thread or starts a model turn. `end_to_end_verified` remains false:
only actual receipt and ACK establish a successful callback. A tool sandbox may
deny socket/process access; retry the check through an authorized ordinary-user
execution context rather than weakening peer checks.

A definite App Server writer conflict or explicit bridge failure is retained in
`failed/` with `delivery_state: blocked`. It does not spend retries attempting a
second writer. Other transient failures retain the existing retry policy.
Unknown submission outcomes retain their cross-queue lease and cannot be retried
through the ordinary recovery command.
When the owning Core reports an active turn, delivery waits in the pending queue
without consuming its retry budget; it does not interrupt that turn.

After inspecting and repairing the connection, requeue the same callback:

```bash
ltc retry --id <callback-id> --callback-mode desktop
# Or explicitly put a failed result into the manual inbox:
ltc retry --id <callback-id> --callback-mode manual
ltc status --state pending --state failed
```

`retry` preserves the ID, original session, prompt and workload artifacts. It
records previous attempts/errors and never executes the business command again.
Acknowledged, canceled, actively delivering and ambiguous callbacks are refused.
Manual receipt uses the emitted ACK after inspecting the result in its bound
session. This confirms receipt, not autonomous transport acceptance or goal completion.

## Upgrade and acceptance

New tasks use record version **3**, and callbacks with explicit modes use version
**2**. Upgrade the coordinator before submitting them. Older coordinators reject
these versions instead of silently ignoring manual/desktop intent. Old task and
callback versions remain readable. Do not downgrade a queue containing new work.

Acceptance is by OS **and connection**, not OS alone: CLI, shared Desktop,
unconfigured Desktop, restart, owner change, lost response, and duplicate receipt.
The 2026-09-26 Mac success `9e4dabc9` occurred in a `source=vscode` conversation.
On 2026-09-27 Desktop resumed that same conversation into its separate stdio Core;
the 60.014-second workload `bdfd671d` completed, but its callback hit an active
writer conflict. Those two observations are separate evidence, not interchangeable
proof of Mac Desktop acceptance.

See [Mac Desktop setup](macos-desktop-bridge.md) and
[Windows Desktop setup](windows-desktop-bridge.md). Automatic launcher generation
is part of the `codex/macos-desktop-callback` preview. The Windows installation path still
needs native acceptance; historical package-context callback success does not
establish that the new setup/packaging flow works on Windows.
