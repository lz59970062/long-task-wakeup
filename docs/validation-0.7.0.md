# 0.7.0 release validation — 2026-09-27

The final release consolidates the Linux, macOS and Windows work from
`cb48ace023ea66a8662953f1e33670aa082a5ac5` on `codex/0.7.0-preview`.
Release preparation changes package metadata and release documentation; it does
not replace the validated execution backends or callback protocols.

## Cross-platform evidence

The unified baseline's [GitHub Actions run](https://github.com/lz59970062/long-task-wakeup/actions/runs/36283457985)
completed successfully. Its seven jobs cover Linux, macOS and Windows with Python
3.9 and 3.12, plus the non-root Docker image. Native platform jobs separately run
their systemd/screen, launchd/screen or Windows Scheduler/lifecycle checks.
Discovery counts include platform-specific and opt-in skips; they are not counts
of tests actually executed on every platform. CI does not perform real model
calls or Desktop GUI acceptance.

The final release commit is submitted to this same matrix before `v0.7.0` is
published. Its check results are attached to the [tagged commit](https://github.com/lz59970062/long-task-wakeup/commits/v0.7.0).

Native evidence from the platform development machines is retained in
[macOS validation](validation-macos.md) and [Windows validation](validation-windows.md).
The Mac record includes original-session receipt and ACK for a 120-second task.
The Windows record includes native execution and original-session receipt/ACK
through the explicitly enabled experimental package-context Desktop bridge.
These historical records identify their tested environments and earlier failures.

## Final Linux package checks

Checks use isolated Agent profiles, queues and locks, with inherited Desktop
bridge configuration disabled. The production coordinator is not restarted and
no real model call or GPU workload is used.

| Check | Observed result |
| --- | --- |
| Full source suite, Python 3.12.4 | 418 discovered: 337 passed, 81 platform/opt-in skips, zero failures |
| Source distribution and wheel | Built the source archive, then built the wheel from that archive; package and CLI version are `0.7.0` |
| Installed wheel outside the checkout | All three console entry points load; both LTC aliases report `0.7.0`; skill, Agent metadata, test template and Windows Scheduler script are included |
| Installed-wheel native Linux integration | 10 discovered: 4 passed (systemd, screen lifetime, stale PID, configuration repair), 6 other-platform/opt-in skips |
| Non-root Docker with runtime networking disabled | 10 discovered: 3 passed (screen lifetime, stale PID, configuration repair), 7 other-platform/opt-in skips |
| Skill/source hygiene | Root and packaged skills match; skill validator and `git diff --check` passed |

Local wheel imports resolve under `/tmp/ltc-070-installed`, not the checkout.
The Windows Desktop wrapper entry point was checked for import and its safe
missing-Core failure on Linux; no native Desktop launch is claimed here.
The local Docker build uses the example Dockerfile with the existing
`runtime-cuda124:latest` base (Python 3.11), without using CUDA. The public CI job
separately builds the default `python:3.12-slim` image.

Run the source suite with:

```bash
env -u CODEX_LONG_TASK_WAKEUP_DESKTOP_BRIDGE_FILE \
  CODEX_LONG_TASK_WAKEUP_DESKTOP_APP_SERVER=0 \
  CODEX_HOME=/tmp/ltc-070-release-profile \
  CLAUDE_CONFIG_DIR=/tmp/ltc-070-release-claude \
  CODEX_LONG_TASK_WAKEUP_TARGET_LOCK_DIR=/tmp/ltc-070-release-locks \
  PYTHONPATH=src python3 -m unittest discover -s tests -q
```

Native integration uses the same isolation plus `LTC_TEST_SYSTEMD=1`,
`LTC_TEST_SCREEN=1` and `LTC_TEST_DIAGNOSTICS=1`, with
`python3 -m unittest discover -s tests -p 'test_*integration.py' -v`.
Use `PYTHONPATH` pointing to an isolated wheel installation and run outside the
source checkout when checking the package. No cross-platform result in this
document implies that every Windows test child imported from the wheel: several
native test helpers explicitly use the source checkout.

## Release boundaries

- Linux uses independent systemd user services or screen; non-systemd containers
  use screen plus an explicitly hosted coordinator. Actual AutoDL provider boot
  hooks and storage-retention policies require environment-specific verification.
- macOS uses launchd in the logged-in user context. Windows uses Task Scheduler
  and Job Objects in a logged-in interactive user session.
- Logout, reboot or container destruction can interrupt tasks. Saved state and
  callbacks do not provide transparent command replay or cross-host migration.
- Callback delivery is at-least-once. Inspect existing work before continuing;
  ACK is receipt of one callback, not proof that the whole goal is complete.
- The optional Windows Codex Desktop bridge remains experimental. Its tested
  package-context route is not a guarantee for other Desktop versions, all App
  Tools flows, or normal package activation. The default CLI active-writer limit
  remains; see [bridge setup and limits](windows-desktop-bridge.md).
- Native PI/DSH adapters and npm/frozen distribution remain future work. No live
  Claude end-to-end callback is claimed by these release checks.
