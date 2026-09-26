"""Small callback envelopes; verbose evidence stays in a durable local artifact.

Rendering is deliberately independent of transport, reminder counters and disk
I/O. Old queue records retain their original rendering in the CLI compatibility
path. User instructions are never silently discarded: the envelope requires
reading the details whenever a message or custom handoff is present.
"""
from pathlib import Path
from typing import Mapping

FORMAT = "compact-v1"
REMINDER = "Inspect results, ACK receipt, then continue the goal. Check existing work before relaunching. ACK is not goal completion."


def render(request: Mapping[str, object], details_path: Path, ack_command: str) -> str:
    task = " ".join(str(request.get("task") or "Task update").split())
    if len(task) > 180:
        task = task[:177] + "..."
    lines = [f"[long-task-callback] {request['id']}", f"Task: {task}"]
    outcome = str(request.get("outcome") or "finished")
    exit_code = request.get("exit_code")
    lines.append(f"Result: {outcome}" + (f"; exit={exit_code}" if exit_code is not None else ""))
    log = request.get("log_path")
    result = request.get("agent_result_path")
    if isinstance(log, str) and log:
        parent = Path(log).parent
        files = [Path(log).name]
        if isinstance(result, str) and Path(result).parent == parent:
            files.append(Path(result).name)
            result = None
        lines.append(f"Files: {parent}/ ({', '.join(files)})")
    if result:
        lines.append(f"Agent result: {result}")
    if request.get("recovery_reason"):
        lines.append(f"Recovery: {request['recovery_reason']}; not automatically restarted. Inspect before relaunching.")
    if request.get("agent_template") == "test" and request.get("agent_template_source", "builtin:test") == "builtin:test":
        lines.append("Test-author handoff, not a test result. Inspect report and changes before executing tests.")
    requires_details = bool(request.get("message") or request.get("agent_template_handoff")
                            or request.get("goal_reminder") or request.get("recovery_reason")
                            or request.get("details_required"))
    lines.append(f"Details: {details_path}" + (" (read before acting)" if requires_details else " (as needed)"))
    target = request.get("target")
    if isinstance(target, dict) and target.get("kind") == "session":
        lines.append(f"Session: {target['value']} (only)")
    else:
        lines.append("WARNING: explicit --last target; may be an unrelated session.")
    lines.extend(["ACK after inspection:", ack_command])
    return "\n".join(lines)
