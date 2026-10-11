# Multi-stage goals

Use a Goal to track a sequence of work, such as training a model, evaluating it, and generating a report.

## Create a plan

Create `goal-plan.yaml` in the project:

```yaml
version: 1
revision: 1
goal: Complete the baseline experiment report
path:
  - id: train
    title: Train the baseline model
    status: in_progress
  - id: evaluate
    title: Evaluate the model and collect metrics
    status: pending
  - id: report
    title: Generate the experiment report
    status: pending
```

Step statuses are `pending`, `in_progress`, `blocked`, and `completed`. Keep the list in execution order. After finishing one step, mark the next one `in_progress`.

## Bind the goal to a session

Ask Codex to execute this in the current session's tool environment:

```bash
ltc goal start --id baseline-report --agent codex --session "$CODEX_THREAD_ID" \
  --cwd "$PWD" --task "Complete the baseline report" --plan-file goal-plan.yaml
```

Attach the goal ID when submitting related tasks:

```bash
ltc run --goal-id baseline-report --cwd "$PWD" --task "Train the baseline" -- python train.py
```

When the task finishes, the Agent reads the result, acknowledges the callback, and updates the plan. By default, an active goal sends a progress check every three hours to help the original conversation continue the current step.

## Complete the goal

Once the artifacts have been checked, mark every plan step `completed` and run:

```bash
ltc goal check --id baseline-report
```

Copy the plan digest from the output and confirm completion:

```bash
ltc goal ack --id baseline-report --state completed --plan-sha256 <plan-digest>
```

If the work is waiting for data or another external condition, record that condition:

```bash
ltc goal ack --id baseline-report --state blocked_conditions --condition "Waiting for the dataset download"
```

Run `ltc goal resume --id baseline-report` when it is ready to continue. When the plan changes, update `revision` and record the reason in the file.
