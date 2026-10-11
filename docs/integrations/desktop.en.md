# Codex Desktop

Codex Desktop integration uses the shared Core launcher in the `0.7.1` preview. Desktop and LTC access sessions through the same Core, which delivers callbacks into the desktop app when tasks finish.

## Generate a launcher

Install the [preview](../installation.md#install-the-preview) on macOS or Windows, then run:

```bash
ltc setup --force --enable --now
ltc desktop status
```

macOS generates `Start LTC Desktop.command`. Windows generates `Start LTC Desktop.cmd` and a PowerShell script. The default location is the `long-task-wakeup` folder inside your Codex configuration directory.

## Start Desktop

Save your work and quit Desktop. Double-click the generated launcher, or run:

```bash
ltc desktop launch
```

Submit a small task from that Desktop session:

```bash
ltc run --callback-mode desktop --cwd "$PWD" --task "Desktop callback example" -- python -c "print('Desktop callback ready')"
```

Ask the Agent to read the result and run the included ACK command to check the integration. In a PowerShell tool environment, use `--cwd "$($PWD.Path)"` in place of `--cwd "$PWD"`.

## Choose an app path

On macOS with multiple app installations, select one explicitly:

```bash
ltc desktop prepare --app /Applications/Codex.app --force
```

Windows uses direct launch by default. The experimental Store package-context option is configured with:

```powershell
ltc setup --desktop-launch-mode package-context --force --enable --now
```

See the [macOS Desktop](https://github.com/lz59970062/long-task-wakeup/blob/main/docs/macos-desktop-bridge.md) and [Windows Desktop](https://github.com/lz59970062/long-task-wakeup/blob/main/docs/windows-desktop-bridge.md) notes for platform setup and validation records. After an app upgrade, run the small example again to check callbacks.
