# macOS

LTC supports macOS through the system launchd manager. The coordinator runs as a
user LaunchAgent, and each task has its own one-shot launchd job. The default
native backend does not need Homebrew screen or systemd.

## Install from this preview branch

Use Python 3.9 or newer and a logged-in macOS GUI session. A virtual environment
avoids modifying Homebrew's managed Python installation:

```bash
git clone --branch codex/0.7.0-preview https://github.com/lz59970062/long-task-wakeup.git
cd long-task-wakeup
python3 -m venv .venv
.venv/bin/python -m pip install .
source .venv/bin/activate
ltc setup --force --enable --now
```

Keep the installed virtual environment at that path; launchd uses its absolute
Python/runtime paths. The normal commands work unchanged:

```bash
ltc run --task "build project" -- make
ltc agent codex --task "review changes" -- "Review this repository's changes."
ltc done --task "external job" --exit-code 0
```

Run from the original Agent conversation's shell so its session can be detected,
or pass `--agent codex|claude --session <original-session-id>`. Setup installs the
skill and creates the daemon; it does not authenticate an Agent CLI for you.

## Inspect and repair

```bash
ltc install-launchd --print
launchctl print "gui/$(id -u)/codex-long-task-wakeup"
tail -f "${CODEX_HOME:-$HOME/.codex}/long-task-wakeup/daemon.log"
ltc doctor --agent codex --session <original-session-id>
```

The coordinator plist is `~/Library/LaunchAgents/codex-long-task-wakeup.plist`.
Installing that file registers it for future GUI logins; `--enable` also clears
any launchctl disabled override and `--now` loads it immediately. Custom names,
queues, Agent profile paths and daemon PATH are supported. Proxy values are read
from LTC's private proxy file, not embedded in the plist or printed by `--print`.

After updating the installation, rerun `ltc setup --keep-skill --force --enable --now`.
A verified live coordinator receives a reload request and waits for delivery workers
to drain before re-exec. Updated LaunchAgent environment/arguments apply on its next
load; changing those settings does not forcibly restart a live coordinator.
Unknown owner/process state is reported for inspection, never treated as a reason
to launch the task again.

One-shot task plists are saved beside `task.json`, never in `Library/LaunchAgents`.
They do not restart on failure or login. The coordinator removes exited job
registrations after reconciling terminal task records. Result files and callback
ACKs remain authoritative. Task cancellation still cancels callbacks only.

## Compatibility and lifetime

For an SSH/headless session without a GUI domain, select the bundled screen and
standalone coordinator explicitly:

```bash
ltc setup --backend screen --service standalone --now
ltc run --backend screen --task "build project" -- make
```

If screen is missing, install it with `brew install screen`. Apple's bundled
screen 4.0 works without the newer `-Logfile` option. Keep the same user, queue and
Agent profile across commands. `--enable` has no startup effect for standalone mode.

Tasks survive terminal closure and native coordinator restart during the GUI
session. Logout, reboot or shutting down the machine interrupts execution; sleep
pauses it. LTC neither prevents sleep nor automatically reruns interrupted work.
Inspect saved results and checkpoints before submitting follow-up work.

Local process identity and service control need the corresponding OS permissions.
Agent tool sandboxes may restrict these operations; configure the coordinator in
an ordinary terminal when necessary. macOS privacy permissions also apply to
files the LaunchAgent and workload access.

## Local validation

```bash
source .venv/bin/activate
test_root="$(mktemp -d /tmp/ltc-tests.XXXXXX)"
TMPDIR=/tmp CODEX_HOME="$test_root/codex" CLAUDE_CONFIG_DIR="$test_root/claude" \
CODEX_LONG_TASK_WAKEUP_TARGET_LOCK_DIR="$test_root/locks" \
LTC_TEST_LAUNCHD=1 LTC_TEST_SCREEN=1 python -m unittest discover -s tests -q
```

Use disposable `CODEX_HOME`, `CLAUDE_CONFIG_DIR` and
`CODEX_LONG_TASK_WAKEUP_TARGET_LOCK_DIR` directories for tests. Native integration
tests create uniquely named temporary launchd jobs and remove them afterward;
they use isolated profiles and never invoke a real model or touch an installed
coordinator. The LaunchAgent smoke test covers setup, doctor and in-place reload;
the lifetime test stops a coordinator while its task continues, then checks one
execution, a saved result, a bound callback and owner collection.
See the [local validation record](validation-macos.md) for the tested environment and limits.

Platform references: [Apple's launchd guide](https://developer.apple.com/library/archive/documentation/MacOSX/Conceptual/BPSystemStartup/Chapters/CreatingLaunchdJobs.html),
and the installed `launchctl(1)`, `launchd.plist(5)` and Darwin SDK `proc_info.h`.
