# macOS Desktop shared Core — experimental 0.7.1 preview

This opt-in launcher lets Desktop and LTC use one Core through a private Unix
socket. It preserves Desktop's bundled Core, configuration arguments and fresh
App Tools environment. It changes the listening transport and relays Desktop's
stdio JSONL traffic without interpreting tool requests, approvals or JSON-RPC IDs.

The installed Desktop build accepts `CODEX_CLI_PATH` and
`CODEX_APP_SERVER_FORCE_CLI`. These were inspected in local app code; they are
not a stable public Desktop configuration promise. Recheck after app upgrades.
The official [App Server protocol](https://learn.chatgpt.com/docs/app-server)
describes stdio and Unix socket transports. Availability of those transports
does not establish that an already running Desktop uses the shared listener.

## Install and generate the launcher

**Use branch `codex/macos-desktop-callback` for this preview.** Stable `v0.7.0`
does not include the Mac launcher generator described here.
From a checkout of that branch, install the package and run standard
setup; no separate launcher preparation is required:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install .
.venv/bin/ltc setup --force --enable --now
source .venv/bin/activate
```

On Mac, setup generates a double-click launcher named
`Start LTC Desktop.command`, a private Core wrapper and settings under
`${CODEX_HOME:-~/.codex}/long-task-wakeup`. Once an app is selected, its settings
are saved in `desktop.json`. The wrapper and launcher use the current installed
Python/runtime, so they work independently of the caller's working directory or
PATH. The implementation ships in the Python package; it does not require a
developer to write a separate launcher for each user.

Absolute paths in these generated files identify this machine's profile,
installation and Desktop app. They are calculated during setup, not copied from
the developer's home directory. Install and generate them on each machine rather
than copying `.command` or `desktop.json` files between machines. Keep the Python
environment in place, or rerun setup after moving or replacing it.

Plain `pip install` installs the package and command entry points; it does not
run a hook that changes user configuration. Follow it with `ltc setup` as above.
The preview's `scripts/install_from_git.sh` provides a combined Git installation:

```text
LTC_PYTHON=/absolute/path/to/python bash scripts/install_from_git.sh <github-https-url> [subdirectory] -- [setup flags]
```

The helper runs pip and `setup --force --enable --now` with the same interpreter.
`LTC_PYTHON` is optional; additional setup flags go after `--`. Select the preview
with `@codex/macos-desktop-callback` in the Git URL. Installing `v0.7.0`
continues to install that release and does not supply this generator.

### App discovery and customization

Setup looks for valid `Codex.app` and `ChatGPT.app` bundles in `/Applications`
and `~/Applications`. It preserves an existing app selection and an explicitly
configured custom wrapper. When several valid apps are available, select the
intended one; setup does not guess:

```bash
ltc setup --desktop-app /path/to/Codex.app --force --enable --now
```

Use `ltc setup --no-desktop-launcher` to skip launcher generation. If Desktop is
not installed yet, CLI setup and launcher generation still complete. After
installing Desktop, open the launcher or run `ltc desktop prepare` to discover it.

For a later manual change, `ltc desktop prepare --app /path/to/Codex.app --force`
selects an app. `--wrapper /absolute/path/to/ltc-desktop-core` chooses a custom
wrapper; omit it to preserve a configured wrapper, or use the generated wrapper
when preparing a new configuration. Standalone
`prepare --force` explicitly replaces existing Desktop settings. Standard setup
preserves existing app/custom-wrapper choices while refreshing its generated
installation paths.

Preparation does not quit Desktop, edit installed app files, change global
environment variables or replace the Dock icon. `configuration_valid` and
`launch_ready` are different fields; a running Desktop blocks launch even when
the saved configuration is valid.

The current implementation requires the encoded socket path beneath the selected
`CODEX_HOME` to fit macOS's Unix socket path limit. A long custom profile path can
prevent Desktop configuration; setup reports this limit. CLI use is independent
of Desktop readiness.

## Activate at an idle restart

Save/finish active Desktop work and quit the app. Then double-click the generated
launcher, or run:

```bash
ltc desktop launch --check-only
ltc desktop launch
```

The launcher refuses an existing Desktop process. It passes the override only
to the new app, which starts the wrapper with fresh tool-pipe settings. Ordinary
Dock startup continues to use the ordinary app connection; use the LTC launcher
when Desktop automatic callbacks are needed.

After startup, open the **original** conversation, check a normal Desktop App
Tools operation, then run:

```bash
ltc desktop status
ltc doctor --agent codex --session <original-session-id> --callback-mode desktop
```

The mode's metadata path travels with each newly submitted task, so it does not
depend on changing the environment of an already running LaunchAgent. The Core
PID, creation identity, socket peer PID/UID and profile are checked on delivery.
Only Unix sockets are exposed; no TCP port or transport token is required.

Desktop stdio EOF or a normal wrapper termination stops only its owned Core,
closes all callback clients and removes its own endpoint metadata. Independent
launchd workloads remain independent. Forced wrapper kill/power loss may leave
state: inspect the recorded process identity and socket before cleanup. Launch
refuses existing state; it does not delete unknown sockets or writer locks.

To recover a previously completed result after the connection is verified:

```bash
ltc retry --id <original-callback-id> --callback-mode desktop
```

Do not restart its business task. Inspect the delivered artifacts and ACK from
the resumed original conversation. A launcher success or protocol probe alone
does not count as this acceptance.

## Validation status

On 2026-09-27 the real bundled Core `0.155.0-alpha.16.4` passed an isolated probe:
Desktop JSONL created a disposable thread; a second verified Unix client resumed
that same thread; stdin EOF stopped the owned Core and removed socket/metadata.
The probe used an empty temporary profile, no credentials and no model turns.

```bash
LTC_TEST_MACOS_DESKTOP=1 PYTHONPATH=src .venv/bin/python -m unittest discover \
  -s tests -p test_desktop_macos_integration.py -v
```

**Live Desktop startup, App Tools, original-session receipt/ACK and restart
acceptance are still pending.** They require activating the launcher at an idle
Desktop restart. Native Windows acceptance remains the separately recorded
Windows bridge result; this Mac probe does not revalidate Windows.
