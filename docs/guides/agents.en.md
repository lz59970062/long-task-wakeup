# Running child agents

Give a child Agent a task you can describe independently, such as reviewing a module, organizing experiment results, or implementing a small feature.

## Submit a code review

```bash
ltc agent codex --cwd "$PWD" --task "Review argument parsing" -- "Read parser.py and its tests. Check empty input, invalid arguments, and boundary cases. Report file locations, reproduction steps, and suggested fixes."
```

`codex` selects the child Agent. The text after `--` describes its work. Name the files to read, the expected output, and the criteria for checking the result.

LTC prints the log path and answer-file path. When the child finishes, the callback returns to the submitting conversation. The original Agent reads the answer, checks the relevant files, and continues.

## Choose a child Agent

| Command | Worker | Version |
| --- | --- | --- |
| `ltc agent codex` | Codex CLI | 0.7.0 |
| `ltc agent claude` | Claude Code CLI | 0.7.0 |
| `ltc agent pi` | Pi | 0.7.1 preview |

Install and sign into the selected CLI in the submission environment. The child inherits that environment's configuration, proxy settings, and project paths.

For example, ask Claude to review code from a Codex conversation:

```bash
ltc agent claude --cwd "$PWD" --task "Review data loading" -- "Check file reading, batch assembly, and error handling in data_loader.py. Report reproducible problems."
```

The result returns to the submitting Codex conversation. The submitting session determines the callback target; the name after `agent` selects the worker.

## Define the handoff

End the prompt with the information you want back, for example:

> List the files you changed, the test commands you ran, their results, and what needs to be checked next.

Save repeated instructions as a [task template](templates.md).
