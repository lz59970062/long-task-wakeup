# Committee Discussion: Desktop App Server Delivery Guardrails

**Date**: 2026-07-24 11:19 Asia/Shanghai
**Scope**: Revised direct Codex Desktop App Server callback delivery, including IPC guardrails, ACK write access, turn completion leases, and fallback behavior.
**Project root**: `/data3/lizhi_2024/program/long-task-wakeup`
**User constraints**: Existing live callbacks must not be interrupted by upgrade; prevent duplicate delivery and make fallback defensive.

## Expert Panel

| Expert | Role | Personality | Primary Lens | Signature Approach | Status |
|---|---|---|---|---|---|
| Elena Markovic | Local IPC Security Engineer | Security-paranoid | Unix socket and WebSocket trust boundaries | Finds spoofing, framing, and protocol-validation gaps | Round 1 complete; agent `019f9217-c87f-7ca1-ab46-1d13e1e69534` |
| Chen Wei | Distributed Systems Reliability Engineer | Detail-obsessed | Delivery, ACK, retry, and live-upgrade correctness | Traces every state transition and failure race | Round 1 complete; agent `019f9217-cc4e-7600-a69a-15f483a27804` |
| Priya Nair | Developer Experience and Test Architect | Pragmatic minimalist | Compatibility, fallback, documentation, and test coverage | Prefers simple contracts proven by focused tests | Round 1 complete; agent `019f9217-c5fe-7f01-92c7-835911670c2e` |

## Round 1: Independent Reviews

## Elena Markovic — Initial Review

### Key Findings
1. **P1 — Same-UID socket impersonation remains possible.** `SO_PEERCRED` only verifies UID; any process under that UID can replace/serve the socket, read callback prompts, and spoof JSON-RPC success. There is no protected control-plane credential or App Server PID/ownership verification. `src/long_task_callback/cli.py:798`
2. **P1 — Ambiguous `turn/start` failure can cause duplicate delivery.** If App Server accepts `turn/start` but the response is lost, the client returns `False` and immediately falls back to `codex exec resume`, potentially injecting the same callback twice. `src/long_task_callback/cli.py:963`
3. **P2 — WebSocket upgrade validation is too permissive.** The status check accepts any string starting with `HTTP/1.1 101` and does not require `Upgrade: websocket` or `Connection: Upgrade`; a malformed peer can pass with a calculated accept hash. `src/long_task_callback/cli.py:823`
4. **P2 — Direct delivery does not carry the CLI path’s sandbox write configuration.** The Desktop turn receives only `cwd` and prompt, so acknowledgement may fail when the existing task lacks permission to write the queue marker; retry semantics therefore differ from the documented CLI path. `src/long_task_callback/cli.py:963`
5. **P2 — Tests cover only the trusted happy path.** No regression tests cover malformed upgrade headers, peer-credential failure, masked/fragmented server frames, `turn/start` response loss, or worker-level ACK/fallback behavior. `tests/test_cli.py:1625`

### Questions for Committee
1. Is same-UID local-process access an accepted trust boundary? If not, how will the App Server prove identity beyond UID?
2. For accepted-but-unconfirmed `turn/start`, should the system prefer duplicate avoidance (retry later) over immediate CLI fallback?
3. What capability guarantees that the Desktop thread can write the ACK marker?

### Initial Position
**Reject.** Resolve ambiguous delivery and define/authenticate the local IPC trust boundary before merge; then add worker-level failure-path tests.

## Chen Wei — Initial Review

### Key Findings
1. **P1 — `turn/start` outcome is ambiguous but immediately falls back to CLI.** If App Server accepts `turn/start` then disconnects before response, `start_desktop_app_server_turn()` returns `False`, causing a second `codex exec resume` for the same callback. `/data3/lizhi_2024/program/long-task-wakeup/src/long_task_callback/cli.py:963`, `/data3/lizhi_2024/program/long-task-wakeup/src/long_task_callback/cli.py:971`, `/data3/lizhi_2024/program/long-task-wakeup/src/long_task_callback/cli.py:1016`
2. **P1 — ACK releases the session lease while the Desktop turn may still run.** Direct delivery exits immediately after ACK, unlike the CLI path which retains the worker while Codex runs; a new callback can overlap the still-active Desktop turn. `/data3/lizhi_2024/program/long-task-wakeup/src/long_task_callback/cli.py:1007`, `/data3/lizhi_2024/program/long-task-wakeup/src/long_task_callback/cli.py:1015`, `/data3/lizhi_2024/program/long-task-wakeup/src/long_task_callback/cli.py:1166`
3. **P1 — Direct turns do not propagate writable-queue sandbox configuration.** The CLI path explicitly sets `sandbox_workspace_write.writable_roots`; the App Server path only sends `cwd` and text, so the ACK command may lack write permission despite the stated guarantee. `/data3/lizhi_2024/program/long-task-wakeup/src/long_task_callback/cli.py:425`, `/data3/lizhi_2024/program/long-task-wakeup/src/long_task_callback/cli.py:436`, `/data3/lizhi_2024/program/long-task-wakeup/src/long_task_callback/cli.py:963`
4. **P2 — Safe hot reload tracks delivery workers, not active Desktop turns.** Once the direct worker exits after ACK, reload/restart gating sees no live delivery although the Desktop task may still be executing. `/data3/lizhi_2024/program/long-task-wakeup/src/long_task_callback/cli.py:694`, `/data3/lizhi_2024/program/long-task-wakeup/src/long_task_callback/cli.py:1803`, `/data3/lizhi_2024/program/long-task-wakeup/src/long_task_callback/cli.py:2444`
5. **P2 — Tests cover only successful RPC sequencing.** There is no worker-level coverage for ACK/lease release, accepted-but-unconfirmed delivery, App Server timeout, CLI fallback, or hot-upgrade draining. `/data3/lizhi_2024/program/long-task-wakeup/tests/test_cli.py:1625`

### Questions for Committee
1. Does App Server support a callback idempotency key or a queryable `turnId` for `turn/start`?
2. Should ACK mean “callback received” only, or should it retain the target lease until Desktop reports turn completion?
3. Can App Server accept per-turn sandbox/write-root settings equivalent to the CLI resume configuration?

### Initial Position
**Approve with conditions** — resolve ambiguous-submit duplicate delivery, preserve lease semantics through Desktop turn completion (or document and enforce a new contract), and add worker-level lifecycle/fallback/hot-reload tests before merge.

## Priya Nair — Initial Review

### Key Findings
- **P1 — 不确定提交会双重投递。** `turn/start` 已被服务端接收但响应丢失时，代码立即退回 CLI，可能同时创建 Desktop turn 和 `codex exec resume`。应将“未发出 / 已接受 / 结果未知”分开处理。`src/long_task_callback/cli.py:963`、`src/long_task_callback/cli.py:971`、`src/long_task_callback/cli.py:1007`
- **P1 — 直连路径未保持原有会话 lease。** 直连 worker 在 ACK 后立即退出并释放锁，但 Desktop turn 仍可能执行；同一 session 的后续回调因此可能重叠。文档“锁语义不变”的说法不成立。`src/long_task_callback/cli.py:1007`、`src/long_task_callback/cli.py:1042`、`README.md:348`
- **P1 — ACK 可写性未被保证。** CLI resume 会注入队列目录的写权限；App Server `turn/start` 仅传入 `cwd` 和文本，现有 Desktop task 未必能写入 ACK 标记。`src/long_task_callback/cli.py:963`、`src/long_task_callback/cli.py:1127`
- **P2 — 配置开关不可控。** 安装和守护进程环境无条件启用 Desktop 直连，却没有受支持、已文档化的禁用方式；出现兼容问题时只能修改服务配置。`src/long_task_callback/cli.py:201`、`src/long_task_callback/cli.py:2241`
- **P2 — 测试只覆盖理想协议。** 当前测试只验证成功的四次 RPC，未覆盖 App Server 拒绝、`turn/start` 响应丢失、ACK 超时、CLI fallback，以及 worker/lease 生命周期。`tests/test_cli.py:1625`

### Questions
- App Server 是否提供稳定的 turn-completed 事件或查询接口，以便 lease 保持到实际完成？
- `turn/start` 是否支持与 CLI 等价的 sandbox writable roots 注入？
- 对“服务端可能已接收但客户端未知”的场景，产品优先级是否应是避免重复投递？

### Initial Position
**Approve with conditions** — 合入前需解决不确定提交的立即 fallback、ACK 可写性和 ACK 后 lease 释放；补充最少的 worker 级失败/超时回归测试。中英文 README 也应统一 Desktop-first 行为。

## Round 2: Debate

## Elena Markovic — Final Review

**Direct responses to Chen/Priya**
- **Chen:** lease retention through `turn/completed` is now materially improved. However, an unknown submission still becomes retryable after timeout, without an idempotency key or turn-status query; a later direct/CLI retry can duplicate the original turn.
- **Priya:** `sandboxPolicy` and a CLI-only escape hatch address the main usability gaps. But this is only validated against a fake server; worker-level ACK, retry, lease, and hot-reload tests remain absent. Chinese documentation still describes daemon delivery as CLI-only at `README.md:923`.
- **UID trust boundary:** `SO_PEERCRED` limits cross-UID spoofing on Linux, but does not authenticate Codex against another same-UID process. `CODEX_LONG_TASK_WAKEUP_APP_SERVER_SOCKET` further permits redirecting production traffic to an arbitrary socket.
- **WebSocket validation:** HTTP upgrade checks are now sounder and headers bounded. Frame validation regressed: masked server frames, RSV bits, fragmented control frames, and oversized control frames are accepted at `src/long_task_callback/cli.py:969`; JSON-RPC responses also no longer require `"jsonrpc": "2.0"` at `src/long_task_callback/cli.py:868`.
- **Unknown submission/tests:** immediate fallback is correctly suppressed at `src/long_task_callback/cli.py:1105`, but the test only exercises the helper, not the worker’s timeout → retry lifecycle or later duplicate-prevention behavior.

**Vote**
- **Reject.**

**Non-negotiables**
- Reject invalid server WebSocket frames and restore strict JSON-RPC version validation.
- Remove or production-gate the arbitrary socket override; explicitly document same-UID IPC as the accepted trust boundary.
- Add durable idempotency/status reconciliation for unknown `turn/start` outcomes, or prohibit automatic retry/fallback for such callbacks.
- Add worker-level tests for ACK, turn completion, unknown-submit timeout, retry, and target-lease retention.

**Acceptable tradeoffs**
- Holding the lease until timeout is acceptable; delayed liveness is preferable to duplicate callback injection.
- Same-UID trust can be acceptable for a local developer daemon if documented and the endpoint path is not overrideable in production.

**Confidence**
- **High (0.90).**

## Chen Wei — Final Review

### Direct responses to Elena
- Strict upgrade validation and bounded headers are now adequate guardrails; same-UID peer verification remains the explicit trust boundary, not App Server authentication.
- I agree that `APP_SERVER_SOCKET_ENV` and permissive server-frame parsing need documented scope or hardening, but they do not alter delivery-state correctness.
- A valid JSON-RPC `turn/start` error is currently misclassified as “unknown”; it proves rejection and must not consume the full delivery timeout.

### Direct responses to Priya
- ACK write access is now propagated through `sandboxPolicy`, and the worker correctly retains both leases until `turn/completed` or the configured timeout.
- The documented Desktop-disable switch is sufficient operationally.
- The direct-path test validates protocol sequencing, but not the worker/queue/lease lifecycle needed to substantiate these claims.

### Vote
**Approve with conditions.** The revised design fixes the immediate duplicate-fallback and ACK-before-completion lease defects, but must distinguish definitive rejection from uncertain submission and prove state transitions end-to-end.

### Non-negotiables
- Split `turn/start` results into rejected, accepted, and unknown; only transport ambiguity may suppress immediate CLI fallback.
- Add worker-level tests for early ACK plus delayed completion, unknown submission timeout/retry, definitive rejection fallback, and HUP deferral while a direct turn owns the lease.
- Document that an unknown submission is at-least-once after timeout: a later retry can duplicate delivery without App Server idempotency.

### Acceptable tradeoffs
- Holding the lease until `resume_timeout` after a lost completion stream is conservative and preferable to overlapping turns.
- Same-UID socket trust is acceptable only as an explicitly documented local-user boundary.

### Confidence
**0.84** — completion-event handling and lease retention are structurally sound; residual risk is concentrated in error classification and untested worker-level recovery paths.

## Priya Nair — Final Review

### Direct responses to Elena
- Upgrade validation, header bound, peer-UID check, and fragmented-message handling materially improve Round 1 guardrails. `src/long_task_callback/cli.py:820`
- Same-UID impersonation remains an explicit local-trust-boundary tradeoff; document it. Also reject masked server frames rather than accepting them. `src/long_task_callback/cli.py:969`

### Direct responses to Chen
- The three-state design fixes duplicate delivery: only pre-submit failures use CLI; uncertain `turn/start` holds the lease until timeout. `src/long_task_callback/cli.py:1105`
- Holding the worker through matching `turn/completed` restores the intended lease and hot-reload behavior. `src/long_task_callback/cli.py:1149`
- One gap remains: an explicit JSON-RPC `turn/start` rejection is definitely not submitted, but is currently treated as unknown and suppresses CLI fallback. Distinguish rejected from response-lost. `src/long_task_callback/cli.py:1088`

### Vote
**Approve with conditions.**

### Non-negotiables
- Classify an explicit `turn/start` RPC error as safe CLI fallback; retain timeout-only behavior exclusively for lost/ambiguous submissions.
- Add worker-level regressions for rejected→CLI fallback, ambiguous→no CLI fallback, and ACK-before-completion retaining the target lease.
- Update the Chinese daemon documentation, which still states that `--via-daemon` executes CLI resume unconditionally. `README.md:923`

### Acceptable tradeoffs
- Safety-first ambiguous submission may delay recovery until the configured timeout; this is preferable to duplicate turns.
- Same-UID IPC trust is acceptable for this local opt-in feature if documented; no complex credential protocol is needed now.
- The current focused fake-App-Server test is a good protocol smoke test, but not a substitute for the three lifecycle cases. `tests/test_cli.py:1625`

### Confidence
**Medium.** The local schema supports `sandboxPolicy`, `turn/start`, and turn completion, but full confidence requires the worker-level regressions and one real Desktop daemon smoke test.

## Round 2 Tally

- Votes: 0 Approve, 2 Approve with conditions, 1 Reject.
- Consensus: unknown submission must not auto-retry or fall back; explicit rejection must fall back; ACK must not release the lease before matching completion.
- Required modification review: protocol hardening, socket override gating, manual recovery for unknown outcomes, worker-level lifecycle tests, and Chinese documentation.

## Modification Review

## Elena Markovic — Modification Review

**Vote:** **Approve with conditions.** The prior security and duplicate-delivery blockers are resolved: strict frame checks, JSON-RPC validation, gated override, documented same-UID boundary, and terminal `125` manual recovery are correctly implemented.

**Remaining blockers:** Add direct regression tests for RSV bits, masked server frames, invalid control frames, malformed JSON-RPC, and the disabled-by-default socket override; clarify the gated test/debug override; validate JSON-RPC 2.0 on completion notifications too.

**Confidence:** **High (0.93).**

## Chen Wei — Modification Review

**Vote: Approve with conditions.** Explicit RPC rejection now falls back safely, unknown submission suppresses automatic retry, and ACK no longer ends the worker before completion.

**Remaining blockers:** A known Desktop turn must retain its target lease after timeout until an explicit recovery action; add worker fallback and lease tests.

**Confidence:** **0.86.**

## Priya Nair — Modification Review

**Vote: Approve with conditions.** The product contract is now coherent: Desktop-first visibility, safe pre-submit CLI fallback, no replay after unknown submission, lease retention through completion, and a documented CLI-only escape hatch.

**Remaining blocker:** Add a worker-level assertion that explicit rejection launches the CLI fallback.

**Confidence:** **High (0.88).**

## Final Vote

## Elena Markovic — Final Vote

**Historical vote: Approve.** This was based on a then-unverified JSON-RPC 2.0 assumption. The field validation correction below supersedes that protocol-specific conclusion.

## Chen Wei — Final Vote

**Approve.** The durable `retain_target_lease` failed-state guard blocks same-session work and the cancellation regression proves controlled release, closing the timeout-overlap blocker.

## Priya Nair — Final Vote

**Vote: Approve.** The worker fallback assertion, documented manual lease recovery, and 67 passing tests close the prior conditions.

## Final Tally

- Votes: 3 Approve, 0 Approve with conditions, 0 Reject.
- Consensus: Ship the Desktop-first callback route with strict IPC framing, a same-UID trust boundary, CLI fallback only before a proven/ambiguous direct submission, and durable manual recovery for unknown Desktop outcomes.
- Validation: 67 unit tests pass, including direct protocol, worker lifecycle, lease-release, and legacy daemon regressions.
- Convergence: unanimous.

## Final Outcome

✅ **Approved.** The report is `.committee-reviews/2026-07-24-11-19-desktop-app-server-guardrails.md`.

## Post-Review Field Validation Correction

The real local Codex Desktop App Server accepted the WebSocket transport but used schema-shaped protocol messages without a `jsonrpc` member. The strict JSON-RPC 2.0 requirement in the original review would therefore reject a compatible App Server before `turn/start`; it was removed in version `0.4.5`.

The repaired client still rejects malformed protocol data: a response must have an `id`; a notification must have a string `method`; unmatched or malformed responses are errors; valid notifications received while awaiting a response are buffered. WebSocket framing, handshake validation, same-UID peer checking, test/debug override gating, ACK write access, completion-held target leases, and no-replay handling for ambiguous submission are unchanged.

Field evidence: at `2026-07-24T11:44:01+08:00`, the `Desktop direct callback smoke` task appeared in the bound Desktop task using the repaired direct route and was acknowledged. The remaining test-only same-session smoke request was prevented from overlapping by the retained target lease and explicitly canceled as redundant. A focused post-review panel revalidates this correction before release.

## Post-Repair Re-Review

### New Findings and Resolutions

1. **Cross-queue recovery lease:** A failed unknown Desktop submission originally retained state only in its source queue. The final revision writes a target-hash recovery marker under the global target-lock directory before the worker releases the target flock, so every queue blocks the same session.
2. **ACK/cancel and crash convergence:** ACK, cancellation, worker post-persistence rechecks, and startup/selection reconciliation all release a matching marker only when its owner queue records an ACK or cancellation tombstone. This avoids both permanent stale blocks and replay after an uncertain submission.
3. **Marker transaction safety:** Marker persistence, reconciliation, and deletion share a per-target global transaction lock. A competing selector fails closed while that transaction is active.
4. **Stale selection:** The delivery path rechecks the retained marker after acquiring the global target flock and before starting a worker, so a callback selected just before another queue records an unknown outcome is requeued without spending a retry.

### Final Votes

- **Elena Markovic — Approve.** The transaction lock serializes marker operations; the stale-selection recheck closes the final local IPC/recovery race.
- **Chen Wei — Approve.** The post-target-flock recheck and cross-queue regression prevent duplicate worker launch.
- **Priya Nair — Approve.** ACK/cancel/crash reconciliation and the 75-test suite make the operational behavior release-ready.

### Final Validation

- Real bound-Desktop callback observed and acknowledged at `2026-07-24T11:44:01+08:00`.
- `PYTHONPATH=src python3 -m unittest -q tests.test_cli`: **75 tests passed**.
- `git diff --check`: passed.
