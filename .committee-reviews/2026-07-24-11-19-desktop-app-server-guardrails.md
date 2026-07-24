# Committee Review: Desktop App Server Delivery Guardrails

**Date**: 2026-07-24
**Context**: Code and architecture review
**Outcome**: ✅ Approved

## Expert Panel

| Expert | Role | Vote |
|---|---|---|
| Elena Markovic | Local IPC Security Engineer | Approve |
| Chen Wei | Distributed Systems Reliability Engineer | Approve |
| Priya Nair | Developer Experience and Test Architect | Approve |

## Executive Summary

The review covered a direct Codex Desktop callback transport over the local App Server, preserving the durable queue, acknowledgement, target-lease, retry, and safe-reload contract.

The panel initially found duplicate-delivery risk after an ambiguous `turn/start`, missing queue write access, early lease release after ACK, insufficient IPC validation, and incomplete tests. The final implementation treats explicit RPC rejection as safe CLI fallback, treats transport ambiguity as manual recovery without replay, grants the queue directory as a writable per-turn root, and holds the session lease through matching completion.

The final design validates local WebSocket framing and the App Server's schema-shaped messages, double-gates the test/debug socket override, documents the same-UID trust boundary, and retains a durable failed-state lease after unknown Desktop outcomes until ACK or explicit cancel. All 67 unit tests pass.

## Post-Review Field Validation

The review's initial JSON-RPC 2.0 framing assumption was corrected after a real Desktop smoke: the supported local App Server omits a `jsonrpc` member. Version `0.4.5` therefore validates the observed schema instead: requests and responses carry an `id`; notifications carry a string `method`; each message must have one of those structural forms. WebSocket and same-UID validation remain strict.

At `2026-07-24T11:44:01+08:00`, a `sleep 30` task delivered its callback directly into the bound Desktop task, where it was acknowledged. A subsequent test-only same-session callback was correctly held behind the active target lease and then explicitly canceled as redundant. This field validation supersedes any wording in the original transcript that requires a `jsonrpc: "2.0"` member.

## Post-Repair Re-Review

After the field validation, the panel found and closed four additional release blockers: retained unknown-outcome leases were not global across queues; late ACK and cancellation could leak a retained marker; crash recovery could leave an acknowledged marker behind; and a stale queue selection could cross the gap before worker launch. The final design persists a per-target global recovery marker while the target flock is held, serializes marker writes/reconciliation/removal with a dedicated per-target transaction lock, reconciles markers against ACK/cancellation tombstones, and rechecks the marker after acquiring the target flock before launching a worker.

Elena Markovic, Chen Wei, and Priya Nair independently approved the final revision. `PYTHONPATH=src python3 -m unittest -q tests.test_cli` passed all **75** tests, including cross-queue, ACK/cancel, crash-recovery, transaction-lock, and stale-selection regressions.

## Critical Issues Addressed

- Ambiguous direct submission no longer immediately falls back or auto-retries.
- Explicit App Server rejection falls back once to CLI resume.
- ACK no longer releases a Desktop turn’s target lease before completion.
- Unknown Desktop outcomes retain a cancellable durable target lease.
- Queue write access, protocol validation, socket-override scope, and Chinese documentation are covered.

## Recommendations

- Keep `CODEX_LONG_TASK_WAKEUP_APP_SERVER_SOCKET` behind its explicit test/debug gate.
- Treat shared Unix accounts as unsupported for direct App Server delivery.
- Keep the manual recovery record visible in `status` and cancel it only after inspecting the Desktop task.

## Full Discussion Transcript

The complete two-round review, modification review, final votes, and vote tally are preserved in `.committee-reviews/2026-07-24-11-19-desktop-app-server-guardrails-discussion-context.md`.
