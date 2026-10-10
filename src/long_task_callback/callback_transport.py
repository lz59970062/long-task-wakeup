"""Freeze callback intent separately from the workload's execution backend.

Capability probes are read-only. A successful probe is a current observation,
not a guarantee that the owner, connection or authentication will stay alive.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

MODES = ("auto", "cli", "desktop", "manual")
ROUTE_FIELDS = ("callback_mode", "callback_origin", "callback_bridge_file",
                "pi_callback_protocol", "pi_profile_dir", "pi_channel_root", "pi_delivery")


def configuration_path(profile: Path) -> Path:
    return profile / "long-task-wakeup" / "desktop.json"


def bridge_file(profile: Path) -> Path:
    raw = os.environ.get("CODEX_LONG_TASK_WAKEUP_DESKTOP_BRIDGE_FILE")
    if raw:
        return Path(raw).expanduser().absolute()
    config = configuration_path(profile)
    try:
        record = json.loads(config.read_text(encoding="utf-8"))
    except FileNotFoundError:
        if sys.platform == "win32":
            return profile / "long-task-wakeup" / "desktop-bridge.json"
        return profile / "long-task-wakeup" / "desktop" / "bridge.json"
    if (not isinstance(record, dict) or record.get("version") != 1
            or record.get("codex_home") != str(profile.resolve())
            or not isinstance(record.get("metadata"), str) or not Path(record["metadata"]).is_absolute()):
        raise ValueError("Desktop configuration is invalid for this profile")
    return Path(record["metadata"])


def selection(args: argparse.Namespace) -> dict[str, str]:
    from . import cli

    frozen = getattr(args, "_callback_route", None)
    if isinstance(frozen, dict):
        return dict(frozen)
    mode = getattr(args, "callback_mode", "auto") or "auto"
    if mode not in MODES:
        raise ValueError("unsupported callback mode")
    origin = os.environ.get("CODEX_INTERNAL_ORIGINATOR_OVERRIDE", "").strip().casefold()
    desktop = origin == "codex desktop"
    if mode == "auto":
        mode = "desktop" if cli.resolve_agent(args) == "codex" and desktop else "cli"
    if mode == "desktop" and cli.resolve_agent(args) != "codex":
        raise ValueError("Desktop callback mode currently supports Codex only")
    route = {"callback_mode": mode, "callback_origin": "desktop" if desktop else "other"}
    if mode == "desktop":
        route["callback_bridge_file"] = str(bridge_file(cli.codex_home()))
    if cli.resolve_agent(args) == "pi":
        from . import pi_callback
        route.update(pi_callback.route_fields())
    return route


def inspect_route(request: dict[str, object], *, timeout: float = 2) -> dict[str, object]:
    from . import cli

    mode = request.get("callback_mode", "cli")
    report: dict[str, object] = {"mode": mode, "status": "unverified", "end_to_end_verified": False}
    if mode == "manual":
        return dict(report, status="manual", reason="automatic_delivery_disabled",
                    action="Inspect saved results in the bound session and ACK when received.")
    if cli.request_agent(request) == "pi":
        from . import pi_callback
        return dict(report, **pi_callback.inspect_route(request))
    if cli.request_agent(request) != "codex":
        return dict(report, transport="cli_resume", reason="session_delivery_not_probed")
    connection = None
    try:
        endpoint = cli.desktop_app_server_socket(request)
        if endpoint is None:
            if mode == "desktop":
                raise ValueError("Desktop bridge not selected")
            return dict(report, transport="cli_resume", reason="session_delivery_not_probed")
        connection = cli.AppServerConnection(endpoint, timeout)
        connection.connect()
        connection.request("initialize", {"clientInfo": {"name": "ltc-capability-check", "version": cli.__version__},
                                          "capabilities": {"experimentalApi": True}})
        connection.notify("initialized", {})
        target = request.get("target")
        expected = target.get("value") if isinstance(target, dict) and target.get("kind") == "session" else None
        cursor = None
        # Bounded pagination; failure to find a thread never proves it is free.
        for _ in range(8):
            result = connection.request("thread/loaded/list", {"cursor": cursor, "limit": 100})
            if not isinstance(result, dict) or not isinstance(result.get("data"), list):
                raise ValueError("invalid loaded-thread response")
            if expected and expected in result["data"]:
                return dict(report, status="owner_reachable", transport="app_server", reason="bound_session_loaded")
            cursor = result.get("nextCursor")
            if not cursor:
                break
        return dict(report, status="blocked" if mode == "desktop" else "unverified",
                    transport="app_server", reason="bound_session_not_loaded",
                    action="Open the original session through the configured shared Core; an idle or absent listing is not permission to take another Core's writer.")
    except (OSError, ValueError, RuntimeError, TypeError):
        return dict(report, status="blocked" if mode == "desktop" else "unverified",
                    reason="desktop_bridge_unavailable" if mode == "desktop" else "cli_resume_unverified",
                    action=("Configure/start the Desktop shared Core, then recheck outside a restricted tool sandbox; or explicitly choose manual callback mode."
                            if mode == "desktop" else "CLI resume remains available, but original-session receipt is not verified; inspect ownership if delivery fails."))
    finally:
        if connection is not None:
            connection.close()


def preflight(args: argparse.Namespace) -> bool:
    from . import cli

    try:
        route = selection(args)
        args._callback_route = route
        target, _ = cli.bind_target(args)
        report = inspect_route({**route, "agent": cli.resolve_agent(args), "target": target})
    except (OSError, ValueError) as error:
        report = {"status": "blocked", "reason": "invalid_callback_configuration", "action": str(error)}
    args._callback_capability = report
    if report["status"] == "blocked":
        print("ltc: automatic callback unavailable before task submission: " + str(report["reason"]), file=sys.stderr)
        print("ltc: " + str(report.get("action", "Repair the original session connection, or use --callback-mode manual to save results.")), file=sys.stderr)
        return False
    return True
