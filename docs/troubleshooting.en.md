# Troubleshooting

## The terminal cannot find ltc

Activate the virtual environment where you installed LTC, then check the command:

```bash
source /path/to/long-task-wakeup/.venv/bin/activate
ltc --version
```

On Windows, call `.venv\Scripts\ltc.exe` inside the installation directory. The coordinator also uses the Python path saved during setup. Run `ltc setup` again after moving the environment.

## Submission reports missing session information

Ask the Agent to run `ltc run` through its current session's tool environment. When submitting from a separate terminal or job script, pass `--agent` and `--session`:

```bash
ltc run --agent codex --session "YOUR_SESSION_ID" --task "Build" -- bash scripts/build.sh
```

See [Codex CLI](integrations/codex.md) and [External jobs](guides/external-jobs.md) for session binding.

## The task finished, but the callback has not arrived

Inspect the callback queue and the session configuration:

```bash
ltc status --state pending --state running --state failed
ltc doctor --agent codex --session <session-id>
```

Use the report to choose the next step:

| Finding | Next step |
| --- | --- |
| The coordinator has not started | Run the `setup` command suggested in the report |
| A Claude Code session is open | Ask Claude to run the printed `ltc wait` command as a background Bash task |
| The Codex Desktop connection needs setup | Open the app through the [Desktop launcher](integrations/desktop.md) |
| The callback is in `failed` | Read the error record, fix the cause, and use `ltc retry --id <callback-id>` in the preview version |

The original submission output contains the task's log path. Open the log and artifacts to inspect the task result directly.

## The same callback appears repeatedly

After reading the result, run the complete ACK command from the callback. Check its ID and `--queue-dir` so the acknowledgement reaches the same queue.

## Resuming training after a restart

Find the task log, result file, and latest checkpoint. Ask the Agent to inspect the artifacts, then submit a new task using the training program's resume option:

```bash
ltc run --cwd "$PWD" --task "Resume training" -- python train.py --resume outputs/checkpoint.pt
```

Here, `--resume` belongs to the example training program. Use the option and checkpoint path for your own framework.

## Reporting a problem

Open a [GitHub issue](https://github.com/lz59970062/long-task-wakeup/issues) with your operating system, LTC version, submission command, error text, and relevant log excerpts. Include a small reproduction command to help locate the failing step.
