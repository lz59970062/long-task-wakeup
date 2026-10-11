# Callbacks and ACK

A callback tells the Agent which task finished, where its results are, and how to acknowledge receipt. The Agent reads the results first, then runs the included ACK command.

## Read the callback

| Information | How to use it |
| --- | --- |
| Task name and status | Identify the work and its execution state |
| Log path | Read command output and errors |
| Answer or artifact path | Open the child Agent's answer, report, or result files |
| Details path | Read the complete command and handoff instructions |
| ACK command | Record that the result has been received and checked |

Callbacks use a compact format by default. Add `--callback-format full` when submitting to include the details directly in the callback text.

## Acknowledge receipt

Run the complete command provided in the callback. Its default form is:

```bash
ltc ack --id <callback-id>
```

For a custom queue, the printed command also includes `--queue-dir`. Keep it so the acknowledgement is written to that queue.

## Inspect pending callbacks

```bash
ltc status
ltc status --state pending --state failed
```

`pending` means a callback is waiting for delivery. For entries in `failed`, read the associated error record. In the `0.7.1` preview, you can retry delivery after fixing the connection:

```bash
ltc retry --id <callback-id>
```

This sends the saved result again. To execute another business command, submit a new task with `ltc run`.

## Customize the handoff

Edit `~/.ltc/callback-hook.md`, for example:

```text
After reading the result, tell me the artifact paths and main findings before continuing.
For experiments, include the configuration file and key metrics.
```

Include this text with every callback:

```bash
ltc prompt-policy --user-every 1
```

Run `ltc prompt-policy` to inspect the current intervals. See [Configuration](../reference/configuration.md) for file paths and environment variables.
