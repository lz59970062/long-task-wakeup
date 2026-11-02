# Callback modes — 0.7.1 preview

Task execution and callback delivery are separate capabilities. Native task
owners still run commands independently on Linux, macOS and Windows. Returning
to a conversation also requires a connection to its current owner. A session ID,
an installed CLI, or an idle Agent turn does not prove that connection exists.

## Supported routes and limits

| Selection | Behavior | Acceptance requirement |
| --- | --- | --- |
| `--callback-mode auto` (default) | Recognizes the current `Codex Desktop` origin marker and selects `codex-queue`; otherwise selects `cli` | If an integration omits the marker, select the mode explicitly. Historical session `source` is not current ownership. |
| `--callback-mode cli` | Uses the existing local App Server when available, otherwise the bound Agent's CLI resume | Real original-session receipt/ACK must be verified for that CLI configuration; an unrelated Core may still own the session. |
| `--callback-mode codex-queue` | Runs the public `codex queue --thread UUID --message TEXT` once with the session's environment; never falls back to another writer | Queued is not received: the open chat must consume it and ACK. |
| `--callback-mode desktop` | Retired for new work; refused with this replacement. Saved records keep the route. | See below. |
| `--callback-mode manual` | Runs the workload and saves a callback for later inspection; never automatically starts an Agent | User or original Agent reads results and ACKs receipt. A file by itself does not wake the Agent. |

PI Agent callbacks always use CLI resume: PI has no shared-Core App Server, so
`auto` never selects `codex-queue` for `--agent pi`, delivery runs
`pi --print --session <session-file>`, and an explicit `codex-queue` mode is rejected
before admission.

For a new `run` or `agent`, a blocked route preflight refuses admission before
creating the task. The user can repair the route or explicitly choose `manual`.
`done` reports work that already happened, so it preserves the callback even if
the connection is currently unavailable.

Mode, Codex executable and profile are frozen with the task. The session's
environment is saved privately per callback and removed once nothing can send
again. CLI mode remains independent. Legacy records retain their prior routing.

## Native queue delivery

The receipt in `codex-queue/<id>.json` is written before the command starts, so a
callback is never sent twice automatically. After the queue accepts a message
the target lease is released: Codex owns the message and its order, so one
unanswered callback does not block later callbacks for that thread. The callback
stays pending until the chat ACKs, and `ltc status` says so.

| Outcome | Classification | Next step |
| --- | --- | --- |
| Exit 0 naming this thread | Queued; pending until ACK | Nothing is sent again |
| Exit 1 to 127, or the command cannot be started | Rejected; nothing was queued | `ltc retry --id ID` after repair |
| Kill, timeout, exit 128 or above, exit 0 with unreadable output | Unknown; lease kept, later callbacks wait | Inspect the chat, then `ltc ack` or `ltc cancel` |

A delivery worker that vanishes is judged from the receipt alone: no receipt means
nothing was sent, `queued` keeps the acceptance, and anything else is unknown.

## Retired Desktop launcher

Setup no longer generates a launcher, `ltc desktop` only explains the replacement,
and `--callback-mode desktop` is refused for new work, because a normally started
Desktop already gets callbacks through the queue. `setup --desktop-app`,
`--desktop-launch-mode` and `--no-desktop-launcher` are accepted and ignored so
install scripts keep working, and setup names leftover launcher files without
deleting them.

Saved `desktop` records and tasks keep their results and route. A connection that
still works delivers them as before; otherwise they wait in `failed/` naming
`ltc retry --id ID --callback-mode codex-queue`, which does not rerun the workload.

## Readiness and recovery

`ltc doctor` reports `checks.callback` and a separate `callback` object:

- `owner_reachable`: a read-only query observed the bound thread in this Core.
- `unverified`: CLI delivery may be available, but no original-session delivery
  was proved. An absent loaded-thread entry does not mean the thread is free.
- `available`: the selected Codex has `codex queue`; receipt is still unproved.
- `blocked`: the route's executable, thread or connection was not verified.
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
ltc retry --id <callback-id> --callback-mode codex-queue
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
**2**; the native queue uses task version **6** and callback version **5**. Upgrade
the coordinator before submitting them. Older coordinators reject these versions
instead of ignoring manual intent or resuming a thread an open chat owns, and keep
the record with its results; `ltc retry` delivers it after the upgrade. Old task and
callback versions remain readable. Do not downgrade a queue containing new work.

Acceptance is by OS **and connection**, not OS alone: CLI, shared Desktop,
unconfigured Desktop, restart, owner change, lost response, and duplicate receipt.
The 2026-09-26 Mac success `9e4dabc9` occurred in a `source=vscode` conversation.
On 2026-09-27 Desktop resumed that same conversation into its separate stdio Core;
the 60.014-second workload `bdfd671d` completed, but its callback hit an active
writer conflict. Those two observations are separate evidence, not interchangeable
proof of Mac Desktop acceptance.

The earlier launcher notes remain as history: [Mac](macos-desktop-bridge.md),
[Windows](windows-desktop-bridge.md). See the
[queue validation record](validation-codex-queue.md) for what has been run.
