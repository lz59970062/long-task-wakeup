# 0.7.1a1 local validation — 2026-09-27

This is an unreleased preview on `codex/macos-desktop-callback`, based on the
0.7.0 release `f2695b8`. Native execution and live callback acceptance are
reported separately.

## Completed checks

The automated results below predate the follow-up installation integration
(automatic launcher generation in setup, app discovery and the packaged Core
entry shim). They do not establish regression coverage for those later edits.

| Check | Result |
| --- | --- |
| Final unittest discovery on macOS | 437 discovered; 356 passed, 81 skipped; 16.370 seconds |
| New callback-mode contracts | 12 passed: origin detection, manual receipt, refusal before admission, read-only probing, busy deferral, explicit Desktop routing and callback-only retry |
| Mac launcher/metadata unit checks | 6 passed |
| Installed Mac wrapper with real bundled Core | Two clients shared a disposable thread; EOF stopped the Core and removed socket/metadata; 1 passed in 2.472 seconds |
| Native launchd integration | 2 passed in 0.902 seconds; disposable jobs/profile and fake Agent |
| Installed package | `0.7.1a1`; `pip check` passed; seven changed runtime modules matched checkout SHA-256 hashes |
| Existing LTC coordinator | Reloaded in place to `0.7.1a1` after confirming its queue had no active deliveries |
| Desktop preparation | Private configuration and double-click launcher created; bundled Core version `0.155.0-alpha.16.4` passed launcher preflight |

The test environment uses Python 3.14.6 on macOS arm64. Regression commands set
temporary `CODEX_HOME`, `CLAUDE_CONFIG_DIR` and target-lock directories, put the
project virtual environment first on PATH, and remove the live Desktop origin
and App Tools pipe from the test process environment. No model calls or live
conversation mutations are used by these automated tests.

One intermediate full run hit the existing 2-second scheduling assertion at
2.185 seconds. The unchanged test passed alone in 1.383 seconds, and the final
full run passed. Earlier harness errors from sandbox restrictions and an
unactivated fixture Python were resolved by isolated ordinary-user execution
with the virtual environment on PATH; assertions were not weakened.

The installed wrapper's real-server test uses an empty temporary profile and
the Desktop's actual Core. It establishes protocol sharing and lifetime cleanup,
not real GUI tool/approval routing or an authenticated callback turn.

## Current installation and remaining acceptance

The existing service/skill were backed up before preview setup. The launcher is
`~/.codex/long-task-wakeup/Start LTC Desktop.command`; configuration is beside it
in `desktop.json`. No installed Desktop app files or global environment settings
were changed. Desktop was not restarted.

The final `doctor` observation reports local configuration ready, callback
blocked, and the old failed callback still needing attention. Desktop launch
preflight reports `configuration_valid: true`, `launch_ready: false`, with
`desktop_running` as its blocker. These are expected pending activation and must
not be described as end-to-end readiness.

- **Pending:** quit/relaunch Desktop through the prepared launcher at an agreed
  idle time; reopen the original conversation; verify normal App Tools.
- **Pending:** requeue original callback `bdfd671d` with Desktop mode, inspect its
  saved 60.014-second result, receive it in the original conversation and ACK.
  Its completed workload was not rerun and no ACK was manufactured.
- **Pending:** a fresh authenticated CLI original-session callback acceptance
  for this preview. Automated CLI fixtures and the prior IDE/shared-server Mac
  success are not substitutes for that result.
- **Pending:** live Desktop restart acceptance and native Windows regression of
  the new explicit-mode behavior. The recorded 0.7.0 Windows bridge acceptance
  remains historical evidence for that build only.

Do not label this preview a fully accepted cross-client release until those
relevant live checks are recorded.

## Installation packaging follow-up

The Mac launcher is now integrated into standard `ltc setup`. The Git install
helper invokes setup with the installing interpreter. App discovery, a packaged
absolute-path Core entry shim and generated local wrapper remove dependencies
on a developer's checkout path and on console-script PATH discovery.

The wheel was built and installed locally, then inspected as an archive:

- `codex_long_task_callback-0.7.1a1-py3-none-any.whl`
- SHA-256: `771524c64ebcd53f6eddf2d98f449c78da2ea2a4c109850350fa1df7135a13a2`
- Required Desktop/runtime modules were present and matched the source files.
- No developer-home path was found in the wheel contents.
- Python AST parsing, installer shell syntax and `git diff --check` passed.
- Installed `ltc desktop prepare --force` regenerated the local artifacts and
  passed configuration preflight. The existing Desktop remained running, so
  `launch_ready` and `callback_verified` remained false.

No automated tests were added or rerun for this follow-up. The earlier test
counts above apply to the implementation before this installation integration.
Fresh-install/upgrade execution on other machines remains unverified. These
changes belong to the `codex/macos-desktop-callback` preview branch.

## Cross-platform launcher packaging follow-up

The common `desktop.py` dispatcher now connects standard setup and the public
Desktop commands to both Mac and Windows adapters. Windows generates `.cmd`
and BOM-encoded `.ps1` entries tied to the installed Python runtime. Its native
Core wrapper is located in this distribution's installed file records, and its
four PowerShell/Python launch helpers ship in the wheel. Setup/prepare do not
create a Windows GUI process or run its native creation probe. Direct startup
remains the default; package-context is a saved explicit selection.

The final cross-platform wheel has SHA-256
`690879e99118b0ab5be8071fef823272ee876d7c86892255830e976bbdb3af40`.
Archive inspection found all five selected runtime modules and all four Windows
helper resources, matching the checkout bytes, with no developer-home paths.
Python 3.9 grammar parsing and `git diff --check` passed. Windows Git installation
also has a native `scripts/install_from_git.ps1` entry for pip followed by setup.

No tests were added or run for this follow-up. Work was performed on macOS;
PowerShell execution, a fresh native Windows installation, Desktop startup and
original-session callback/ACK have not been validated for this new setup path.
Earlier Windows bridge results remain evidence for the earlier build only.
This is a preview branch, not a fully accepted stable release.
