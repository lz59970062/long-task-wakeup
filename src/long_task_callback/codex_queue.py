"""Deliver once through Codex's public queue CLI; the existing owner wakes itself.

`codex queue --thread UUID --message TEXT` stores the message in the profile's
queue. The client that has the thread open consumes it, after the current turn
if one is running, or when the thread is reopened if none has it open. No private
Desktop IPC, launcher, alternate writer, status polling or --resume is involved.

The receipt is written before the command starts, so after any interruption it
can only say that a message may exist. Automatic delivery therefore never sends
twice: a kill, a timeout or an unreadable success is an unknown outcome, while a
rejection the command reported, or a command that could not start, sent nothing
and can be repaired with `ltc retry`. Acceptance means queued, not received: the
callback stays pending until the chat runs its ACK, but it no longer holds the
target lease, because Codex owns the message and its order from then on.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
from typing import Mapping

from .agents import AGENTS
from .agents.codex import CODEX_BIN_ENV
from .platforms import windows_process

ROUTE_FIELDS = ("callback_codex_bin", "callback_codex_home")
CALLBACK_VERSION = 5  # Older daemons must reject this route instead of resuming the thread.
TASK_VERSION = 6
PUBLISHED_RETURN_CODE = 121  # Delivery worker: the queue accepted it; ACK is pending.
AWAITING_ACK = "queued_awaiting_ack"
AWAITING_REASON = "published through codex queue; awaiting ACK, nothing is sent again"
UNKNOWN_ERROR = ("Codex queue callback outcome is unknown; automatic retry suppressed to prevent duplicate delivery. "
                 "Inspect the chat, then run ltc ack or ltc cancel for this id")
# Files are written beside the record, in the queue directory the coordinator owns.
RECEIPT_DIRECTORY = "codex-queue"
ENVIRONMENT_DIRECTORY = "environments"
COMMAND_TIMEOUT = 60.0
PROBE_TIMEOUT = 10.0
# 128+N is how a shell wrapper reports a child killed by signal N, not a rejection.
FIRST_SIGNAL_EXIT = 128
# These describe the process that captured the environment, not the session: a
# service unit, a screen window, a delivery worker or the Desktop app itself.
EXCLUDED_ENVIRONMENT = ("INVOCATION_ID", "JOURNAL_STREAM", "SYSTEMD_EXEC_PID", "STY", "WINDOW",
                        "XPC_SERVICE_NAME", "XPC_FLAGS", "CLAUDE_CODE_ENTRYPOINT",
                        "CODEX_INTERNAL_ORIGINATOR_OVERRIDE",
                        "CODEX_LONG_TASK_DELIVERY_LOCK_FDS", "CODEX_LONG_TASK_DELIVERY_LOCK_FD")
_THREAD_ID = re.compile(r"[0-9a-fA-F]{8}(-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}\Z")
_ACCEPTED = re.compile(r"Queued message ([0-9a-fA-F-]+) for thread ([0-9a-fA-F-]+)\.")


def route_fields() -> dict[str, str]:
    from . import cli

    candidate = os.environ.get(CODEX_BIN_ENV)
    if not candidate and sys.platform == "darwin":
        for app in ("ChatGPT", "Codex"):
            resources = Path("/Applications") / (app + ".app") / "Contents/Resources"
            for path in (resources / "codex-cli/CodexCLI.app/Contents/MacOS/codex", resources / "codex"):
                if path.is_file() and os.access(path, os.X_OK):
                    candidate = str(path)
                    break
            if candidate:
                break
    candidate = candidate or "codex"
    found = shutil.which(candidate)
    # Preserve an absolute missing path for a useful preflight diagnostic.
    executable = str(Path(found or candidate).expanduser().absolute())
    return {"callback_codex_bin": executable, "callback_codex_home": str(cli.codex_home().resolve())}


def validate_route(request: Mapping[str, object]) -> None:
    target = request.get("target")
    if request.get("agent", "codex") != "codex" or not isinstance(target, dict) or target.get("kind") != "session":
        raise ValueError("codex-queue requires a bound Codex session")
    value = target.get("value")
    if not isinstance(value, str) or not _THREAD_ID.match(value):
        raise ValueError("codex-queue requires the Codex thread UUID (CODEX_THREAD_ID), not a session name")
    for key in ROUTE_FIELDS:
        path = request.get(key)
        if not isinstance(path, str) or not path or "\0" in path or not Path(path).is_absolute():
            raise ValueError(f"codex-queue requires an absolute frozen {key}")


def receipt_path(root: Path, callback_id: str) -> Path:
    return root / RECEIPT_DIRECTORY / f"{callback_id}.json"


def read_receipt(root: Path, callback_id: str) -> dict[str, object] | None:
    try:
        receipt = json.loads(receipt_path(root, callback_id).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        return {"state": "unreadable"}
    return receipt if isinstance(receipt, dict) else {"state": "unreadable"}


def may_have_been_queued(root: Path, callback_id: str) -> bool:
    """Whether any route sending this callback again could duplicate it."""
    receipt = read_receipt(root, callback_id)
    return receipt is not None and receipt.get("state") != "rejected"


def environment_path(root: Path, callback_id: str) -> Path:
    return root / ENVIRONMENT_DIRECTORY / f"{callback_id}.json"


def capture_environment(root: Path, callback_id: str, environment: Mapping[str, str] | None = None) -> None:
    """Keep the session's environment for the coordinator; failure only loses that help."""
    from . import cli

    values = AGENTS.child_environment(os.environ if environment is None else environment)
    for name in EXCLUDED_ENVIRONMENT:
        values.pop(name, None)
    try:
        cli.private_directory(root / ENVIRONMENT_DIRECTORY)
        cli.write_private_text(environment_path(root, callback_id), json.dumps(values, ensure_ascii=False))
    except OSError as exc:
        print(f"ltc: warning: could not save the session environment for {callback_id}: {exc}", file=sys.stderr)


def load_environment(root: Path, callback_id: str) -> dict[str, str] | None:
    try:
        values = json.loads(environment_path(root, callback_id).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(values, dict):
        return None
    return {str(name): value for name, value in values.items() if isinstance(value, str)}


def discard_environment(root: Path, callback_id: str) -> None:
    try:
        environment_path(root, callback_id).unlink()
    except OSError:
        pass


def command_environment(request: Mapping[str, object], session: Mapping[str, str] | None) -> dict[str, str]:
    # A service coordinator's PATH may not reach the `node` an npm Codex needs;
    # the submitting session's environment does. The route's profile always wins.
    values = AGENTS.child_environment({**os.environ, **(session or {})})
    for name in EXCLUDED_ENVIRONMENT:
        values.pop(name, None)
    values["CODEX_HOME"] = str(request["callback_codex_home"])
    return values


def _saved_session(request: Mapping[str, object]) -> dict[str, str] | None:
    root, callback_id = request.get("queue_dir"), request.get("id")
    if isinstance(root, str) and root and isinstance(callback_id, str) and callback_id:
        return load_environment(Path(root), callback_id)
    return None


def inspect_route(request: Mapping[str, object], *, timeout: float) -> dict[str, object]:
    from . import cli

    try:
        validate_route(request)
        env = command_environment(request, _saved_session(request))
        result = subprocess.run(cli.process_command([str(request["callback_codex_bin"]), "queue", "--help"], env),
                                env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True, encoding="utf-8",
                                errors="replace", timeout=max(PROBE_TIMEOUT, timeout),
                                **windows_process.background_popen_kwargs())
        if result.returncode != 0 or not all(flag in result.stdout for flag in ("--thread", "--message")):
            raise ValueError("the selected Codex executable does not support native queue delivery; update Codex, "
                             f"or point {CODEX_BIN_ENV} at one that does")
        return {"status": "available", "transport": "codex_queue", "reason": "native_queue_command_available",
                "end_to_end_verified": False,
                "action": "Capability only: an open receiving chat must consume the message and ACK."}
    except (OSError, ValueError, TypeError, subprocess.TimeoutExpired) as exc:
        return {"status": "blocked", "transport": "codex_queue", "reason": "native_queue_unavailable",
                "end_to_end_verified": False, "action": str(exc)}


def _release(root: Path, request: dict[str, object]) -> None:
    from . import cli

    try:
        cli.release_retained_target_lease(root, request)
    except (OSError, RuntimeError) as exc:
        # The receipt, not the lease, stops a replay; the daemon retries the release.
        print(f"ltc: warning: could not release the target lease of {request.get('id')}: {exc}", file=sys.stderr)


def _reject(root: Path, request: dict[str, object], record: dict[str, object], error: str) -> dict[str, object]:
    from . import cli

    print(f"ltc: codex queue did not take callback {record['callback_id']}: {error[-500:]}", file=sys.stderr)
    record.update(state="rejected", error=error[-2000:], finished_at=time.time())
    cli.write_request(receipt_path(root, str(record["callback_id"])), record)
    _release(root, request)
    return {"returncode": 126, "delivery": "codex_queue", "error": error[-2000:]}


def publish(payload: dict[str, object]) -> dict[str, object]:
    from . import cli

    request = payload["request"]
    root = Path(str(payload["queue_dir"]))
    ident = str(request["id"])
    try:
        validate_route(request)
    except (ValueError, TypeError) as exc:
        return _reject(root, request, {"callback_id": ident, "state": "submitting", "started_at": time.time()}, str(exc))
    prior = read_receipt(root, ident)
    if prior is not None and prior.get("state") == "queued":
        _release(root, request)
        return {"returncode": PUBLISHED_RETURN_CODE, "delivery": "codex_queue", "message_id": prior.get("message_id")}
    if prior is not None and prior.get("state") != "rejected":
        # A killed sender may already have queued its message. Never infer
        # permission to replay from a missing or partial acceptance response.
        return {"returncode": 125, "delivery": "codex_queue", "manual_recovery_required": True}
    cli.retain_target_lease(root, request)
    if cli.ack_path(root, ident).exists() or cli.is_canceled(root, ident):
        cli.release_retained_target_lease(root, request)
        return {"returncode": 0, "delivery": "codex_queue", "skipped": "acknowledged_or_canceled"}
    thread = str(request["target"]["value"])
    record: dict[str, object] = {"callback_id": ident, "thread_id": thread, "state": "submitting", "started_at": time.time()}
    try:
        cli.write_request(receipt_path(root, ident), record)
    except OSError as exc:
        cli.release_retained_target_lease(root, request)
        return {"returncode": 126, "delivery": "codex_queue", "error": f"could not save the delivery receipt: {exc}"}
    env = command_environment(request, load_environment(root, ident))
    try:
        # Windows resolves the executable here, so a missing Codex raises before anything is sent.
        command = cli.process_command([str(request["callback_codex_bin"]), "queue", "--thread", thread,
                                       "--message", str(payload["prompt"])], env)
        result = subprocess.run(command, env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                                encoding="utf-8", errors="replace",
                                timeout=min(COMMAND_TIMEOUT, max(1.0, float(payload["timeout"]))),
                                **windows_process.background_popen_kwargs())
    except (OSError, ValueError) as exc:
        return _reject(root, request, record, str(exc))
    except subprocess.TimeoutExpired:
        record.update(state="unknown", error="codex queue timed out", finished_at=time.time())
        cli.write_request(receipt_path(root, ident), record)
        discard_environment(root, ident)
        return {"returncode": 125, "delivery": "codex_queue", "manual_recovery_required": True}
    accepted = _ACCEPTED.search(result.stdout or "")
    if result.returncode == 0 and accepted and accepted[2].casefold() == thread.casefold():
        record.update(state="queued", message_id=accepted[1], queued_at=time.time())
        cli.write_request(receipt_path(root, ident), record)
        discard_environment(root, ident)
        # Codex owns the message now and keeps the order. An unanswered ACK must
        # not hold back later callbacks for this thread; the receipt stops a replay.
        _release(root, request)
        return {"returncode": PUBLISHED_RETURN_CODE, "delivery": "codex_queue", "message_id": accepted[1]}
    if 0 < result.returncode < FIRST_SIGNAL_EXIT and not accepted:
        return _reject(root, request, record, (result.stderr or "").strip() or f"exit {result.returncode}")
    record.update(state="unknown", exit_code=result.returncode, stderr=(result.stderr or "")[-2000:], finished_at=time.time())
    cli.write_request(receipt_path(root, ident), record)
    discard_environment(root, ident)
    return {"returncode": 125, "delivery": "codex_queue", "manual_recovery_required": True}


def mark_awaiting_ack(request: dict[str, object], receipt: Mapping[str, object] | None) -> None:
    request["delivery_state"] = AWAITING_ACK
    request.setdefault("live_delivered_at", time.time())
    request["last_deferred_reason"] = AWAITING_REASON
    if receipt is not None and receipt.get("message_id"):
        request["codex_queue_message_id"] = receipt["message_id"]
    request.pop("last_error", None)


def rejection_error(root: Path, callback_id: str) -> str:
    receipt = read_receipt(root, callback_id) or {}
    detail = str(receipt.get("error") or "codex queue did not take the message").strip()
    return f"{detail[-500:]}. Nothing was queued; fix the cause, then run ltc retry --id {callback_id}"


def recover_interrupted(root: Path, path: Path, request: dict[str, object]) -> Path:
    """Decide where a record goes when its delivery worker vanished, from the receipt alone."""
    from . import cli

    ident = str(request["id"])
    receipt = read_receipt(root, ident)
    state = None if receipt is None else receipt.get("state")
    if state == "queued":
        mark_awaiting_ack(request, receipt)
        cli.write_request(path, request)
        _release(root, request)
        return root / "pending"
    if state in (None, "rejected"):
        # The receipt precedes the command, so no message was accepted.
        _release(root, request)
        return root / "pending"
    request["retain_target_lease"] = True
    request["last_error"] = UNKNOWN_ERROR
    cli.write_request(path, request)
    return root / "failed"


def reconcile(root: Path) -> None:
    """Finish what an ACK from a sandbox or an interrupted release left behind."""
    from . import cli

    for path in sorted((root / "pending").glob("*.json")):
        try:
            request = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(request, dict) and request.get("delivery_state") == AWAITING_ACK:
            _release(root, request)
    try:
        entries = list((root / ENVIRONMENT_DIRECTORY).glob("*.json"))
    except OSError:
        return
    for entry in entries:
        ident = entry.stem
        if (cli.ack_path(root, ident).exists() or cli.is_canceled(root, ident)
                or cli.request_path(root, "done", ident).exists()):
            discard_environment(root, ident)
