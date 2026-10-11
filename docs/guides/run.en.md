# Running commands

Use `ltc run` for training, builds, tests, or data processing. Ask the Agent to execute it in the current conversation so LTC can record the callback target.

## Submit a task

```bash
ltc run --cwd "$PWD" --task "Train the ResNet baseline" -- python train.py --config configs/resnet.yaml
```

`--cwd` sets the working directory. Relative paths for configuration, input data, and output directories start there. Choose a recognizable `--task` name to identify the run when its callback arrives.

LTC treats everything after `--` as the program and its arguments. The task inherits the submission environment, so activate the required environment first:

```bash
source .venv/bin/activate
ltc run --cwd "$PWD" --task "Run the tests" -- python -m pytest tests/
```

## Read the log

The submission output includes a `task log` path. Open that file in an editor, or read its latest lines in a terminal:

```bash
tail -n 60 /path/to/task/attempt-1.log
```

The callback includes the same path. Ask the Agent to inspect the exit code, log, and project artifacts, such as the test summary or a training checkpoint.

## Run a script

For a longer command sequence, put the steps in a script:

```bash
ltc run --cwd "$PWD" --task "Build the package" -- bash scripts/build.sh
```

For shell pipelines or redirection, invoke the shell explicitly:

```bash
ltc run --cwd "$PWD" --task "Generate the report" -- bash -lc 'python analyse.py && python report.py'
```

On Windows, you can use `powershell.exe -File scripts/build.ps1` as the task command.

## Choose an execution backend

The default `--backend auto` selects a backend for the operating system. On a server or container using GNU screen, choose it explicitly:

```bash
ltc run --backend screen --cwd "$PWD" --task "Train the model" -- python train.py
```

See [Installation](../installation.md) for backend setup and [Callbacks and ACK](callbacks.md) for handling completed work.
