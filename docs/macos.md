# macOS

For the 0.7.1 preview's Desktop automatic callback configuration, see
[Mac Desktop shared Core](macos-desktop-bridge.md). OS task execution and
[callback modes](callback-modes.md) have separate readiness and acceptance.
Use branch `codex/macos-desktop-callback` for this preview. Stable
`v0.7.0` does not include its automatic launcher setup.

LTC supports macOS through the system launchd manager. The coordinator runs as a
user LaunchAgent, and each task has its own one-shot launchd job. The default
native backend does not need Homebrew screen or systemd.

## Install 0.7.0

Use Python 3.9 or newer and a logged-in macOS GUI session. A virtual environment
avoids modifying Homebrew's managed Python installation:

```bash
git clone --branch v0.7.0 --depth 1 https://github.com/lz59970062/long-task-wakeup.git
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

## Install the 0.7.1 preview branch

Clone the preview branch, install into your chosen virtual environment and run
the standard setup step:

```bash
git clone --branch codex/macos-desktop-callback https://github.com/lz59970062/long-task-wakeup.git
cd long-task-wakeup
python3 -m venv .venv
.venv/bin/python -m pip install .
.venv/bin/ltc setup --force --enable --now
source .venv/bin/activate
```

On Mac, this setup also generates `Start LTC Desktop.command` and a private Core
wrapper under `${CODEX_HOME:-~/.codex}/long-task-wakeup`. The launcher is bound to
this installation's Python runtime. Its absolute paths are calculated locally;
no username or checkout location is hardcoded in the distributed source. Generate
it on each machine, and rerun setup after moving or replacing the environment.
Plain pip installation has no user-configuration hook, so the setup command is
required for a complete installation.

Setup discovers valid `Codex.app` and `ChatGPT.app` bundles in `/Applications`
and `~/Applications`, preserving an existing app or custom wrapper selection.
If several valid apps are found, choose one with
`ltc setup --desktop-app /path/to/Codex.app --force --enable --now`.
Use `--no-desktop-launcher` to skip generation. Desktop does not need to be
installed for CLI setup or launcher generation; after installing it, open the
launcher or run `ltc desktop prepare` to complete discovery.

The preview's Git installation helper combines pip and setup using one Python:

```text
LTC_PYTHON=/absolute/path/to/python bash scripts/install_from_git.sh <github-https-url> [subdirectory] -- [setup flags]
```

It runs `setup --force --enable --now`; extra setup flags follow `--`, for example
`--desktop-app /path/to/Codex.app`. Select `codex/macos-desktop-callback` in the
Git URL with `@codex/macos-desktop-callback`. The stable `v0.7.0` commands above
continue to install the stable release without this new generator.

Generation does not start or restart Desktop. Activating the shared Core remains
an explicit launch after finishing active Desktop work. See the
[Desktop guide](macos-desktop-bridge.md) for that step, custom configuration,
the Unix socket path-length limit and pending acceptance checks.

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
