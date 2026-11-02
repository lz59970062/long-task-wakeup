# Native Codex queue validation — 2026-11-02

Validation of the `codex-queue` route in `0.7.1a3` on Linux with Codex CLI 0.166.0
(an npm install whose `codex` is a Node script), Python 3.12 and 3.9. Every run used
a temporary `HOME`, `CODEX_HOME` and `LTC_HOME` under a directory outside `/tmp`,
with `XDG_RUNTIME_DIR`, `DBUS_SESSION_BUS_ADDRESS`, `CODEX_THREAD_ID` and the host's
`CODEX_HOME` unset. No LTC was installed, no service manager was used, and no remote
model was called.

## Automated suites

| Check | Result |
| --- | --- |
| Baseline before the change, Python 3.12 | 557 tests, OK, 95 skipped |
| `python -m unittest discover -s tests`, Python 3.12 | 635 tests, OK, 110 skipped |
| Same, Python 3.9.25 in a dedicated environment | 635 tests, OK, 110 skipped |
| New tests | 63 unit tests in `tests/test_codex_queue.py`; 15 opt-in runs in `tests/test_codex_queue_integration.py`, which account for the extra skips |
| `scripts/docs_sync.py check`, `mkdocs build --strict` | Passed |
| Wheel built from the tree | `0.7.1a3`, contains `codex_queue.py` |

## End-to-end runs

`tests/test_codex_queue_integration.py` drives the real Codex TUI in a
pseudo-terminal as the open chat, a real foreground `ltc daemon`, real `codex queue`,
and a local stub Responses API. The stub answers a callback by asking Codex to run
the callback's own ACK command through `exec_command`, so the ACK passes through the
thread's real `workspace-write` sandbox.

```bash
LTC_TEST_NATIVE_CODEX_BIN="$(command -v codex)" LTC_TEST_E2E_ROOT=/path/outside/tmp \
LTC_TEST_OLD_SOURCE=/path/to/old/src PYTHONPATH=src \
python -m unittest discover -s tests -p test_codex_queue_integration.py -v
```

All 15 passed on Python 3.12 in 181 seconds. Seven of them (idle, busy, closed, rejected,
both stripped-PATH runs, no ACK) also passed on Python 3.9.

| Scenario | Observed |
| --- | --- |
| Idle chat | Delivered once, ACKed from the sandbox with the queue in its writable roots, moved to `done` |
| Busy chat | Accepted while a turn was held open; no model request carried it until the turn ended; `ltc status` showed the wait; delivered once |
| Closed, then reopened, shared app-server stopped | Accepted; no model request until reopening; delivered once |
| Closed, then reopened, shared app-server alive | Accepted; still not delivered 3 seconds later; delivered once on reopening |
| Rejected, then retried | Real `no rollout found` from a profile that lacked the thread; `failed` with the cause and no lease; repaired profile and `ltc retry` delivered once |
| Coordinator `PATH=/nonexistent` | Delivered once using the submitting session's environment |
| Same, snapshot removed | `env: node` exit 127 classified as rejected; `ltc retry` from the session delivered once |
| No ACK, later callback | Second callback delivered while the first stayed pending; `ltc status` listed both; no lease held; first not resent |
| Sandbox cannot write the queue | Real sandbox refused the ACK; the chat saw LTC's explanation, not a traceback; stayed pending, not resent; a terminal ACK finished it |
| Killed after the queue accepted | Unknown; retry refused; chat received it exactly once and its ACK settled the record |
| Exit 0 with unreadable output | Same |
| Killed sender, later callback | Lease held and later callback unsent until `ltc cancel`, then delivered |
| Managed `ltc run` on the screen backend, coordinator without `node` | Workload ran; task v6, callback v5; delivered once |
| Saved `desktop` record | Held in `failed` with exit code and log path intact, naming the repair; `ltc retry --callback-mode codex-queue` delivered once |
| Coordinator from the previous commit (`8e29b99`) | v5 callback moved to `failed` unsent, v6 task left unchanged, nothing queued; new coordinator's `ltc retry` delivered once |

## Not run

- A real Codex Desktop app. The origin marker was set by hand, and the open chat was the TUI.
- macOS and Windows. Only mocked unit tests cover the Windows executable-resolution
  path and the macOS bundled-Codex lookup.
- The launcher, bridge and Core code removed after this record. Its removal and the block
  on saved `desktop` records on macOS and Windows have only been tested on Linux with a
  simulated platform, so the removed paths were not run on either system.
- Delivery of a saved `desktop` record through a working socket on Linux. Here the
  connection was absent, so the record took the blocked path.
- Codex versions other than 0.166.0.

The output match on `Queued message ID for thread UUID.` is specific to the Codex CLI's
wording. If a later version changes it, deliveries become unknown outcomes rather than
duplicates, and later callbacks for that thread wait until the record is resolved.
