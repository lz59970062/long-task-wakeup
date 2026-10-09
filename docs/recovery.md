# Restart, recovery and self-repair

[← Back to README](../README.md)

## Configuration recovery owned by the Agent

`run`, `agent` and `done` check environment support, configuration and LTC's runtime
chain automatically: storage, coordinator, recorded task owners, results and
callback handoff. When anything needs attention, stderr contains an `[ltc-status]` JSON block with the issues, repair
and recheck commands, and whether work has already been persisted. Healthy calls
remain quiet. The installed skill tells the calling Agent to configure LTC,
verify the repair and continue the task; routine configuration is not assigned
back to the user. A persisted task is not resubmitted after repair.

For an explicit check, use `ltc doctor --agent codex --session <original-session>`
with the same `--queue-dir` and `--backend` as your task. It prints JSON and exits
0 when local prerequisites pass, 1 when configuration is needed. This check does
not establish Agent authentication or successful original-session delivery.
Repair commands use the current installation and preserve installed skill files
with `setup --keep-skill`; they do not automatically execute themselves.
The report separates configuration gaps from runtime failures. A missing task
owner or unresolved callback requires inspecting its records, so the generic
setup command is omitted. An empty queue needs no screen session, and a completed
task is not a dead-service alert. Native unsupported systems receive no Linux
repair command. Owner queries that fail are reported as unknown, never proof that
work can safely be rerun.
For externally owned work reported with `done`, the repair uses
`setup --callback-only` and does not require a local task execution backend.

## Daemon restart

Native task services are independent of the coordinator service. Recovery reads durable results
and probes the recorded owner; an unavailable manager never authorizes another launch. Legacy
screen tasks keep their named sessions, but stopping their containing service may stop those
processes as well. A completed result can reconstruct a missing callback with the same ID.

## Worker startup handshake

Native launches persist an attempt identity before submission. Ambiguous outcomes are inspected
without automatic resubmission, even if the unit has already disappeared.

Each legacy screen launch must complete a durable handshake by changing the task from `launching` to
`running`. LTC allows a one-second startup window. If screen disappears before that transition,
the daemon records the failed attempt and retries at most three times with exponential backoff.
After the final attempt, the task becomes `launch_failed`, its one-time recovery callback is
queued, and it is never launched again automatically. Worker tokens use an `ltc_` prefix and are
passed as one `--token=value` argument so a token can never be parsed as another command-line
option. A legacy `launching` record without an attempt counter is treated as an unknown prior
launch failure and is never automatically retried during upgrade.

## Same-host reboot

Running processes cannot survive a host reboot. LTC therefore does **not** guess or automatically rerun
the command. After the daemon starts in the new boot, it restores the originally bound Codex or
Claude Code conversation with:

- the task and execution-owner identifiers;
- the local log path;
- the interruption reason;
- instructions to inspect outputs and checkpoints.

That agent then follows the normal task-creation workflow: recreate the run from a valid checkpoint,
supplement the status if artifacts prove it already finished, or record the precise blocking
condition. There is no `--resume-command` interface.

## Cross-host recovery

Cross-host recovery is intentionally unsupported. LTC does not transfer workspaces, datasets,
environments, checkpoints, credentials, or compute allocation to another machine.
