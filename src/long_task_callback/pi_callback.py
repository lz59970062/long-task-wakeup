"""Pi's live mailbox and explicitly managed, pre-start session ownership.

A normal Pi extension provides online delivery only. Automatic file recovery
requires an LTC-managed owner whose entire process has exited. The advisory
writer lease coordinates LTC writers; it cannot police unrelated Pi processes.
"""
from __future__ import annotations

import argparse
import hashlib
from importlib import resources
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
import threading
import time
import uuid

from . import storage
from .platforms import windows_io

PROTOCOL = "live-v1"
CHANNEL_ROOT_ENV = "LTC_PI_CHANNEL_ROOT"
RESERVATION_ENV = "LTC_PI_RESERVATION"
PROFILE_ENV = "PI_CODING_AGENT_DIR"
ROUTE_FIELDS = ("pi_callback_protocol", "pi_profile_dir", "pi_channel_root", "pi_delivery")
_SAFE_ID = re.compile(r"[A-Za-z0-9_-]+\Z")


def profile_dir() -> Path:
    return Path(os.path.normcase(str(Path(os.environ.get(PROFILE_ENV, "~/.pi/agent")).expanduser().resolve())))


def canonical_session(value: object) -> str:
    if not isinstance(value, str) or not value or not Path(value).is_absolute():
        raise ValueError("Pi callbacks require an absolute session JSONL file, not --last or a partial ID")
    return os.path.normcase(str(Path(value).resolve()))


def route_fields() -> dict[str, str]:
    profile = profile_dir()
    root = Path(os.environ.get(CHANNEL_ROOT_ENV, str(profile / "long-task-callback" / "channels")))
    # Pi itself queues steer while running and starts a normal turn while idle.
    # Freeze this contract in the record, without adding a model-facing option.
    return {"pi_callback_protocol": PROTOCOL, "pi_delivery": "steer", "pi_profile_dir": str(profile),
            "pi_channel_root": os.path.normcase(str(root.expanduser().resolve()))}


def validate_route(request: dict) -> None:
    if request.get("pi_callback_protocol") != PROTOCOL:
        raise ValueError("This Pi callback predates live-session routing; inspect its result and ACK manually")
    if request.get("pi_delivery", "follow-up") not in ("follow-up", "steer"):
        raise ValueError("Pi delivery must be follow-up or steer")
    target = request.get("target")
    canonical_session(target.get("value") if isinstance(target, dict) and target.get("kind") == "session" else None)
    for field in ("pi_channel_root", "pi_profile_dir"):
        value = request.get(field)
        if not isinstance(value, str) or not Path(value).is_absolute():
            raise ValueError("Pi callback is missing its frozen profile/channel directory")


def channel_dir(session: str, root: Path) -> Path:
    digest = hashlib.sha256(canonical_session(session).encode("utf-8")).hexdigest()
    return root / digest


def writer_lock_path(session: str) -> Path:
    value = canonical_session(session)
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()
    return Path(value).parent / f".ltc-pi-{digest}.lock"


def ensure_private_directory(path: Path) -> None:
    if path.is_symlink():
        raise ValueError(f"Pi callback directory is a symlink: {path}")
    if os.name == "nt":
        windows_io.ensure_private_directory(path)
    else:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        if path.stat().st_uid != os.getuid() or path.stat().st_mode & 0o077:
            raise ValueError(f"Pi callback directory must be private to its owner: {path}")


def read_record(path: Path, *, versions: tuple[int, ...] = (1,)) -> dict:
    if path.is_symlink():
        raise ValueError(f"Pi callback record is a symlink: {path}")
    stat = path.stat()  # Preserve FileNotFoundError for first-time registration.
    if not path.is_file() or stat.st_size > 2 * 1024 * 1024:
        raise ValueError(f"Invalid Pi callback record: {path}")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or value.get("version") not in versions:
        raise ValueError(f"Invalid Pi callback record: {path}")
    return value


def session_header(session: str) -> dict:
    path = Path(canonical_session(session))
    if not path.is_file() or path.stat().st_nlink != 1:
        raise ValueError("Pi session file is missing or is not a regular file")
    with path.open(encoding="utf-8") as stream:
        header = json.loads(stream.readline(65536))
    if (not isinstance(header, dict) or header.get("type") != "session"
            or not isinstance(header.get("id"), str) or not header["id"]):
        raise ValueError("Pi session file has no valid session header")
    return header


def owner_record(request: dict) -> tuple[Path, dict]:
    validate_route(request)
    target = request.get("target")
    session = canonical_session(target.get("value") if isinstance(target, dict) and target.get("kind") == "session" else None)
    root = request.get("pi_channel_root")
    profile = request.get("pi_profile_dir")
    directory = channel_dir(session, Path(root))
    if directory.is_symlink():
        raise ValueError("Pi callback channel is a symlink")
    owner = read_record(directory / "owner.json")
    if (owner.get("profile_dir") != profile or owner.get("session_file") != session
            or owner.get("session_id") != session_header(session)["id"]
            or owner.get("channel_dir") != str(directory) or not isinstance(owner.get("owner_nonce"), str)
            or not _SAFE_ID.fullmatch(owner["owner_nonce"])
            or type(owner.get("owner_pid")) is not int or owner["owner_pid"] <= 0):
        raise ValueError("Pi callback owner does not match the bound session")
    return directory, owner


def confirmed_owner_dead(owner: dict) -> bool:
    """Authorize recovery only from a known scope and confirmed PID absence.

    Any reused/live PID blocks recovery. Missing identities, different hosts or
    PID namespaces, and permission/probe failures are never proof of death.
    """
    from . import cli
    identity = owner.get("owner_identity")
    current = cli.daemon_process_identity(os.getpid())
    if owner.get("managed") is not True or not isinstance(identity, dict) or not current:
        return False
    fields = ("boot_id", "machine_id", "pid_namespace") if sys.platform.startswith("linux") else (
        ("boot_id", "machine_id", "uid") if sys.platform == "darwin" else ("boot_id", "machine_id", "sid"))
    if any(key not in identity or identity[key] != current.get(key) for key in fields):
        return False
    start_field = "start_ticks" if sys.platform.startswith("linux") else (
        "start_seconds" if sys.platform == "darwin" else "creation_time")
    if type(identity.get(start_field)) is not int or identity[start_field] <= 0:
        return False
    pid = owner.get("owner_pid")
    if type(pid) is not int or pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        try:
            from .platforms.windows import _kernel32
            kernel = _kernel32()
            handle = kernel.OpenProcess(0x100000, False, pid)
            if not handle:
                return ctypes.get_last_error() == 87  # Invalid PID, not an indeterminate failure.
            try:
                return kernel.WaitForSingleObject(handle, 0) == 0
            finally:
                kernel.CloseHandle(handle)
        except (OSError, AttributeError, RuntimeError):
            return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except OSError:
        return False
    return False


def valid_live_lease(owner: dict) -> bool:
    from . import cli
    pid = owner.get("lease_pid")
    return (type(pid) is int and pid > 0 and isinstance(owner.get("lease_identity"), dict)
            and cli.daemon_process_identity(pid) == owner["lease_identity"])


def require_delivery_support(request: dict, owner: dict) -> None:
    if request.get("pi_delivery", "follow-up") == "steer":
        modes = owner.get("delivery_modes")
        if not isinstance(modes, list) or "steer" not in modes:
            raise ValueError("The live Pi extension does not support steer; upgrade it with "
                             "ltc install-pi-extension --force and reload or restart Pi")


def inspect_route(request: dict) -> dict:
    from . import cli
    report = {"transport": "pi_live_session", "status": "blocked", "end_to_end_verified": False,
              "pi_delivery": request.get("pi_delivery", "follow-up")}
    try:
        _, owner = owner_record(request)
        if cli.pid_is_running(owner["owner_pid"]):
            if owner.get("managed") is True and not valid_live_lease(owner):
                raise ValueError("The Pi launcher exited while its Pi process remains alive; manual recovery is required")
            if owner.get("state") == "active":
                require_delivery_support(request, owner)
                return dict(report, status="owner_registered", reason="pi_live_mailbox_registered")
            raise ValueError("The Pi process is alive but this session's mailbox is not active; reopen the bound session")
        if not confirmed_owner_dead(owner):
            raise ValueError("This ordinary Pi session is online-only; reopen it with the callback extension or inspect and ACK manually")
        return dict(report, status="offline_candidate", transport="pi_managed_resume", reason="managed_pi_owner_exited")
    except (OSError, ValueError, KeyError) as exc:
        return dict(report, reason="pi_live_channel_unavailable", action=str(exc) +
                    "; use ltc install-pi-extension and reload Pi, or ltc pi for managed sessions")


class PiDelivery:
    delivery_name = "pi_live_session"

    def __init__(self, *, lease=None, environment=None, command=None, directory=None, nonce=None,
                 previous_owner=None, request=None, queue=None):
        self.lease = lease
        self.environment = environment
        self.command = command
        self.directory = directory
        self.nonce = nonce
        self.process = None
        self.previous_owner = previous_owner
        self.request = request
        self.queue = queue

    @property
    def online(self) -> bool:
        return self.lease is None

    def wait_for_completion(self, timeout: float) -> bool:
        # Admission/session observation cannot substitute for the existing ACK.
        time.sleep(timeout)
        return False

    def close(self) -> None:
        if self.lease is not None:
            if self.process is None and self.previous_owner is not None:
                # A failed Popen is a definite pre-dispatch failure. Restore the
                # old evidence while still holding the writer lease; never do
                # this after a child has started or mailbox was published.
                try:
                    owner = read_record(self.directory / "owner.json")
                    if owner.get("owner_nonce") == self.nonce and owner.get("state") == "reserved":
                        storage.write_request(self.directory / "owner.json", self.previous_owner)
                        from . import cli
                        cli.release_retained_target_lease(self.queue, self.request)
                except (OSError, ValueError, RuntimeError):
                    pass
            if self.process is not None and self.process.poll() is not None:
                try:
                    owner = read_record(self.directory / "owner.json")
                    spawn = read_record(self.directory / f"spawn-{self.nonce}.json")
                    if (owner.get("owner_nonce") == self.nonce == spawn.get("owner_nonce")
                            and owner.get("owner_pid") == spawn.get("owner_pid")
                            and owner.get("owner_identity") is None):
                        owner["owner_identity"] = spawn["owner_identity"]
                        storage.write_request(self.directory / "owner.json", owner)
                except (OSError, ValueError, KeyError):
                    pass  # Missing evidence conservatively disables further recovery.
            self.lease[0].close()
            self.lease = None

    def monitor_owner(self, process) -> None:
        self.process = process
        record_spawn(self.directory, self.nonce, process)
        def monitor():
            while process.poll() is None:
                if _verify_owner_identity(self.directory, self.nonce):
                    return
                time.sleep(0.02)
        threading.Thread(target=monitor, daemon=True).start()


PUBLISHED_RETURN_CODE = 122  # Delivery worker: envelope is in the live mailbox; ACK is pending.
PUBLISHED_REASON = "published to the live Pi session; awaiting ACK"


def envelope_path(directory: Path, queue: Path, request_id: str) -> Path:
    key = hashlib.sha256((str(queue) + "\0" + request_id).encode("utf-8")).hexdigest()
    return directory / "inbox" / f"{key}.json"


def publication_outcome(root: Path, request: dict, timeout: float, now: float) -> str | None:
    """Why a published callback can no longer be ACKed by its live Pi, if so.

    Publication hands the callback to the live process; the daemon does not
    wait for the ACK. The receiving owner must stay the one it was published
    to: an exited, closed or replaced owner, or a missed deadline, leaves the
    outcome unknown, which is never replayed automatically.
    """
    from . import cli
    published = request.get("pi_published_at")
    if not isinstance(published, (int, float)):
        return None
    if now - published >= timeout:
        return "no ACK within the resume timeout"
    try:
        directory, owner = owner_record(request)
        envelope = read_record(envelope_path(directory, root.resolve(), str(request["id"])), versions=(1, 2))
    except (OSError, ValueError, KeyError):
        return "the live Pi mailbox or its owner can no longer be verified"
    if envelope.get("owner_nonce") != owner.get("owner_nonce"):
        return "the Pi session was reopened by another process before ACK"
    if owner.get("state") != "active" or not cli.pid_is_running(owner["owner_pid"]):
        return "the live Pi session exited before ACK"
    return None


def reconcile_publications(root: Path, timeout: float) -> None:
    """Daemon hook: settle Pi callbacks whose live owner can no longer ACK them."""
    from . import cli
    now = time.time()
    for path in sorted((root / "pending").glob("*.json")):
        try:
            request = cli.load_request(path)
        except (OSError, ValueError):
            continue
        request_id = str(request.get("id", path.stem))
        if (cli.request_agent(request) != "pi" or "pi_published_at" not in request
                or cli.ack_path(root, request_id).exists() or cli.is_canceled(root, request_id)):
            continue
        reason = publication_outcome(root, request, timeout, now)
        if reason is None:
            continue
        received = None
        try:
            directory, _ = owner_record(request)
            received = (directory / "receipts" / envelope_path(directory, root.resolve(), request_id).name).exists()
        except (OSError, ValueError, KeyError):
            pass
        evidence = {True: "Pi admitted it into the session", False: "Pi had not admitted it yet",
                    None: "admission is unknown"}[received]
        request["retain_target_lease"] = True
        request["last_error"] = (f"Pi callback outcome is unknown: {reason} ({evidence}); automatic retry "
                                 f"suppressed to prevent duplicate delivery. Inspect the session, then ACK or use "
                                 f"ltc retry --id {request_id}")
        try:
            cli.write_request(path, request)
            failed = cli.move_request(path, root / "failed")
        except FileNotFoundError:
            continue  # ACKed or canceled meanwhile.
        print(f"ltc: warning: Pi callback {request_id}: {reason}; manual recovery required", file=sys.stderr)
        if cli.ack_path(root, request_id).exists():  # ack() raced the move.
            try:
                cli.move_request(failed, root / "done")
                cli.release_retained_target_lease(root, request)
            except FileNotFoundError:
                pass


def extension_path() -> Path:
    return Path(__file__).with_name("pi_extension") / "ltc-callback.js"


def _reservation(directory: Path, session: str, profile: str) -> dict:
    from . import cli
    header = session_header(session)
    return {"version": 1, "session_file": session, "session_id": header["id"],
            "channel_dir": str(directory), "profile_dir": profile, "managed": True,
            "state": "reserved", "owner_nonce": secrets.token_hex(24),
            "owner_pid": os.getpid(), "lease_pid": os.getpid(),
            "lease_identity": cli.daemon_process_identity(os.getpid()),
            "recorded_at": time.time()}


def prepare_delivery(payload: dict) -> PiDelivery:
    from . import cli
    request = payload["request"]
    try:
        directory, owner = owner_record(request)
        session = canonical_session(request["target"]["value"])
        request_id = str(request["id"])
        queue = Path(payload["queue_dir"]).resolve()
        if not _SAFE_ID.fullmatch(request_id):
            raise ValueError("Invalid Pi callback ID")
        key = hashlib.sha256((str(queue) + "\0" + request_id).encode("utf-8")).hexdigest()
        if cli.pid_is_running(owner["owner_pid"]):
            if owner.get("managed") is True and not valid_live_lease(owner):
                raise ValueError("The Pi owner remains alive after its managed launcher exited")
            if owner.get("state") != "active":
                raise ValueError("The original Pi process is alive but the bound session is inactive")
            require_delivery_support(request, owner)
            inbox = directory / "inbox"
            ensure_private_directory(inbox)
            # Scope IDs by queue, because different queues may use the same ID.
            envelope = inbox / f"{key}.json"
            delivery = request.get("pi_delivery", "follow-up")
            content = {"version": 2 if delivery == "steer" else 1, "pi_delivery": delivery,
                       "callback_id": request_id, "queue_dir": str(queue),
                       "owner_nonce": owner["owner_nonce"], "session_file": session,
                       "prompt": str(payload["prompt"]), "ack_path": str(payload["ack_path"]),
                       "canceled_path": str(payload["canceled_path"]), "created_at": time.time()}
            cli.retain_target_lease(queue, request)
            if envelope.exists():
                old = read_record(envelope, versions=(1, 2))
                if any(old.get(k) != content[k] for k in ("callback_id", "queue_dir", "owner_nonce", "session_file")):
                    raise ValueError("Preserving a conflicting Pi callback envelope")
                if old.get("pi_delivery", "follow-up") != delivery:
                    raise ValueError("Preserving a Pi callback envelope with a different delivery policy")
            else:
                storage.write_request(envelope, content)
            return PiDelivery()

        if not confirmed_owner_dead(owner):
            raise ValueError("The original ordinary Pi process exited; automatic file recovery is disabled for unmanaged sessions")
        if any((directory / part / f"{key}.json").exists() for part in ("inbox", "receipts")):
            raise ValueError("This Pi callback was already published; inspect its outcome and ACK manually instead of replaying")
        lock_path = writer_lock_path(session)
        if lock_path.is_symlink():
            raise ValueError("Pi writer lease is a symlink")
        lease = cli.acquire_path_lock(lock_path, blocking=False)
        if lease is None:
            raise ValueError("The bound Pi session still has a managed writer lease")
        try:
            # Revalidate under the writer lease; PID reuse is conservative: any
            # process at the recorded PID prevents offline recovery.
            current_directory, current = owner_record(request)
            if current != owner or current_directory != directory or not confirmed_owner_dead(current):
                raise ValueError("Pi session ownership changed before offline recovery")
            reservation = _reservation(directory, session, request["pi_profile_dir"])
            cli.retain_target_lease(queue, request)
            storage.write_request(directory / "owner.json", reservation)
            environment = dict(os.environ, **{PROFILE_ENV: request["pi_profile_dir"],
                                             CHANNEL_ROOT_ENV: request["pi_channel_root"],
                                             RESERVATION_ENV: reservation["owner_nonce"]})
            environment["LTC_PI_RECOVERY"] = "1"
            environment["LTC_PI_CALLBACK_ACK_PATH"] = str(payload["ack_path"])
            environment["LTC_PI_CALLBACK_CANCEL_PATH"] = str(payload["canceled_path"])
            command = [str(part) for part in payload["command"]]
            command.extend(["--extension", str(extension_path())])
            return PiDelivery(lease=lease, environment=environment, command=command,
                              directory=directory, nonce=reservation["owner_nonce"],
                              previous_owner=owner, request=request, queue=queue)
        except BaseException:
            lease[0].close()
            raise
    except (OSError, ValueError, KeyError) as exc:
        raise cli.CallbackTransportBlocked(str(exc)) from exc


def install_extension(args: argparse.Namespace) -> int:
    profile = Path(getattr(args, "profile", None) or profile_dir()).expanduser().resolve()
    target = profile / "extensions" / "ltc-callback.js"
    source = resources.files("long_task_callback").joinpath("pi_extension").joinpath("ltc-callback.js").read_bytes()
    if target.is_symlink():
        raise ValueError(f"Preserving an existing extension symlink: {target}")
    if target.exists() and target.read_bytes() != source and not getattr(args, "force", False):
        raise ValueError(f"Preserving an existing Pi extension: {target}; review it before using --force")
    if not target.exists() or target.read_bytes() != source:
        ensure_private_directory(target.parent)
        storage.write_private_text(target, source.decode("utf-8"), preserve_newlines=True)
    # Windows children inherit the protected ACL initialized by the installer.
    ensure_private_directory(profile / "long-task-callback" / "channels")
    print(f"Installed Pi callback extension: {target}")
    from . import cli
    skill = profile / "skills" / "long-task-callback"
    if cli.install_skill_tree(skill, include_codex_plugin=False, force=bool(getattr(args, "force", False)),
                              keep_existing=not getattr(args, "force", False), focused="pi_skill") != 0:
        raise ValueError(f"Could not install the Pi skill at {skill}")
    print("Existing Pi processes need /reload or a restart. Ordinary Pi receives online callbacks; use ltc pi for managed offline recovery.")
    return 0


def _fresh_session(profile: Path, cwd: Path) -> str:
    directory = profile / "sessions" / "ltc-managed"
    ensure_private_directory(directory)
    stamp = time.strftime("%Y-%m-%dT%H-%M-%S", time.gmtime())
    session_id = str(uuid.uuid4())
    session = canonical_session(str(directory / f"{stamp}_{session_id}.jsonl"))
    # Pi's documented JSONL header; select and lease the path before creating it.
    return session


def _verify_owner_identity(directory: Path, nonce: str) -> bool:
    from . import cli
    try:
        owner = read_record(directory / "owner.json")
        if owner.get("owner_nonce") != nonce or owner.get("state") not in ("active", "closed"):
            return False
        pid = owner.get("owner_pid")
        if type(pid) is not int or pid <= 0 or not cli.pid_is_running(pid):
            return False
        if owner.get("owner_identity") is None:
            identity = cli.daemon_process_identity(pid)
            if identity is None:
                return False
            owner["owner_identity"] = identity
            storage.write_request(directory / "owner.json", owner)
        return True
    except (OSError, ValueError):
        return False


def record_spawn(directory: Path, nonce: str, process) -> None:
    """Capture the spawned PID while print-mode Pi is waiting on stdin.

    The extension copies matching evidence into its owner record. A separate
    file avoids racing its active/closed transitions on short callback turns.
    Windows command shims may spawn Node under another PID; the monitor then
    verifies the actual Node owner instead, and absence of evidence blocks recovery.
    """
    from . import cli
    identity = cli.daemon_process_identity(process.pid)
    if identity is not None:
        storage.write_request(directory / f"spawn-{nonce}.json", {
            "version": 1, "owner_nonce": nonce, "owner_pid": process.pid, "owner_identity": identity})


def managed_pi(args: argparse.Namespace) -> int:
    from . import cli
    forwarded = list(args.pi_args)
    if forwarded[:1] == ["--"]:
        forwarded.pop(0)
    forbidden = {"--session", "-c", "--continue", "-r", "--resume", "--session-dir", "--fork", "--no-session"}
    if any(part.split("=", 1)[0] in forbidden for part in forwarded):
        raise ValueError("Choose the managed session with ltc pi --session; forwarded Pi options cannot change session ownership")
    cwd = Path(args.cwd).expanduser().resolve()
    if not cwd.is_dir():
        raise ValueError("Pi working directory is not a directory")
    route = route_fields()
    profile = Path(route["pi_profile_dir"])
    session = canonical_session(args.session) if args.session else _fresh_session(profile, cwd)
    lock_path = writer_lock_path(session)
    if lock_path.is_symlink():
        raise ValueError("Pi writer lease is a symlink")
    lease = cli.acquire_path_lock(lock_path, blocking=False)
    if lease is None:
        raise ValueError("This session already has an LTC-managed Pi writer")
    process = None
    directory = channel_dir(session, Path(route["pi_channel_root"]))
    reservation = None
    try:
        ensure_private_directory(Path(route["pi_channel_root"]))
        ensure_private_directory(directory)
        if not Path(session).exists():
            if args.session:
                raise ValueError("The requested Pi session does not exist")
            storage.write_request(Path(session), {"type": "session", "version": 3,
                                  "id": str(uuid.uuid4()), "cwd": str(cwd),
                                  "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())})
        session_header(session)
        try:
            existing = read_record(directory / "owner.json")
        except FileNotFoundError:
            existing = None
        if existing and (type(existing.get("owner_pid")) is not int or existing["owner_pid"] <= 0
                         or cli.pid_is_running(existing["owner_pid"])):
            raise ValueError("The previous Pi process is still alive; reopen it instead of starting another writer")
        if existing and existing.get("managed") is True and not confirmed_owner_dead(existing):
            raise ValueError("Cannot confirm the previous managed Pi owner exited in this host/process scope; inspect ownership manually")
        reservation = _reservation(directory, session, route["pi_profile_dir"])
        storage.write_request(directory / "owner.json", reservation)
        environment = dict(os.environ, **{PROFILE_ENV: route["pi_profile_dir"],
                                         CHANNEL_ROOT_ENV: route["pi_channel_root"],
                                         RESERVATION_ENV: reservation["owner_nonce"]})
        environment.pop("LTC_PI_RECOVERY", None)
        command = [cli.get_agent("pi").executable(environment), "--session", session,
                   "--extension", str(extension_path()), *forwarded]
        print(f"Pi session: {session}", file=sys.stderr)
        process = subprocess.Popen(cli.process_command(command), cwd=cwd, env=environment)
        record_spawn(directory, reservation["owner_nonce"], process)
        deadline = time.monotonic() + 15
        verified = False
        while process.poll() is None:
            try:
                if not verified:
                    verified = _verify_owner_identity(directory, reservation["owner_nonce"])
                    if not verified and time.monotonic() >= deadline:
                        raise RuntimeError("Pi callback extension did not establish managed session ownership")
                time.sleep(0.05)
            except KeyboardInterrupt:
                # The foreground Pi receives the same terminal interrupt. Keep
                # its lease until it exits instead of orphaning a live writer.
                continue
        return int(process.returncode)
    finally:
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        if reservation is not None:
            try:
                owner = read_record(directory / "owner.json")
                if owner.get("owner_nonce") == reservation["owner_nonce"]:
                    owner["state"] = "closed"
                    storage.write_request(directory / "owner.json", owner)
            except (OSError, ValueError):
                pass
        lease[0].close()
