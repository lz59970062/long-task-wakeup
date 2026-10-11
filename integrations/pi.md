# Pi

Pi integration uses the `0.7.1` preview. Pi can receive results for its own tasks and work as a child Agent on independent assignments.

## Install the extension

Follow [preview installation](../installation.md#install-the-preview), then run:

```bash
ltc setup --with-pi-extension --enable --now
```

Run `/reload` in an existing ordinary Pi session, or restart Pi. The extension and skill are installed in the Pi configuration directory, which defaults to `~/.pi/agent`.

## Submit from Pi

Ask Pi to run:

```bash
ltc run --cwd "$PWD" --task "Prepare the data" -- python prepare_data.py
```

LTC binds the task to the session named by `PI_SESSION_FILE`. When the result arrives, the extension delivers the callback into that Pi process. An idle Pi starts handling it immediately; a busy Pi receives it after the current tool work finishes.

After reading the artifacts, ask Pi to execute the callback's ACK command.

## Launch Pi through LTC

To allow session recovery after Pi closes, start a managed session:

```bash
ltc pi --cwd "$PWD"
```

To continue an existing session, provide its absolute path:

```bash
ltc pi --session /absolute/path/to/session.jsonl
```

## Give work to Pi

Submit from the original Agent conversation:

```bash
ltc agent pi --cwd "$PWD" --task "Review data preparation" -- "Read prepare_data.py. Check missing values, duplicate records, and output formats. Report specific issues."
```

The answer is saved in the LTC task directory. Its path appears in both the submission output and callback. See the [Pi implementation notes](https://github.com/lz59970062/long-task-wakeup/blob/main/docs/pi.md) for model options, system prompts, and session recovery.
