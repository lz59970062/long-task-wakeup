# LTC test template review
Scope: release 0.6.5 from preview; add `ltc agent codex --template test -- <requirements>` with child model/effort overrides. Default test profile gpt-5.6-luna / max (verify local CLI). Child independently writes tests from requirements, does not execute tests or modify production; parent executes on callback. Template prompt is guidance, not filesystem isolation. Preserve generic agent commands and callback targeting. No remote publication assumed.
Panel: Ada, pragmatic API designer (small compatible interface); Lin, skeptical testing specialist (independent behavioral oracle); Sam, reliability engineer (durable metadata and callback failure handling).

## Round 1: Independent Reviews

**Ada — 初始投票：有条件赞成。** 该方案可以小幅扩展现有 Agent 接口，不必新增顶层命令。

1. **明确模板参数，保留已有提示词解析。** 推荐 `ltc agent codex --template test -- <requirements>`。当前 `argparse.REMAINDER` 会把第一个位置参数之后的内容视为提示词，因此不要偷偷把裸 `test` 解释为模板，否则已有 “test …” 提示词会改变语义。模板名用 `choices` 验证，未知模板立即失败。
2. **子代理模型与回调代理严格分开。** 在 child parser 上增加 `--model`、`--effort`，不要放入 `add_common_flags`。现有 `--agent` 表示回调目标，已经容易混淆；help 必须明确新增参数只影响 child。普通无模板命令应维持原有 argv，不注入 Luna 默认值。
3. **默认值按 worker 和模板解析。** `test + codex` 可默认 Luna/max，但不能把 Codex 模型名传给 Claude。Claude 默认值应明确继承其 CLI 配置，或者对暂不支持的组合清楚报错。CLI override 优先于模板；不支持的 effort 应在创建队列记录前拒绝。
4. **让提交内容可检查。** 当前 dry-run 不显示命令或提示词。模板模式应展示最终模型、effort、完整展开提示词，确保用户能看到委托任务实际内容。持久化 resolved profile 和模板版本，沿用现有 prompt 文件保存完整展开文本，避免后续升级影响在途任务解释。
5. **正式版要同步安装产物。** 当前版本同时在 `pyproject.toml` 和 `__init__.py`，README 有多处 preview 描述。若提示词作为资源文件新增，必须进入 setuptools package-data，并从 wheel 安装后验证模板可用；仅源码目录测试不足以证明发布可用。

**两个问题：**

- `--template test` 是否同时支持 Claude？若支持，是否接受 Claude 默认继承其本地模型，而 Luna/max 仅作为 Codex 默认？
- `test` 在没有 requirements 时应报错，还是自动根据当前 diff 生成测试任务？建议本次要求明确 requirements，避免把实现自身当作测试依据。


Lin — skeptical testing specialist

1. **P1: A different model does not create an independent oracle.** The template should require deriving expected behavior from supplied requirements/public contracts before inspecting implementation. Implementation may inform imports and fixtures, but cannot establish correctness. Missing or contradictory requirements must be reported as gaps, rather than converting existing behavior into assertions.

2. **P1: Child success must not imply tests passed.** `managed_task_prompt()` currently reports generic agent completion and the result path (`cli.py:2653`). A test-template callback must explicitly say tests are unexecuted and instruct the parent to inspect artifacts, execute the proposed commands, and report actual results. Nonzero child exit or missing artifacts must not trigger an apparent successful test handoff.

3. **P1: Require fault-sensitive tests and a reviewable handoff.** Each meaningful case should identify its requirement, expected outcome, and plausible faulty behavior it would detect. Cover relevant boundaries, invalid inputs, and state transitions; avoid mandatory coverage quotas. Reject tautologies, assertions copied from implementation, and mocks replacing the very behavior being tested. Handoff should list changed files, execution commands, assumptions, and untested gaps.

4. **P2: Preserve independence after failures.** Parent instructions should prohibit weakening assertions merely to obtain green results. Distinguish implementation bugs, test bugs, environment failures, and ambiguous specifications. If expected behavior needs revision, explain it against the requirement before modifying tests.

5. **P2: Prompt-only boundaries should remain explicit.** The existing child executes in the same writable working directory (`agent_wrapped_command()`, `cli.py:2989`). “No production edits/no execution” is guidance, not enforcement. Document that limitation; ask the child to report touched files and any execution. Do not advertise sandbox isolation.

Questions:
- When the prompt says only “test this module,” should the child produce a gap report and tests for documented contracts, rather than invent behavior? **Recommendation: yes.**
- Is the parent expected to run mutation checks or negative controls? **Recommendation: suggest targeted checks where practical; do not make mutation tooling a release prerequisite.**

**Vote: Approve with changes.** Release requires the requirement-first template and explicit parent-execution callback. Add automated regression checks for generic-agent compatibility, persisted profile/model overrides, and distinct successful/failed test-authoring handoffs; prompt phrase assertions alone do not establish workflow correctness.


Sam（可靠性）审阅意见：**有条件赞成**。现有持久化任务与回调框架可复用，以下条件应在正式版落实。

1. **P1：在提交时冻结模板与模型配置。** 将模板名、模板版本、解析后的子代理模型与 effort 写入 task metadata，同时持久化展开后的完整提示词。当前已有 `agent_prompt_path` 与 `wrapped_command`，可以复用；不要等 daemon 执行时再查默认模板，否则升级期间排队任务的行为会漂移。回调 metadata 和 dry-run 也应展示这些配置。

2. **P1：明确区分进程完成与测试交付完成。** 当前 worker 即使退出非零，仍记录 `outcome="completed"`；Codex 没写结果时还创建空结果文件。这可以保留为进程生命周期语义，但 `test` 回调必须明确提示退出码、结果可能为空，以及“尚未执行测试”。不能把 CLI 正常退出表述为测试已交付、更不能表述为测试通过。

3. **P1：回调必须交代主代理下一步。** 目前 `managed_task_prompt()` 仅补充 child 名称与结果路径。模板任务应增加：检查测试文件及交付报告，核对需求覆盖，再由主代理执行测试并分析失败；子代理失败或缺少有效交付时先诊断，不要直接执行报告中给出的任意命令。要求报告包含新增/修改文件、覆盖的需求、建议执行命令与未解决假设。

4. **P2：模型覆盖只能作用于子代理。** 现有 `agent` 表示回调目标，而 `agent_worker` 才表示执行方。新增参数最好明确命名并分开保存，避免子代理 Luna/max 覆盖恢复主会话的模型配置。Claude 对不支持的 effort/default 应明确拒绝或定义专用配置，不能静默忽略。

5. **P2：保持恢复兼容。** 模板字段应为可选；旧版 agent task 缺失字段时仍正常执行和回调。模板错误、参数冲突尽量在写 task 和绑定 daemon 工作之前校验。失败后沿用现有“不自动重跑子任务”原则，避免重复修改测试文件。

需明确的两个设计问题：

- `test` 是否第一版仅支持 Codex？若支持 Claude，Claude 默认模型和 effort 策略是什么？
- 是否接受“提示词约束、共享工作区”作为第一版边界？它不能硬性保证子代理不运行测试或不修改生产代码，文档必须准确说明。


## Implementation for Round 2
Implemented template resource, child-only flags and resolved metadata; Codex Luna/max, Claude inherits CLI model and rejects effort. Nonempty requirements required. Prompt and callback explain parent execution, requirement-derived expectations and failure diagnosis. Shared-workspace limitations documented. Review current diff, respond to peers directly, vote and identify remaining blockers.

## Round 2: Debate

Ada — Round 2

Vote: Approve, subject to validation before release. Confidence: high for API compatibility; medium for installation/runtime until checks complete.

The implementation resolves my Round 1 API concerns. `--template` is explicit and rejects unknown names; model and reasoning flags live only on child parsers. Existing generic child argv remains unchanged through optional arguments. Codex template defaults are isolated from Claude, which inherits its own configuration and rejects an unsupported reasoning option. Nonempty requirements are required. Dry-run exposes the expanded prompt and resolved profile. Submission stores both the fully expanded text and profile metadata, and callbacks preserve metadata without altering the parent's model.

Response to Lin: I agree a different model is insufficient as an independent oracle. The actual template now asks for requirements-derived expectations, defect-sensitive cases, missing-contract reporting and an explicit unexecuted handoff. The callback tells the parent to review and execute, and prohibits weakening assertions merely for green results. This is an appropriate prompt-level workflow for this release; it should not claim guaranteed independence.

Response to Sam: I agree queue-time freezing and distinguishing process completion from delivery matter. The diff freezes command and prompt at submission, adds optional metadata, preserves older tasks via `.get()`, and explicitly calls out failed, empty, blocked and partial handoffs. No additional daemon state or retry path is introduced, which keeps the change appropriately bounded.

Remaining release conditions:
1. Run behavioral checks covering CLI override precedence, generic argv compatibility, callback parent/child separation, Claude effort rejection before any queued task, and the failed-authoring handoff. Prompt substring tests alone are not sufficient.
2. Build and install the wheel in a temporary target and render the bundled template there. `package-data` now includes it, but the resource lookup should be exercised on the installed artifact.
3. Verify the intended Codex CLI accepts the documented model/effort configuration locally. Account access cannot be universally guaranteed; the README accurately states that limitation and absence of fallback.

Tradeoffs accepted: shared writable workspace with prompt-level restrictions; no mutation tooling or enforced execution sandbox in this release; Claude model inherited by default. An explicit model override retains template effort unless `--reasoning-effort` also changes it. This is predictable and documented, although users choosing other models may need to override both flags.

No new code defect or design blocker found in the reviewed diff. The added README section appears after License; moving it adjacent to Agent would improve discoverability but is optional and not a release condition.


Lin — Round 2

Ada's explicit flag design correctly preserves existing prompts beginning with `test`; child-only model options avoid changing callback configuration. Sam's frozen submission metadata and lifecycle distinction are necessary and now appear in the implementation. I agree that Claude should inherit its own CLI defaults and reject Codex effort rather than guessing a model mapping.

The template now establishes requirements before implementation, requires plausible defects and coverage mapping, and prohibits both test execution and production edits. The callback tells the parent to inspect failed/empty/partial delivery before execution and never equates authoring success with passing tests. This addresses my blocking concerns. Prompt instructions cannot guarantee behavioral compliance; the documented shared-workspace limitation is an acceptable first-release tradeoff.

Vote: approve, conditional on parent-run regression checks and installed-wheel resource verification. Confidence: high in interface and handoff design; medium in model adherence until a real authoring task is observed. Automated checks can prove routing, defaults, persistence and callback text, but cannot prove the generated tests use an independent oracle. Parent review remains essential.

Independent tests will exercise CLI submission and persisted artifacts using only a mocked GNU screen availability boundary; no child is launched. Callback scenarios use persisted submitted records and explicit completed/failed process outcomes. Expected defaults and semantics derive from the README and requested workflow. Tests have NOT been run.


Sam — Round 2 reliability review

Vote: Approve with validation conditions. Confidence: high for code-path compatibility; medium for real CLI execution until validation evidence is recorded.

Response to Ada: The explicit --template interface avoids changing existing prompt parsing. Child-only --model and --reasoning-effort flags are correctly separated from callback routing. Persisting expanded instructions and argv at submission addresses upgrade drift. Package-data now includes the template resource; installed-wheel validation remains necessary.

Response to Lin: The requirement-first template and explicit parent handoff implement the independent-oracle boundary well. The callback now distinguishes process completion from executed-test results, exposes exit code, requires diagnosis of empty/partial/blocked delivery, and tells the parent not to weaken assertions. Shared-workspace limitations are documented accurately.

Resolved Round 1 findings: Task and callback records carry template version/model/effort; worker consumes saved argv/prompt; legacy tasks retain optional-field compatibility; Claude does not receive Codex defaults and rejects unsupported effort before submission. Generic callback model routing remains unchanged. No new blocking implementation defect found.

Remaining conditions and minor clarifications:

1. Validation: Add/execute meaningful regression checks for submitted durable metadata and expanded prompt, CLI overrides, unchanged generic argv, Claude effort rejection, callback routing, and zero/nonzero/empty authoring handoffs. Build and install a wheel into a temporary target and render test there. These checks substantiate release readiness rather than merely matching prompt phrases.
2. Documentation P2: README says LTC freezes model/effort across upgrades. That is true for resolved Codex test defaults and explicit overrides; inherited CLI defaults are saved as None and can change with CLI configuration before execution. Qualify the guarantee as freezing explicit/template-resolved values, while inherited CLI defaults remain external configuration.
3. Prompt P2: The report currently must explicitly state "Tests have NOT been run". Because execution restrictions are guidance, add an instruction to disclose any accidental execution or out-of-scope edits truthfully instead of requiring an inaccurate declaration. The callback already asks the parent to verify the report, so this is a small consistency improvement.

Accepted tradeoffs: No execution/filesystem enforcement, no runtime model-availability preflight, and no separate machine-readable authoring-status parser. Existing exit-code plus report inspection is adequate for this release when its limitations remain explicit. No requirement to change existing completed lifecycle semantics or automatically rerun failed child tasks.


## Round 2 Tally
3/3 approve subject to validation. Consensus: explicit child-only API, frozen template snapshot, requirement-derived oracle, parent-run tests and honest shared-workspace limitations. No unresolved design disagreement. No extra debate required.

## Follow-through
Clarified inherited CLI defaults are not pinned; prompt requires truthful disclosure of accidental execution or production edits. Independent contract tests are authored by Lin and executed by the parent.

## Validation and final outcome
Approved: all validation conditions addressed. Existing 109 tests pass with an isolated temporary target-lock directory and local socket permissions. Nine independently authored template tests pass, including real fake-child subprocess handoff for success/nonzero empty result and frozen configuration across template changes. Installed wheel renders the template from outside the source tree. Local Codex CLI exposes model/config overrides, and its model catalog lists gpt-5.6-luna with max effort. No live model generation was performed; model adherence remains a parent-review responsibility.

Report: .committee-reviews/2026-09-14-test-template.md. Panel work complete.
