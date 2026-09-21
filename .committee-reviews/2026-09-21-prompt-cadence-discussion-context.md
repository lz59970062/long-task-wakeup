# Callback reminder cadence review
Scope: reduce repeated standard instructions and user callback-hook reminders. Keep results, messages, recovery/template-specific guidance, target binding and ACK command every time. Do not execute/ack the user's pasted example callback.
Design: per queue + agent + bound session counter at first delivery attempt; first callback full, system every4 (1,5,9), user every3 (1,4,7). Stable allocation per callback ID, atomic one-file counter+allocation ledger under queue prompt-counters, existing POSIX lock. Retries/restarts reuse allocations; enqueue/dry-run do not count. Unknown --last target remains full without counting. Prompt-generation stores both full and compact versions; legacy requests without compact text keep full system prompt. Delivery worker picks full/compact and conditionally attaches user hook; due hook content still reread on each retry. Config callback-prompts.yaml under LTC state directory, defaults4/3; `ltc prompt-policy --system-every N --user-every M`, no flags shows current. Positive integers; 1 always; invalid config/counter IO => warn and preserve full reminders instead of losing callback. Core skill documents omitted instructions still apply.
Panel: Ada (API clarity), Lin (independent testing), Sam (reliability and crash/concurrency). Parent implements; reviewers do not edit production. Template/recovery-specific messages are not filtered as generic boilerplate.

## Round 1: Independent reviews

Ada — Independent Round 1, callback reminder cadence

Vote: approve with explicit contract and validation conditions.

1. Document exact ordinal semantics, not merely “every N”: first callback is full, system defaults to 1/5/9 and user hook to 1/4/7. This counts distinct callback IDs allocated at the first delivery attempt, not successful deliveries, ACKs, enqueue operations or retry attempts. Queue + agent + bound session scope means changing queue directories starts a separate sequence. `--last` remains full because the concrete destination is unknown.
2. Make `ltc prompt-policy` show effective values, configuration path, counter scope and the first/every examples. With one flag, preserve the other value; no flags must be read-only. Validate both CLI values before writing, reject booleans/floats/zero/negative values from YAML, and update atomically. Avoid silently resetting counters on a policy edit. State whether changed periods apply to the next unallocated callback; retry allocations must remain unchanged.
3. Compact prompts must preserve all task data, user messages, target routing and ACK command, plus recovery and test-authoring-specific instructions. Remove known generic sections while constructing prompts, never by substring replacement on a finished prompt: the user message may contain identical wording. Existing requests with no compact representation should retain the full prompt.
4. User-hook suppression changes an existing delivery contract: `callback-hook.md` previously attaches on every delivery attempt. Clearly describe this as reminder cadence, and document that omitted instructions remain applicable. `--user-every 1` is the predictable option for hooks containing instructions that must appear every time. This is a deliberate tradeoff when new context or compaction removes earlier reminders; periodic delivery does not guarantee retained model memory.
5. Configuration and ledger failures should preserve delivery with full reminders and an actionable warning. Expose enough diagnostic information to distinguish fallback from normal cadence. A malformed config should not be silently overwritten by a one-field edit because doing so may discard a user's other policy value.

Questions:
- When changing a period, is scheduling computed from the existing absolute ordinal with `(ordinal - 1) % period == 0`? Recommendation: yes for new allocations, with already allocated retries frozen.
- Do goal-idle reminder callbacks consume the same sequence? Recommendation: yes if they are delivered callbacks, but explicitly document this so the user can explain apparent gaps between task-completion reminder ordinals.

Release conditions: behavioral tests for exact default sequences, one-field policy updates and invalid inputs, retry/restart stability, separate queue/agent/session scopes, safe fallback and preservation of messages/ACK/template/recovery material. No production edits made.


Lin — independent testing, Round 1

1. P1: Allocation must be durable and idempotent by callback ID, not delivery attempt. Verify retry after a simulated restart and concurrent fresh IDs; checking sequential counters alone misses lost updates and duplicate allocations.
2. P1: Compact prompts must retain result data, ACK command, and task-specific instructions. Construct compact text from known structure rather than deleting matching phrases from arbitrary user/recovery/template text. Test user messages that happen to contain generic reminder wording.
3. P1: Invalid policy or corrupt counter must fail toward full reminders plus hook. Do not reset the counter silently or throw away callback delivery. Exercise unreadable/malformed state and unknown target kinds.
4. P2: User hook content must be reread for due retries; cadence decisions stay fixed. Test changing hook between retries separately from policy changes. Legacy payloads should remain deliverable without compact text.
5. P2: The cadence source is first delivery attempt, not successful completion. Documentation should say this explicitly; failed submissions and dry-run must not advance it.

Questions: Are policy updates intended to change due decisions for callbacks that already received an allocation? Recommendation: no. Are callbacks with no compact variant counted? Recommendation: retain full system text but apply normal user-hook allocation for a valid bound session.

Vote: approve conditional on atomic allocation, conservative fallback, and parent-run tests proving retained callback content. Prompt shortening is useful but must never remove task-specific action or acknowledgement instructions.


Sam — Reliability Round 1

Vote: Approve with conditions. The one-file counter/allocation transaction is the correct approach; separate counter and per-request persistence would introduce a crash window. Confidence medium pending implementation and crash/concurrency tests.

1. P1 — Allocate only after delivery ownership is acquired. process_one increments attempts before run_resume_until_exit_or_ack and rolls back on TargetLeaseUnavailable. Allocating there would count deferrals. Allocate in the delivery worker after target locks are held, before choosing the prompt; desktop-to-CLI fallback must reuse the same decision. One callback ID must allocate once despite retries or daemon restart.
2. P1 — Lock a stable separate lock inode around read/validate/update/write of the complete ledger. Do not flock the JSON inode replaced by write_request. Existing atomic write_request provides fsync plus replace; use it for the ledger and validate the complete saved allocation before trusting it. Namespace keys should use an unambiguous tuple encoding or digest of agent and bound session; queue root provides queue separation.
3. P1 — Persist the actual system/user booleans, not merely the ordinal. Policy edits between attempts must not change a callback's reminder decisions. Check an existing allocation before interpreting the latest policy; transient config errors should not discard an otherwise valid durable allocation. If persistence fails, send full reminders, never compact text based on an uncommitted decision.
4. P2 — Define graceful fallback precisely. Invalid JSON/YAML, invalid counts including booleans, Unicode errors, and lock/filesystem errors should warn and deliver full system/user reminders. Missing config is the normal defaults case. Corrupt ledgers should not be silently reset and overwrite history. A failed allocation cannot promise retry stability; explicitly document full-reminder fallback as the exceptional case.
5. P2 — Keep the delivery payload authoritative for both transports. Once selecting compact/full, update payload prompt even when no user hook is appended; otherwise desktop and CLI paths can diverge. Legacy requests without compact content must retain full text while independently applying the user cadence. Unknown --last targets should avoid ledger writes altogether. Unbounded allocation retention is acceptable initially only if documented; pruning entries still eligible for retries breaks stable decisions.

Questions:
- Do policy changes affect only first delivery attempts, leaving all saved allocations untouched? Recommendation: yes.
- Are goal-inactivity callbacks counted as callbacks even though their legacy/system-specific text stays full? Recommendation: explicitly decide and document; preserving full goal guidance is required regardless of counting.

Validation conditions: concurrent same-session allocations produce unique ordinals; the same callback ID reuses decisions after restart and policy change; lock/IO/corruption failures still deliver full reminders; lease deferral/enqueue/dry-run do not count; first/nth selection agrees across CLI and desktop paths; ACK, routing, results, and template/recovery guidance remain present.


## Implementation for Round 2
Current cli.py implements prompt_compact at queue preparation; full routing retained. select_delivery_prompt runs inside delivery_worker_main after inherited target/delivery locks; mutates payload for both transports. Ledger atomically persists count and allocation together under separate lock inode, agent/session/queue scoped. Missing config defaults4/3, invalid config/state full fallback preserving history. Existing allocations read before policy to keep retries stable. `ltc prompt-policy` no flags read-only; partial update preserves other setting.

## Round 2: Debate

Ada — Round 2 callback cadence review

Vote: Approve with documentation and validation conditions. Confidence: medium-high. No release-blocking code defect found in the reviewed implementation.

The API preserves one-field updates, rejects invalid intervals, reads without writes when no flags are supplied, and performs atomic policy replacement under a separate lock. Existing callback decisions are read before current policy, which correctly freezes retries. Queue/agent/session scope is encoded without delimiter ambiguity. Compact text retains routing and the concrete ACK command, and payload mutation makes transport choice consistent.

Response to Lin: preservation is implemented through generated-wrapper prefix/suffix checks rather than global phrase deletion. Thus a normal user message containing standard guidance stays intact; the generated trailing copy alone is removed. This is acceptable for current producers, although a structured prompt builder would be more robust if new producers are introduced. Please retain the adversarial message test and tests for template/recovery guidance. Legacy requests retain full system text while participating in user cadence, as recommended.

Response to Sam: allocation is inside the delivery worker after ownership has been acquired, and persisted count plus decisions share one atomic ledger update. Separate stable locking and saved booleans satisfy the crash/retry design. The ledger validates all entries before trusting them, and corruption preserves history with full-reminder fallback. Unbounded retention is a conscious initial limitation and should be documented.

Remaining conditions:
1. User-facing documentation is still missing from the reviewed diff. Explain first callback plus every N (defaults 1/5/9 and 1/4/7), first-attempt counting, queue/agent/session scope, goal-reminder participation, policy edits affecting only new allocations, unchanged retry decisions, and `--last` fallback. Explain omitted hook instructions still apply, with `--user-every 1` for always-present hooks.
2. Current `prompt-policy` output prints the path and YAML only. Add a concise scope/first-callback explanation to output or help so users do not infer N/2N/3N. This is a minor discoverability issue, not a correctness blocker if documentation clearly defines it.
3. Parent-run regression checks must demonstrate exact sequence and preserved task content, partial update behavior, stable retries after config edits, independent scopes, due-hook rereading, and conservative corruption fallback.

Accepted tradeoffs: compact ACK omits environmental boilerplate but retains the command and timing condition; a compact callback does not restate every behavioral instruction. Existing conversation context is assumed, not guaranteed across compaction. The cadence footer offers useful diagnostics, though it consumes some of the token savings.


Lin — Round 2

I agree with Ada that absolute ordinal semantics and the user-every=1 escape hatch need explicit documentation. The implementation preserves partial policy updates and read-only display. I agree with Sam that allocation belongs in the locked delivery worker, not enqueue or attempt bookkeeping; the shared payload mutation and separate lock plus atomic ledger address those failure modes.

The implementation removes only an anchored generated wrapper and terminal guidance suffix; it does not replace matching user-message substrings. I added an adversarial preservation case where user content repeats standard reminder wording and includes recovery/template instructions. New allocation tests cover four concurrent processes, fresh-dict retries, policy changes, corruption fallback, and independent scopes. Selection tests cover both cadence periods, legacy full text and hook rereading on retries.

Vote: approve conditional on parent-run tests. Confidence high on ordinary selection semantics, medium on filesystem-failure behavior until existing reliability checks and independent tests run. No claim of model-memory retention follows from periodic reminders; omission only reduces repeated text. The full ledger grows without pruning, an acceptable documented tradeoff for stable historical retries. No remaining design blocker identified from this round.

Tests have NOT been run by the author. See cadence-test-handoff.md for command and limits.


Sam — Reliability Round 2

Vote: Approve conditional on the planned behavioral validation. No blocking reliability defect found in current implementation. Confidence: high for lock ordering, persistence, and retry semantics; medium overall until transport/concurrency tests pass.

Response to Ada: Policy changes preserve earlier allocations because the ledger lookup precedes policy loading. Partial CLI updates read and merge under a stable config lock; no-flags display does not acquire/create a lock. Invalid config is rejected rather than overwritten. The CLI currently prints path and YAML but not scope/ordinal examples; adding those would meet your usability recommendation, though documentation can adequately explain the contract.

Response to Lin: The ledger validates stored periods, sequence uniqueness, count consistency, and due booleans. Corrupt state remains untouched and selects full reminders. Legacy requests lacking compact text retain full system content while user scheduling still applies; hooks are reread after each due selection. Compact extraction is limited to the generated opening and terminal guidance, rather than global phrase deletion. Tests must still prove preservation of user messages containing those phrases and task-specific recovery/template content.

Reliability findings confirmed:

1. Allocation occurs inside delivery_worker_main after inherited delivery/target descriptors arrive via pass_fds. Target-lease deferrals, enqueue, and dry-run do not reach allocation. The extra ledger lock is separate from replaced JSON, held across read/validate/update/write, and released without unlinking its inode. Counter locks do not inherit into agent processes.
2. One atomic write_request stores counter and allocation, including actual due flags and original periods. Crash before replacement leaves no allocation; crash after replacement makes retries reuse it. A committed allocation followed by failed launch still consumes one ordinal, consistent with first-attempt semantics. Existing allocation lookup does not depend on current policy readability.
3. Scope is root/prompt-counters plus a SHA256 digest of JSON [agent, session]. This is unambiguous, queue-local, and agent-separated. --last/unknown targets return full without allocating. Policy is derived from the parent-provided hook path's directory, so worker-side environment changes do not accidentally select another state directory.
4. select_delivery_prompt always updates payload['prompt'], including compact callbacks without a hook. Both desktop and CLI therefore consume the same decision, and transport fallback cannot allocate a second ordinal.
5. IO, decoding, malformed YAML/state, and missing POSIX-lock support during allocation fall back to full reminders. UnicodeDecodeError is already a ValueError subclass. Ledger history is not reset on corruption. Hook read errors retain callback delivery using the preexisting warning behavior.

Accepted limitations: A write can fail after replacement/fsync, so that particular attempt may send full reminders even though a retry finds a committed compact allocation; this is the documented conservative IO-failure exception, not a duplicate-counter bug. The ledger grows and is fully validated/rewritten on each new callback; acceptable at current scope, but long-lived high-volume queues need a future retention design preserving retry idempotence. Saved policy periods govern retries indefinitely.

Validation conditions: Concurrent callback IDs in the same scope; retry after process restart and policy edit; corrupt/unreadable ledger/config full fallback; identical transport selection; legacy and --last behavior; retained routing, ACK command, template/recovery details and user text. No production edits made.


## Round 2 tally
3/3 approve with documentation and validation conditions; no unresolved disagreement.

## Validation and resolution
145 full-suite tests passed in 13.290 seconds. All 14 independently authored cadence tests passed in the final focused run. Covered separate schedules, per-session/agent/queue scope, retry policy freezing, four-process distinct-ID and same-ID allocation, invalid configuration/counter fallback, partial settings, and Desktop/CLI payload selection. A final startup guard avoids counting already ACKed/canceled callbacks; the regression verifies neither transport starts. Core skill and README document ordinal semantics, goal reminders, preserved content, legacy behavior, and hook suppression. No real model callback was sent and the user's pasted sample was not executed or acknowledged.

Final outcome: Approved. Report: .committee-reviews/2026-09-21-prompt-cadence.md. Panel work complete.
