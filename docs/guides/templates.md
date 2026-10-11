# 编写任务模板

把经常重复的任务要求保存为 YAML，下次提交时只补充本次的文件和目标。

## 写一个审查模板

在项目中创建 `review.yaml`：

```yaml
version: 1
prompt: |
  阅读提供的需求和代码，检查会影响实际运行的问题。
  每个问题给出文件位置、触发条件、预期行为和实际行为。
  以审查报告交付结果。
handoff: |
  逐项核对审查结果，确认问题后再安排修改和测试。
```

`prompt` 是子 Agent 的任务要求，`handoff` 是返回原会话后的处理要求。

## 使用模板

先预览展开后的提示词：

```bash
ltc agent codex --template-file ./review.yaml --dry-run -- "检查 parser.py 和 tests/test_parser.py。"
```

查看输出后，提交任务：

```bash
ltc agent codex --template-file ./review.yaml --cwd "$PWD" --task "解析器审查" -- "检查 parser.py 和 tests/test_parser.py。"
```

`--` 后面的文字会追加到模板提示词中。LTC 在提交时保存展开后的内容，便于追溯这次任务使用的要求。

## 在多个项目中复用

将文件保存为 `~/.config/ltc/templates/review.yaml`，就可以按名称调用：

```bash
ltc agent codex --template review --task "审查当前改动" -- "阅读当前 git diff，检查行为变化。"
```

## 注册为 Agent 技能

`0.7.1` 预览版支持把模板注册为 Agent 可发现的技能：

```bash
ltc template register review --file ./review.yaml --worker codex --description "审查代码并报告可复现的问题"
ltc template list
```

注册后，Codex 可以使用 `$ltc-review`，Claude Code 可以使用 `/ltc-review` 调用它。注册字段、文件位置和更新方式见[模板注册资料](https://github.com/lz59970062/long-task-wakeup/blob/main/docs/template-registration.md)。
