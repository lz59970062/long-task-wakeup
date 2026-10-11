# Use cases

Choose who will do the work: use `ltc run` for a program, `ltc agent` for an independent analysis or development task, and `ltc done` for work already running in a scheduler. Run the commands below from an Agent session configured with LTC, replacing file paths with those in your project.

## Evaluate a model after training

You are developing a model and want the Agent to read metrics, check the checkpoint, and arrange evaluation once training finishes.

```bash
ltc run --cwd "$PWD" --task "Train the baseline" -- python train.py --config configs/baseline.yaml
```

Before submission, tell the Agent:

> When training finishes, read the log and record the final metrics and checkpoint path. Check the artifacts, ACK the callback, then evaluate that checkpoint and write the results to reports/baseline.md.

The training program saves the checkpoint. The Agent uses the exit code and log to decide whether to start evaluation or address a training error. Use [Multi-stage goals](goals.md) to keep a record of each stage.

## Handle test or build failures

Submit a long test suite as a background task:

```bash
ltc run --cwd "$PWD" --task "Full test suite" -- python -m pytest tests/ --junitxml=reports/tests.xml
```

After receiving the result, the Agent opens the log and `reports/tests.xml`, identifies failed cases, and runs relevant tests after making changes. For a build, use `bash scripts/build.sh` and name the package paths to inspect in your handoff instructions.

## Ask another Agent to review code

From Codex, ask Claude to review a module:

```bash
ltc agent claude --cwd "$PWD" --task "Review data loading" -- "Read data_loader.py and its tests. Check missing files, empty data, and batch boundaries. Report locations, reproduction steps, and suggested fixes."
```

Claude's answer is saved in the task directory, and the result returns to the originating Codex conversation. Codex reads the report, checks the findings, and arranges changes. Save recurring review instructions in a [task template](templates.md).

## Receive results from Slurm or tmux

For a job already running on a cluster or in tmux, report at the end of its script:

```bash
ltc done --agent codex --session "YOUR_SESSION_ID" --task "Cluster training finished" \
  --exit-code "$job_exit" --message "Results are in /shared/experiments/baseline/"
```

Save `job_exit=$?` immediately after the business command exits. The submission host, reporting host, and coordinator use the same queue directory, and the receiving Agent needs access to the result path. See [External jobs](external-jobs.md) for a complete script.

## Check data-processing outputs

```bash
ltc run --cwd "$PWD" --task "Build a retrieval index" -- python build_index.py --output outputs/index
```

Specify checks in the handoff, such as comparing input and output counts, sampling ten records, and running a retrieval query. The Agent checks the artifacts when the callback arrives, then writes a short data-quality report.

## Run a custom Agent workflow

If your project exposes its Agent workflow through a Python entry point, submit that entry point as a command:

```bash
ltc run --cwd "$PWD" --task "Analyze a document collection" -- python workflows/analyse_documents.py
```

Have the script save its report and intermediate artifacts. Results return to the submitting Codex, Claude, or Pi conversation. To make the custom harness itself receive callbacks, implement a session adapter as described in [Harness support](../integrations/support.md#add-a-harness).

## Choose a callback route

| Current environment | Recommended workflow |
| --- | --- |
| Codex CLI | Submit in the current conversation; the coordinator resumes that session |
| Claude Code CLI or desktop Code session | Run the printed `ltc wait` command as a background Bash task |
| Pi | Install the extension for live callbacks; launch through `ltc pi` for recovery after closing |
| Codex Desktop | Use the preview shared Core launcher, then check delivery with a small task |

See [Harness support](../integrations/support.md) for versions and validation records.
