# Harness support

A harness is the program around an Agent: it manages conversations, calls models, runs tools, and delivers new messages. LTC integration covers starting the harness as a child Agent and returning task results to its original conversation.

This page reflects the current `main` implementation and repository validation records, checked on 2026-10-12. The quickstart uses stable `0.7.0`; newer integrations use the `0.7.1` preview.

## Native integrations

| Harness / client | Child Agent | Results in the original conversation | Version and validation |
| --- | --- | --- | --- |
| Codex CLI | `ltc agent codex` | Bind through `CODEX_THREAD_ID` and resume through the CLI | 0.7.0; see the release and platform records below |
| Claude Code CLI | `ltc agent claude` | Background `ltc wait` for live delivery; standalone CLI for session resume | Child and CLI resume support from 0.7.0; live delivery in the 0.7.1 preview, verified on Linux/macOS |
| Claude desktop app, Code sessions | Child tasks use a signed-in standalone Claude CLI | Run `ltc wait` through background Bash | 0.7.1 preview; verified with macOS local sessions and Linux SSH sessions |
| Pi | `ltc agent pi` | Extension sends a steer message to the live process; `ltc pi` adds recovery after closing | 0.7.1 preview; recorded validation on Linux with Pi 0.87.1; macOS/Windows validation pending |
| Codex Desktop | Child tasks use Codex CLI | Shared Core launcher with `--callback-mode desktop` | 0.7.1 preview; real Desktop restart, receipt, and ACK acceptance for the new installation path is pending |

You can combine child and receiving Agents. For example, `ltc agent claude` in Codex runs the task with Claude and returns the result to Codex. Follow the [Codex](codex.md), [Claude](claude.md), [Pi](pi.md), or [Desktop](desktop.md) guide.

Claude also has an `ltc claude` channel research preview. The repository records live receipt and ACK on Linux with Claude Code 2.1.296. See the [channel setup](https://github.com/lz59970062/long-task-wakeup/blob/main/docs/claude-code.md#ltc-channel-research-preview).

## Other harnesses and orchestration frameworks

| Name | Current integration route | Work for native integration |
| --- | --- | --- |
| Gemini CLI, OpenCode, Aider, Goose | Run a configured command-line task through `ltc run -- <command>` and return results to an existing receiver | Implement child arguments, session detection, and callback transport; validate each end to end |
| LangGraph, AutoGen, CrewAI, custom Agent SDK programs | Save the workflow in a script and use `ltc run`, or call `ltc done` at the end of an existing job | Implement a session adapter and wakeup channel for the receiving harness |
| DSH | An adapter is on the repository roadmap | Complete implementation and validation, then add it to the native table |

These frameworks currently use the generic command interface. The native adapter registry contains `codex`, `claude`, and `pi`. Begin with a script that prints a result and exits, check the log, callback, and ACK, then connect the actual workflow. See the [custom workflow example](../guides/use-cases.md#run-a-custom-agent-workflow).

## Execution environments

| Environment | Task backend | Setup conditions |
| --- | --- | --- |
| Linux | systemd user services, GNU screen | Desktop or server; screen is useful in containers and SSH environments |
| macOS | launchd, GNU screen | launchd uses the logged-in user's GUI session |
| Windows 10+ | Task Scheduler, Job Object | Keep the user logged in; use local NTFS storage for task state |

These task backends ship in `0.7.0`. Configure the callback route for your chosen harness using the native integration table. Windows validation for Claude live callbacks is pending.

## Add a harness

Start with [`agents/base.py`](https://github.com/lz59970062/long-task-wakeup/blob/main/src/long_task_callback/agents/base.py) and [`agents/registry.py`](https://github.com/lz59970062/long-task-wakeup/blob/main/src/long_task_callback/agents/registry.py):

1. Implement child startup: locate the executable, build its command, pass the prompt, and choose file or standard-output result capture.
2. Define session binding: read the session identifier and retain the profile and directory needed for recovery.
3. Add delivery: construct a resume command for a closed session and a message channel for a running session.
4. Register the adapter and add CLI selection, installation entry points, and a usage skill for that harness.
5. Use an isolated session to validate submission, logs, results, receipt, and ACK. Check busy-session, exit, and retry behavior.

`ChildAgentAdapter` describes the child-task interface. `AgentAdapter` adds session detection and resume. Organizing the implementation around these interfaces lets you validate execution and delivery separately.

## Implementation and validation sources

- [Current adapter registry](https://github.com/lz59970062/long-task-wakeup/blob/main/src/long_task_callback/agents/__init__.py)
- [0.7.0 release validation](https://github.com/lz59970062/long-task-wakeup/blob/main/docs/validation-0.7.0.md)
- [Claude Code platform records](https://github.com/lz59970062/long-task-wakeup/blob/main/docs/claude-code.md#platform-status)
- [Pi integration validation](https://github.com/lz59970062/long-task-wakeup/blob/main/docs/validation-pi-child.md)
- [Desktop preview acceptance record](https://github.com/lz59970062/long-task-wakeup/blob/main/docs/validation-0.7.1-preview.md)
