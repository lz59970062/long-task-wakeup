# Your first task

Run a ten-second task, receive the result in the original Agent conversation, then ask the Agent to read the log and acknowledge the callback.

These steps use LTC 0.7.0, Linux, and Codex CLI. Prepare Python 3.9 or later, Git, and a signed-in Codex CLI. Linux tasks run under systemd user services or GNU screen. See [Installation](installation.md) for other environments.

## Install LTC

Run in your terminal:

```bash
git clone --branch v0.7.0 --depth 1 https://github.com/lz59970062/long-task-wakeup.git
cd long-task-wakeup
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install .
ltc setup --enable --now
ltc --version
```

`setup` installs the Agent skills and starts the background coordinator. The last command should print `0.7.0`.

Keep the virtual environment at this location, since the coordinator uses its Python interpreter. Start Codex CLI from the current terminal. When working in your own projects later, activate this environment before starting Codex.

## Submit your first task

In your Codex CLI conversation, ask Codex to execute this command. LTC reads the callback target from the session environment.

```bash
ltc run --cwd "$PWD" --task "My first LTC task" \
  -- python3 -c "import time; time.sleep(10); print('LTC demo finished')"
```

`--task` names the task, `--cwd` sets its working directory, and the command follows `--`. LTC prints the task ID, execution backend, and log path, then returns. The Python program waits ten seconds in the background, prints one line, and exits.

## Read the result and acknowledge it

LTC sends the callback to the same conversation. It includes the task status, log path, and ACK command. Ask Codex to open the log. You should see:

```text
LTC demo finished
```

After reading the result, ask Codex to execute the complete ACK command from the callback. For the default queue, it looks like this:

```bash
ltc ack --id <callback-id>
```

Replace `<callback-id>` with the ID in that callback. ACK records receipt and ends reminders for this callback.

To inspect pending callbacks, run:

```bash
ltc status
```

## Run your own task

Replace the sample Python command with your training command. Ask the Agent to run it in the project directory:

```bash
ltc run --cwd "$PWD" --task "Train the model" \
  -- python3 train.py --config configs/exp.yaml
```

Choose a recognizable name, such as “Train the ResNet baseline” or “Build the Linux package.” The Agent can use that name, the log, and the output files to continue after receiving the result.

To give a code review to a child Agent, use:

```bash
ltc agent codex --cwd "$PWD" --task "Review the current changes" \
  -- "Read the current git diff. Check for issues that could cause runtime failures, and report the file locations and causes."
```

LTC prints the answer-file path and calls back to the original conversation when the child finishes. See [Task templates](guides/templates.md) to reuse these instructions.

Next, read [Architecture](overview.md) to see how tasks and callbacks move through LTC.
