# Acknowledgements and multi-stage goals

[← Back to README](../README.md)

Callback delivery ACK confirms that one wakeup was received and inspected:

```bash
ltc ack --queue-dir <queue-dir> --id <callback-id>
```

An unacknowledged callback is retried with backoff. ACK is monotonic.
An ACK marker immediately ends Desktop completion-stream waiting and releases the delivery lease,
even if the App Server completion notification is unavailable.
The resumed agent writes only the queue ACK marker. Global target-lock cleanup is performed by the
daemon, so ACK does not require broader filesystem permissions.

Goal ACK answers a different question: whether the whole multi-stage objective is finished or
cannot proceed:

```yaml
# goal-plan.yaml
version: 1
revision: 1
goal: Finish and publish the report
path:
  - id: draft
    title: Complete the draft
    status: completed
  - id: verify
    title: Verify figures and references
    status: in_progress
  - id: publish
    title: Publish the final report
    status: pending
amendments:
  - revision: 1
    reason: Initial path
```

```bash
ltc goal start --id report-goal --session <session-id> --cwd "$PWD" \
  --task "finish the report" --plan-file goal-plan.yaml
ltc goal check --id report-goal
ltc goal ack --id report-goal --state completed --plan-sha256 <checked-sha256>
ltc goal ack --id report-goal --state blocked_conditions --condition "awaiting dataset access"
ltc goal resume --id report-goal
```

The YAML file is the mutable source of truth. Its ordered `path` uses `pending`, `in_progress`,
`blocked`, and `completed`; completed items must be a continuous prefix. It tells the resumed
agent exactly which item is current and what remains. Users may revise later items, reopen work,
append steps, or change the top-level goal, preferably incrementing `revision` and recording the
reason in `amendments`.

Before reporting completion, the agent must run `goal check` and compare actual work and artifacts
with the latest file. `goal ack --state completed` is rejected unless the supplied digest matches
that check and every path item, including the final one, is `completed`. Any YAML edit invalidates
the previous digest and forces a fresh check, so a remembered obsolete plan cannot finish a goal.
Existing active goals created before 0.6.2 can be migrated with
`ltc goal set-plan --id <goal-id> --plan-file goal-plan.yaml`; attaching or replacing a plan also
clears the previous check.

Use one file per independent goal, preferably `.ltc/goals/<goal-id>.yaml`. A clear, low-risk path
may be drafted by the agent and announced without a blocking approval; strategic choices,
material resource changes, or changed acceptance criteria should be agreed with the user first.
Blocked, resumed, and revised work keeps the same file. A follow-on goal gets a new file. Completed
plans remain at their recorded paths as audit records and are never automatically deleted or
reused. If archival relocation is desired, move and reattach the file with `goal set-plan` before
the final check and completion ACK.

Callback ACK never completes the goal. While an active goal has no newly queued ordinary callback,
the daemon automatically asks the same conversation for its status every three hours by default.
The agent must continue, ACK the goal as completed, or record the exact blocked condition. This
inquiry behavior also applies after reboot recovery.

Bind a submitted task or an external completion callback with `--goal-id <goal-id>`.
