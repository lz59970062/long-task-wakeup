# PI Agent child-worker validation

Recorded on 2026-09-28 on Linux with Python 3.12.4, Pi 0.87.1 and Node
22.23.3. The checkout was fast-forwarded from `f2695b8` to remote `main`
`d16cff5` before adding this feature. Package version remains the current
unreleased preview, `0.7.1a1`.

## Supported contract

`ltc agent pi` launches a fresh Pi child with prompt stdin and
`pi --print --mode text --no-session`. It saves stdout as the private
`agent-result.txt` and preserves the process exit code. `--model` supports
Pi's provider-qualified notation; `--reasoning-effort` maps to `--thinking`.
Callbacks remain bound to the originating Codex or Claude session. Pi is
not registered for callback discovery or resume.

Pi-only `--system-prompt-file PATH` reads a nonblank UTF-8 source and saves a
private `agent-system-prompt.md` snapshot before submission. Pi receives the
snapshot through `--system-prompt`; it replaces Pi's base system prompt while
retaining normal appended instructions/context. The task remains on stdin.
CLI relative paths use the submitting directory; template
`pi.system_prompt_file` uses the YAML directory, and CLI overrides it. Required
snapshots are checked before launching Pi so a missing file cannot silently
become literal path text. An editable sample is
`examples/prompts/pi-system.md`.

## Automated checks

| Check | Result |
| --- | --- |
| Independent Pi contract tests, `tests/test_pi_agents.py` | All 24 passed within final source discovery |
| Complete source discovery including system prompt support | 461 discovered, 373 passed, 88 skipped, zero failures; 37.508 seconds |
| Wheel build with existing interpreter/dependencies | Passed; Pi adapter and synchronized skill included |
| Installed package comparison | All 39 Python/skill files matched current source |
| Installed CLI | `ltc 0.7.1a1`; `ltc agent pi --system-prompt-file examples/prompts/pi-system.md --dry-run` resolved the system source |
| Coordinator upgrade | Hot reload retained PID `3543435`; runtime reported `0.7.1a1` |
| Existing workloads | At both reloads, all five tasks recorded immediately beforehand retained their worker PID and were alive afterward |
| Installed Codex/Claude skills | Both matched repository `SKILL.md` |
| Whitespace check | `git diff --check` passed |

Commands used from the repository:

```bash
PYTHONPATH=src:tests python3 -m unittest test_pi_agents -v

PYTHONPATH=src \
  CODEX_HOME=/tmp/ltc-pi-validation/system-suite-codex \
  CLAUDE_CONFIG_DIR=/tmp/ltc-pi-validation/system-suite-claude \
  CODEX_LONG_TASK_WAKEUP_TARGET_LOCK_DIR=/tmp/ltc-pi-validation/system-suite-locks \
  python3 -m unittest discover -s tests -q

python3 -m pip wheel --no-deps --no-build-isolation --no-cache-dir \
  --wheel-dir /tmp/ltc-pi-validation/wheels-system-prompt .
```

The new tests cover both callback parents, environment snapshots, parent-marker
removal, exact UTF-8 stdout and log capture, nonzero and empty failed results,
model/thinking options, custom and built-in templates, frozen task artifacts,
invalid-input rejection before persistence, child-only registration and Pi
executable diagnostics. Fake child processes exercise the actual private LTC
worker; they do not call a model API.
System-prompt tests additionally cover CLI/YAML precedence and relative paths,
BOM normalization, literal text, separate task/system artifacts, invalid source
admission, dry-run and changed/deleted sources. Missing/blank private snapshots
or paths outside the task directory interrupt the worker without invoking Pi.

## Actual Pi and native task ownership

A separate probe connected the installed Pi CLI to a deterministic loopback
OpenAI-compatible SSE provider with an isolated Pi profile and synthetic API
key. No paid provider was used. It verified stdin content, final text,
assistant error exit status, an actual Pi `write` tool call, the changed file
and the tool result returned to the provider.

After updating the installation, the same probe submitted Pi children through
the real existing coordinator and independent systemd user services:

| Task | Actual behavior | Durable exit | Callback receipt |
| --- | --- | --- | --- |
| `a09187e1` | Custom system snapshot reached the system role; Pi write tool replaced a fixture; final text `PI_LTC_LOCAL_TOOL_OK` | 0 | Bound original Codex session, inspected and manually ACKed |
| `d1008210` | Custom system snapshot reached the system role; simulated provider SSE error; empty assistant-result file | 1 | Bound original Codex session, inspected and manually ACKed |

The mock provider recorded system/developer and user messages separately and
verified the frozen system text led the former while the task remained in the
user role. A separate actual-Pi/private-worker probe rewrote/deleted the source
after submission and still delivered the original snapshot. Both probes used
BOM/Unicode/space-containing source files, with no interpolation of literal
shell or template notation.

No Pi conversation files were persisted. These cases used
`--callback-mode manual`; automatic CLI/Desktop callback delivery and native
macOS/Windows Pi execution were not exercised. The earlier private-worker
probe used a simulated owner and is not evidence of native ownership; the
two tasks above used real systemd services.

Local evidence retained at validation time:

- Final source suite: `/tmp/ltc-pi-validation/system-prompt-unittest.log`.
- Earlier source suite before the additional system-prompt requirement:
  `/tmp/ltc-pi-validation/unittest.log` (452 discovered, 364 passed, 88 skipped).
- Actual-Pi/private-worker system-prompt probe:
  `/tmp/ltc-pi-real-probe-_u5lz4n1/SUMMARY.json`.
- Actual coordinator/systemd/Pi system-prompt probe:
  `/tmp/ltc-pi-real-probe-qta_hm43/SUMMARY.json`, `http-events.json` and
  `fixture.diff` in that directory.
- Per-case `write-success.system-contract.json` and `error.system-contract.json`
  in each final probe directory record the source, snapshot and role checks.
- Both upgrade verifications: `/tmp/ltc-pi-validation/upgrade-result.json` and
  `/tmp/ltc-pi-validation/system-prompt-upgrade-result.json`.
- Prior service/skill backups and a rebuilt 0.7.0 recovery wheel:
  `/tmp/ltc-pi-validation/upgrade-backup/`.
- Complete pre-system-prompt package/service/skill backup:
  `/tmp/ltc-pi-validation/system-prompt-upgrade-backup/`.

The first backup identity check failed under the sandbox, and installation
continued before that backup completed. Before reloading, the identity was
verified outside the sandbox, unchanged services/skills were backed up and
the exact 0.7.0 release source was archived and rebuilt for recovery.

Two earlier probe attempts failed in the harness: one expected string content
instead of OpenAI text blocks; the other omitted the submitting user's D-Bus
environment. Correcting those harness assumptions produced the passing
records above. They required no production-code changes or workload replay.
The existing queue also reports historical callback/handoff issues, so these
new task results do not establish that the entire historical queue is healthy.

## Re-verification and branch isolation on 2026-10-03

`origin/main` was fetched and still resolved to `d16cff5`; the preview work was
moved onto the branch `feature/pi-agent-support` so `main` keeps its original
commit and a clean worktree.

Repeated checks on Python 3.12.12 with Pi 0.87.1:

| Check | Result |
| --- | --- |
| `tests/test_pi_agents.py` | 24 passed |
| `tests/test_template_registration.py` + `test_template_registration_namespaces.py` | 23 passed |
| `unittest discover -s tests` (isolated `CODEX_HOME`/`CLAUDE_CONFIG_DIR`/lock dir) | 484 discovered, all passed, 88 skipped, 41.9 s |
| `ltc agent pi --dry-run --system-prompt-file examples/prompts/pi-system.md ...` | resolved the frozen system source and printed the submission summary without writing task files |
| `pi --print --mode text --no-session --system-prompt <file> --thinking high --model provider/... --offline` | Pi accepted every flag and stopped at model resolution; no option-error |

This pass re-ran the repository suites and CLI argument/shape checks only. The
real-provider, coordinator/systemd and native macOS/Windows probes recorded above
were not repeated here and keep their original evidence.
