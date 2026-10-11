# External jobs

For work already started by Slurm, tmux, or another runtime, call `ltc done` at the end to deliver the result to an Agent conversation.

## Report a completed run

This command creates a callback from the original Agent session:

```bash
ltc done --cwd "$PWD" --task "Build the data index" --exit-code 0 --message "The index is saved in outputs/index.json"
```

Pass the actual exit code in `--exit-code` and the artifact location or handoff instructions in `--message`. The receiving Agent can then open the file and continue using the result.

## Add it to a job script

Insert the original session ID in your script. Save the business command's exit code before reporting the result to LTC. For Bash:

```bash
#!/usr/bin/env bash
set +e
python train.py --config configs/exp.yaml
job_exit=$?
ltc done --agent codex --session "YOUR_SESSION_ID" --cwd "$PWD" \
  --task "Slurm training" --exit-code "$job_exit" \
  --message "Logs and checkpoints are saved in outputs/exp/"
exit "$job_exit"
```

Read `CODEX_THREAD_ID` in the originating Codex tool environment to obtain the session ID. Claude Code uses `CLAUDE_CODE_SESSION_ID`. Save that value in the submitted script to identify the receiving conversation.

For Slurm, LTC, the coordinator, and the receiving Agent need access to the same queue and session files. With a shared directory, pass the same `--queue-dir` to `setup`, `done`, and `ack`.

## Set up a callback service

To configure a machine for reporting external jobs, use:

```bash
ltc setup --callback-only --enable --now
```

See [Callbacks and ACK](callbacks.md) for handling the result.
