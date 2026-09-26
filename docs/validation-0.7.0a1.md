# 0.7.0a1 preview validation

Validated locally on 2026-09-25, with lifecycle self-checks revalidated on
2026-09-26. Tests were authored by a separate agent and run
by the maintainer. No production LTC daemon was restarted, no real model call
was made, and no GPU was used.

| Check | Observed result |
| --- | --- |
| Full source suite, Python 3.12.4 | 265 discovered: 261 passed, 4 opt-in integration tests skipped |
| Lifecycle diagnostics | Passed independent cases for missing/unknown owners, per-task observation isolation, completion races, absent handoff, unresolved delivery, ACK/cancel history, idle queues and unsupported platforms |
| Installed wheel, real systemd user manager | Passed: stopping the coordinator service leaves its independently owned task running |
| Installed wheel, unavailable user bus | Passed: killing the standalone coordinator leaves its screen-owned task running |
| Installed wheel, real standalone startup | Passed: a stale PID pointing to an unrelated live process neither receives a signal nor prevents coordinator startup |
| Installed wheel, generated configuration repair | Passed: doctor reports missing configuration; its repair command installs the skill and starts a real standalone coordinator; its recheck reports local readiness |
| Non-root Docker, network disabled at runtime | Passed screen lifetime, stale-PID startup and generated configuration repair tests |
| Wheel outside the source tree | Version `0.7.0a1`, package import and bundled skill resource verified |
| Skill and source hygiene | Bundled/root skill files match; skill validation and `git diff --check` passed |

Both real lifetime tests execute a workload exactly once in their fixture, retain
its exit code 7 and artifact, and create one callback for the specified fixture
session. This verifies process ownership, result persistence and callback queuing;
it does not establish exactly-once execution or live delivery to an authenticated
Agent. Callback delivery remains at-least-once.

The real screen test also checks lifecycle diagnostics while the coordinator is
gone but the task remains alive, and after the result/callback are saved and the
task owner has exited. Neither is reported as a task-owner fault. These observations
passed both from the isolated wheel installation and inside Docker. macOS/Windows
diagnostic classification is covered by simulated platform tests, not native runs.

The host used Linux x86_64, systemd 245 and GNU screen 4.08. The local Docker build
used the example Dockerfile with an existing Debian/Ubuntu-compatible base image
(`runtime-cuda124:latest`, Python 3.11); its CUDA capabilities were not used.
The default `python:3.12-slim` build is configured in CI but was not exercised in
this local validation. CI also includes Python 3.9 and 3.12; a workflow definition
is not a completed cloud run.

## Reproduce

Run ordinary tests with a private test lock directory:

```bash
CODEX_HOME=/tmp/ltc-preview-test-profile \
  CLAUDE_CONFIG_DIR=/tmp/ltc-preview-claude-profile \
  CODEX_LONG_TASK_WAKEUP_TARGET_LOCK_DIR=/tmp/ltc-preview-test-locks \
  PYTHONPATH=src python3 -m unittest discover -s tests -q
```

Real tests require local permission to create temporary user services and screen
sessions. They use temporary queues and do not contact Agent providers:

```bash
LTC_TEST_SYSTEMD=1 LTC_TEST_SCREEN=1 LTC_TEST_DIAGNOSTICS=1 \
  CODEX_HOME=/tmp/ltc-preview-integration-profile \
  CODEX_LONG_TASK_WAKEUP_TARGET_LOCK_DIR=/tmp/ltc-preview-test-locks \
  PYTHONPATH=src python3 -m unittest discover -s tests \
    -p 'test_*integration.py' -v
```

To verify an installed wheel, replace `PYTHONPATH=src` with an isolated wheel
installation path. The worker entry point must resolve into that same installation.
For the container check, build the [example image](../examples/docker/Dockerfile)
and provide a readable copy of the independent test:

```bash
mkdir -p /tmp/ltc-preview-container-tests
cp tests/test_screen_integration.py /tmp/ltc-preview-container-tests/
cp tests/test_diagnostics_integration.py /tmp/ltc-preview-container-tests/
chmod 755 /tmp/ltc-preview-container-tests
chmod 644 /tmp/ltc-preview-container-tests/test_screen_integration.py
chmod 644 /tmp/ltc-preview-container-tests/test_diagnostics_integration.py
docker run --rm --init --network none \
  -v /tmp/ltc-preview-container-tests:/tests:ro \
  -e LTC_TEST_SCREEN=1 -e LTC_TEST_DIAGNOSTICS=1 \
  ltc:0.7.0a1 python -m unittest discover -s /tests \
    -p 'test_*integration.py' -v
```

## Callback size and remaining platform work

One representative callback with an 8-character task ID, session UUID, command,
log path and managed-task message measured 1,364 characters with the old full
wrapper, 461 with the ordinary compact envelope and 583 with its periodic system
reminder. That is a character measurement, not a model-token measurement or a
universal limit. Filesystem paths and user hooks affect size; custom instructions
remain available in the details artifact and require reading when flagged.

AutoDL deployment uses the tested non-systemd Linux path, but an actual AutoDL
instance, its storage retention and authenticated Agent callback have not been
validated. Native macOS, native Windows, PI and DSH adapters and npm/frozen
distribution remain future work. The [architecture handoff](architecture.md)
defines their integration boundaries; [container instructions](containers.md)
describe current deployment and process-lifetime limits.
