"""Freeze callback intent separately from the workload's execution backend.

Capability probes are read-only. A successful probe is a current observation,
not a guarantee that the owner, connection or authentication will stay alive.
"""
from __future__ import annotations

import argparse
import os
import sys

# `desktop` stays parseable only to answer it with the replacement. Saved records
# keep the mode, and their callback_bridge_file, until they are retried.
MODES = ("auto", "cli", "codex-queue", "desktop", "manual")
OFFERED_MODES = tuple(mode for mode in MODES if mode != "desktop")
ROUTE_FIELDS = ("callback_mode", "callback_origin", "callback_bridge_file",
                "callback_codex_bin", "callback_codex_home",
                "pi_callback_protocol", "pi_profile_dir", "pi_channel_root", "pi_delivery")


def desktop_route_repair(request: dict[str, object]) -> str:
    return f"ltc retry --id {request.get('id') or '<callback-id>'} --callback-mode codex-queue"


def selection(args: argparse.Namespace) -> dict[str, str]:
    from . import cli

    frozen = getattr(args, "_callback_route", None)
    if isinstance(frozen, dict):
        return dict(frozen)
    mode = getattr(args, "callback_mode", "auto") or "auto"
    if mode not in MODES:
        raise ValueError("unsupported callback mode")
    if mode == "desktop":
        raise ValueError("--callback-mode desktop is retired; use codex-queue, which works on a normally started Codex Desktop")
    origin = os.environ.get("CODEX_INTERNAL_ORIGINATOR_OVERRIDE", "").strip().casefold()
    desktop = origin == "codex desktop"
    if mode == "auto":
        mode = "codex-queue" if cli.resolve_agent(args) == "codex" and desktop else "cli"
    if mode == "codex-queue" and cli.resolve_agent(args) != "codex":
        raise ValueError("The codex-queue callback mode supports Codex only")
    route = {"callback_mode": mode, "callback_origin": "desktop" if desktop else "other"}
    if mode == "codex-queue":
        from . import codex_queue
        route.update(codex_queue.route_fields())
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
    if mode == "codex-queue":
        from . import codex_queue
        return dict(report, **codex_queue.inspect_route(request, timeout=timeout))
    if cli.request_agent(request) == "pi":
        from . import pi_callback
        return dict(report, **pi_callback.inspect_route(request))
    if cli.request_agent(request) == "dsh":
        return dict(report, transport="live_wait", reason="live_session_delivery",
                    action="Run `ltc wait --task <id>` as a background job from the bound DSH session; "
                           "DSH callbacks are never resumed headlessly.")
    if cli.request_agent(request) != "codex":
        return dict(report, transport="cli_resume", reason="session_delivery_not_probed")
    connection = None
    try:
        endpoint = cli.desktop_app_server_socket(request)
        if endpoint is None:
            if mode == "desktop":
                raise ValueError("Desktop App Server not selected")
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
                    action=(f"Run {desktop_route_repair(request)}; an idle or absent listing is not permission to take another Core's writer."
                            if mode == "desktop" else "Open the original session through the Core this socket reaches; an idle or absent listing is not permission to take another Core's writer."))
    except (OSError, ValueError, RuntimeError, TypeError):
        return dict(report, status="blocked" if mode == "desktop" else "unverified",
                    reason="desktop_route_unavailable" if mode == "desktop" else "cli_resume_unverified",
                    action=(f"Run {desktop_route_repair(request)}, or choose --callback-mode manual to keep the result for reading in the session."
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
