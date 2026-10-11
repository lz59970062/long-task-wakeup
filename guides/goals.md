# 跟踪多阶段目标

一项工作包含多个连续阶段时，用 Goal 记录进度。例如先训练模型，再评估结果，最后生成报告。

## 创建计划文件

在项目中创建 `goal-plan.yaml`：

```yaml
version: 1
revision: 1
goal: 完成基线实验报告
path:
  - id: train
    title: 训练基线模型
    status: in_progress
  - id: evaluate
    title: 评估模型并整理指标
    status: pending
  - id: report
    title: 生成实验报告
    status: pending
```

步骤状态使用 `pending`、`in_progress`、`blocked` 或 `completed`。按执行顺序维护这个列表，完成一步后，把下一步改为 `in_progress`。

## 绑定会话

让 Codex 在当前会话的工具环境中执行：

```bash
ltc goal start --id baseline-report --agent codex --session "$CODEX_THREAD_ID" \
  --cwd "$PWD" --task "完成基线实验报告" --plan-file goal-plan.yaml
```

提交相关任务时，附上这个目标的 ID：

```bash
ltc run --goal-id baseline-report --cwd "$PWD" --task "训练基线" -- python train.py
```

任务结束后，Agent 读取结果、确认回调并更新计划文件。活跃目标默认每三小时收到一次进度询问，帮助原会话继续处理当前步骤。

## 完成目标

所有产物检查完毕后，将计划中的步骤标记为 `completed`，然后运行：

```bash
ltc goal check --id baseline-report
```

复制输出中的计划摘要，完成确认：

```bash
ltc goal ack --id baseline-report --state completed --plan-sha256 <本次检查的摘要>
```

如果还在等待数据或外部条件，可以记录具体情况：

```bash
ltc goal ack --id baseline-report --state blocked_conditions --condition "等待数据集下载完成"
```

条件具备后，执行 `ltc goal resume --id baseline-report` 继续。计划变更时更新 `revision`，并在文件里记录变更原因。
