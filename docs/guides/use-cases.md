# 典型使用场景

先选择工作由谁执行：普通程序交给 `ltc run`，独立的分析或开发工作交给 `ltc agent`，已在调度系统中运行的作业通过 `ltc done` 上报。下面的命令在已配置 LTC 的 Agent 会话中执行，文件名替换为自己项目中的实际路径。

## 训练结束后接着评估

你正在调试一个模型，希望训练完成后由 Agent 读取指标、检查 checkpoint，并安排评估。

```bash
ltc run --cwd "$PWD" --task "训练基线模型" -- python train.py --config configs/baseline.yaml
```

提交前告诉 Agent：

> 训练结束后读取日志，记录最终指标和 checkpoint 路径。确认产物后 ACK，再使用这个 checkpoint 运行评估，把结果写入 reports/baseline.md。

训练程序负责保存 checkpoint。Agent 根据回调中的退出码和日志决定继续评估，还是先处理训练错误。需要持续记录每个阶段时，使用[多阶段目标](goals.md)。

## 测试或构建完成后处理失败项

完整测试需要较长时间时，可以提交一次后台运行：

```bash
ltc run --cwd "$PWD" --task "完整测试" -- python -m pytest tests/ --junitxml=reports/tests.xml
```

Agent 收到结果后打开日志和 `reports/tests.xml`，定位失败用例，再对修改过的部分运行测试。构建也采用相同流程：将命令换成 `bash scripts/build.sh`，并在交接要求中写明需要检查的安装包路径。

## 让另一个 Agent 审查代码

在 Codex 中让 Claude 检查一个模块：

```bash
ltc agent claude --cwd "$PWD" --task "审查数据加载" -- "阅读 data_loader.py 和相关测试，检查缺失文件、空数据和批次边界。给出问题位置、复现步骤和修复建议。"
```

Claude 的回答保存在任务目录中，结果回到发起请求的 Codex 会话。Codex 读取报告、核对问题并安排修改。将固定的审查要求写成[任务模板](templates.md)，下次只需补充本次文件。

## 接收 Slurm 或 tmux 作业的结果

作业已经在集群或 tmux 中执行时，在脚本结尾上报：

```bash
ltc done --agent codex --session "填入原会话ID" --task "集群训练完成" \
  --exit-code "$job_exit" --message "结果保存在 /shared/experiments/baseline/"
```

`job_exit` 在业务命令退出后立即用 `job_exit=$?` 保存。发起端、上报端和协调器使用同一个队列目录，接收回调的 Agent 能读取结果路径。完整脚本见[接入已有任务](external-jobs.md)。

## 数据处理结束后检查产物

```bash
ltc run --cwd "$PWD" --task "生成检索索引" -- python build_index.py --output outputs/index
```

在交接要求中指定检查方法，例如核对输入和输出条数、抽查十条记录、运行一次检索查询。Agent 收到回调后按这些步骤检查产物，再生成简短的数据质量报告。

## 运行自定义 Agent 工作流

如果项目把自己的 Agent 工作流封装成 Python 入口，可以把它作为普通命令提交：

```bash
ltc run --cwd "$PWD" --task "分析文档集合" -- python workflows/analyse_documents.py
```

在脚本中保存报告和中间产物，结果回到提交它的 Codex、Claude 或 Pi 会话。需要让自定义 harness 自己接收回调时，按[框架接入说明](../integrations/support.md#接入新的-harness)实现会话适配器。

## 选择回调入口

| 当前使用的环境 | 推荐操作 |
| --- | --- |
| Codex CLI | 在当前会话提交，让协调器恢复同一个会话 |
| Claude Code CLI 或桌面 Code 会话 | 提交后把输出中的 `ltc wait` 命令作为后台 Bash 任务运行 |
| Pi | 安装扩展，在当前 Pi 进程接收回调；需要关闭后恢复时从 `ltc pi` 启动 |
| Codex Desktop | 使用预览版共享 Core 启动器，再运行小任务检查回调 |

对应版本和验证记录见 [Harness 支持情况](../integrations/support.md)。
