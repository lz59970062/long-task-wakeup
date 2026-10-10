"""Publish named templates and small discoverable skills from one registration.

The registry owns generated entrypoints, not the user's main LTC instructions.
Only a delimited index in those instructions is refreshed. Task execution still
uses the existing template loader and durable child-agent lifecycle.
"""

from __future__ import annotations

import contextlib
import hashlib
import importlib.resources as resources
import json
import os
from pathlib import Path
import re
import shlex
from dataclasses import dataclass, field

import yaml

from . import storage
from .agents import get_child_agent
from .platforms import posix, windows_io
from .templates import _UniqueSafeLoader, load_template, user_template_dir

INDEX_BEGIN = "<!-- ltc:registered-templates:begin -->"
INDEX_END = "<!-- ltc:registered-templates:end -->"
REGISTRY_NAME = ".ltc-registrations.json"


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _read(path: Path) -> str | None:
    if path.is_symlink():
        raise ValueError(f"refusing to replace a symlink: {path}")
    try:
        with path.open(encoding="utf-8", newline="") as handle:
            return handle.read()
    except FileNotFoundError:
        return None


def _registry() -> dict:
    path = user_template_dir() / REGISTRY_NAME
    text = _read(path)
    if text is None:
        return {"version": 1, "templates": {}}
    try:
        data = json.loads(text)
        if data.get("version") != 1 or not isinstance(data.get("templates"), dict):
            raise ValueError("unsupported registration registry")
        for name, entry in data["templates"].items():
            _name(name)
            if (not isinstance(entry, dict)
                    or not isinstance(entry.get("skill_roots"), list)
                    or not all(isinstance(p, str) and Path(p).is_absolute() for p in entry["skill_roots"])
                    or not isinstance(entry.get("files"), dict)
                    or not isinstance(entry.get("aliases"), dict)
                    or not isinstance(entry.get("parent_homes"), dict)
                    or not isinstance(entry.get("description"), str)):
                raise ValueError(f"invalid registration for {name}")
            get_child_agent(entry.get("worker"))
        return data
    except (ValueError, AttributeError) as exc:
        raise ValueError(f"cannot read registration registry {path}: {exc}") from exc


def _name(name: str) -> None:
    if (not isinstance(name, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,58}", name)
            or name.endswith("-") or "--" in name):
        raise ValueError("registered template names must use lowercase letters, digits and hyphens (up to 59 characters)")


def _roots(target: str) -> tuple[list[Path], list[tuple[Path, Path]]]:
    roots = []
    aliases = []
    if target in ("codex", "both"):
        home = Path(os.environ.get("CODEX_HOME", "~/.codex")).expanduser().resolve()
        default = home == (Path.home() / ".codex").resolve()
        root = home / "skills"
        if default and os.name == "nt":
            root = Path.home() / ".agents" / "skills"
        roots.append(root)
        if default and os.name != "nt":
            aliases.append((Path.home() / ".agents" / "skills", root))
    if target in ("claude", "both"):
        roots.append(Path(os.environ.get("CLAUDE_CONFIG_DIR", "~/.claude")).expanduser().resolve() / "skills")
    return roots, aliases


def _namespace_markers() -> tuple[str, str]:
    namespace = _digest(str(user_template_dir().resolve()))
    prefix = f"<!-- ltc:registered-templates:namespace:{namespace}"
    return f"{prefix}:begin -->", f"{prefix}:end -->"


def _index_range(text: str) -> tuple[int, int] | None:
    if INDEX_BEGIN not in text and INDEX_END not in text:
        return None
    if text.count(INDEX_BEGIN) != 1 or text.count(INDEX_END) != 1:
        raise ValueError("LTC registered-template index markers are damaged; preserve and repair them before registration")
    start, end = text.index(INDEX_BEGIN), text.index(INDEX_END)
    if end < start:
        raise ValueError("LTC registered-template index markers are reversed")
    return start + len(INDEX_BEGIN), end


def _index(text: str, entries: dict, root: Path) -> str:
    canonical_root = root.resolve()
    selected = [(name, entry) for name, entry in sorted(entries.items())
                if any(Path(path).resolve() == canonical_root for path in entry.get("skill_roots", []))]
    namespace_begin, namespace_end = _namespace_markers()
    lines = [namespace_begin]
    for name, entry in selected:
        lines.append(f"- `{name}`: {entry['description']} Read [ltc-{name}](../ltc-{name}/SKILL.md) to delegate to {entry['worker']}.")
    lines.append(namespace_end)
    namespace_block = "\n".join(lines) if selected else ""
    index_range = _index_range(text)
    if index_range is not None:
        content_start, end = index_range
        content = text[content_start:end]
        if namespace_begin in content or namespace_end in content:
            if content.count(namespace_begin) != 1 or content.count(namespace_end) != 1:
                raise ValueError("LTC registration namespace markers are damaged; preserve and repair them before registration")
            namespace_start = content.index(namespace_begin)
            namespace_finish = content.index(namespace_end)
            if namespace_finish < namespace_start:
                raise ValueError("LTC registration namespace markers are reversed")
            content = (content[:namespace_start] + namespace_block
                       + content[namespace_finish + len(namespace_end):])
        elif namespace_block:
            # Retain other namespaces and any older unlabelled index content.
            content += ("" if content.endswith("\n") else "\n") + namespace_block + "\n"
        return text[:content_start] + content + text[end:]
    if not selected:
        return text
    block = f"{INDEX_BEGIN}\n{namespace_block}\n{INDEX_END}"
    return text + ("\n" if text.endswith("\n") else "\n\n") + "## Registered task templates\n\n" + block + "\n"


def _skill(name: str, entry: dict, root: Path) -> str:
    metadata = yaml.safe_dump({"name": f"ltc-{name}", "description": entry["description"]},
                              sort_keys=False, allow_unicode=True).rstrip()
    template = shlex.quote(entry["template_file"])
    return (f"---\n{metadata}\n---\n\n"
            f"<!-- ltc:template-entry:{name} -->\n"
            f"<!-- ltc:template-owner:{_digest(str(user_template_dir().resolve()))} -->\n"
            f"# {name}\n\n"
            f"Delegate this task to a fresh {entry['worker']} child using the registered LTC template.\n"
            f"Read [the LTC workflow]({root / 'long-task-callback' / 'SKILL.md'}) before submission\n"
            "for readiness checks, parent-session callbacks and acknowledgement.\n\n"
            "Pass the user's actual requirements, scope, target paths and evidence constraints.\n"
            "Distinguish review-only requests from authorized file edits.\n\n"
            "```bash\n"
            f"ltc agent {entry['worker']} --template-file {template} --cwd /absolute/path/to/workspace "
            '  -- "The user\'s concrete task requirements."\n'
            "```\n\n"
            f"Installed template: `{entry['template_file']}`. This file selects the registered\n"
            "configuration even when another workspace sets a different LTC_TEMPLATE_DIR.\n"
            "Inspect the actual result and edits, ACK the callback, and continue the parent task.\n")


@dataclass
class _Plan:
    writes: dict[Path, str | None] = field(default_factory=dict)
    before: dict[Path, str | None] = field(default_factory=dict)
    resolved: dict[Path, Path] = field(default_factory=dict)
    links: dict[Path, Path] = field(default_factory=dict)
    unlink: list[Path] = field(default_factory=list)

    def write(self, path: Path, text: str | None) -> None:
        path = path.absolute()
        self.before[path] = _read(path)
        self.resolved[path] = path.resolve()
        if self.before[path] != text:
            self.writes[path] = text

    def link(self, path: Path, destination: Path) -> None:
        if path.is_symlink():
            if path.resolve() != destination.resolve():
                raise ValueError(f"preserving an unrelated skill alias: {path}")
        elif path.exists():
            raise ValueError(f"preserving an existing skill directory: {path}")
        else:
            self.links[path] = destination

    def apply(self) -> None:
        completed = []
        linked = []
        removed = []
        try:
            for path, original in self.before.items():
                if path.resolve() != self.resolved[path] or _read(path) != original:
                    raise ValueError(f"file changed during registration: {path}")
            ordered = [(p, text) for p, text in self.writes.items() if p.name != REGISTRY_NAME]
            for path, text in ordered:
                # Record before publication: a directory fsync can fail after replace.
                completed.append(path)
                if text is None:
                    path.unlink(missing_ok=True)
                else:
                    storage.write_private_text(path, text, preserve_newlines=True)
            for path, destination in self.links.items():
                path.parent.mkdir(parents=True, exist_ok=True)
                path.symlink_to(destination, target_is_directory=True)
                linked.append(path)
            for path in self.unlink:
                destination = Path(os.readlink(path))
                path.unlink()
                removed.append((path, destination))
            for path, text in self.writes.items():
                if path.name == REGISTRY_NAME:
                    completed.append(path)
                    storage.write_private_text(path, text, preserve_newlines=True)
        except BaseException as exc:
            failures = []
            for path, destination in reversed(removed):
                try:
                    path.symlink_to(destination, target_is_directory=True)
                except OSError:
                    failures.append(str(path))
            for path in reversed(linked):
                try:
                    path.unlink()
                except OSError:
                    failures.append(str(path))
            for path in reversed(completed):
                original = self.before[path]
                try:
                    if original is None:
                        path.unlink(missing_ok=True)
                    else:
                        storage.write_private_text(path, original, preserve_newlines=True)
                except OSError:
                    failures.append(str(path))
            if failures:
                raise RuntimeError(f"registration failed ({exc}); could not restore: {', '.join(failures)}") from exc
            raise

    def summary(self) -> dict:
        return {"write": [str(path) for path, value in self.writes.items() if value is not None],
                "remove": [str(path) for path, value in self.writes.items() if value is None] + [str(p) for p in self.unlink],
                "aliases": {str(path): str(destination) for path, destination in self.links.items()}}


def _owned_write(plan: _Plan, path: Path, text: str, old: dict, *, force: bool) -> None:
    current = _read(path)
    previous = old.get("files", {}).get(str(path))
    if current is not None and current != text and _digest(current) != previous and not force:
        raise ValueError(f"preserving an existing or edited file: {path}; use --force to replace it")
    plan.write(path, text)


def _refresh_indexes(plan: _Plan, entries: dict, roots: list[Path], *, install_missing: bool = False) -> None:
    for root in sorted(set(roots)):
        path = (root / "long-task-callback" / "SKILL.md").resolve()
        text = _read(path)
        if text is None and install_missing:
            package = resources.files("long_task_callback").joinpath("skill")
            text = package.joinpath("SKILL.md").read_text(encoding="utf-8")
            if any(entry.get("parent_homes", {}).get("codex") == str(root) for entry in entries.values()):
                metadata = path.parent / "agents" / "openai.yaml"
                if _read(metadata) is None:
                    plan.write(metadata, package.joinpath("agents").joinpath("openai.yaml").read_text(encoding="utf-8"))
        if text is not None:
            plan.write(path, _index(text, entries, root))


def _save_registry(plan: _Plan, data: dict) -> None:
    # This publication is last, after every entrypoint and dependency is valid.
    plan.write(user_template_dir() / REGISTRY_NAME, json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n")


def _register_plan(args) -> tuple[_Plan, dict]:
    _name(args.name)
    source = load_template(template_file=args.file) if args.file else load_template(args.name)
    if source.source.startswith("builtin:"):
        raise ValueError("registration requires a user YAML template; use --file to supply one")
    worker = args.worker or source.worker
    if worker is None:
        raise ValueError("registration requires worker in the YAML or --worker")
    get_child_agent(worker)
    description = args.description or source.description
    if not isinstance(description, str) or not description.strip():
        raise ValueError("registration requires description in the YAML or --description")
    description = " ".join(description.split())
    if len(description) > 1024:
        raise ValueError("registration description must be at most 1024 characters")
    if "<!-- ltc:" in description:
        raise ValueError("registration description cannot contain reserved LTC index markers")
    data = _registry()
    old = data["templates"].get(args.name, {})
    roots, alias_roots = _roots(args.target)
    parents = (["codex", "claude"] if args.target == "both" else [args.target])
    parent_homes = dict(old.get("parent_homes", {}))
    for parent, root in zip(parents, roots):
        previous = parent_homes.get(parent)
        if previous is not None and previous != str(root):
            raise ValueError(f"{args.name} is already registered in another {parent} home; use its original profile or a separate LTC_TEMPLATE_DIR")
        parent_homes[parent] = str(root)
    # Existing targets are retained when updating a registration.
    roots = list(dict.fromkeys([*map(Path, old.get("skill_roots", [])), *roots]))
    for root in roots:
        previous = old.get("root_identities", {}).get(str(root))
        if previous is not None and previous != str(root.resolve()):
            raise ValueError(f"preserving a changed skill directory: {root}")
    directory = user_template_dir()
    if (directory / f"{args.name}.yml").exists() or (directory / f"{args.name}.yml").is_symlink():
        raise ValueError(f"preserving existing {args.name}.yml; move it before registering a .yaml template")
    template_path = directory / f"{args.name}.yaml"
    raw = yaml.load(Path(source.source).read_text(encoding="utf-8"), Loader=_UniqueSafeLoader)
    raw["worker"], raw["description"] = worker, description
    files = {}
    for agent, settings in source.agent_defaults.items():
        if "system_prompt_file" not in settings:
            continue
        path = source.system_prompt_file_for(agent)
        if not path.is_file():
            raise ValueError(f"system prompt must be a readable regular file: {path}")
        with path.open(encoding="utf-8-sig", newline="") as handle:
            text = handle.read()
        if not text.strip() or "\x00" in text:
            raise ValueError(f"system prompt must contain non-empty UTF-8 text: {path}")
        asset_root = directory / ".ltc-assets"
        if asset_root.is_symlink() or (asset_root / args.name).is_symlink():
            raise ValueError(f"preserving an existing template asset symlink: {asset_root / args.name}")
        destination = asset_root / args.name / f"system-prompt-{agent}.md"
        files[destination] = text
        raw[agent]["system_prompt_file"] = destination.relative_to(directory).as_posix()
    files[template_path] = yaml.safe_dump(raw, sort_keys=False, allow_unicode=True)
    entry = {"worker": worker, "description": description, "version": source.version,
             "template_file": str(template_path), "source": old.get("source", source.source) if source.source == str(template_path) else source.source,
             "skill_roots": [str(root) for root in roots], "files": {},
             "parent_homes": parent_homes,
             "root_identities": {str(root): str(root.resolve()) for root in roots},
             "aliases": dict(old.get("aliases", {}))}
    plan = _Plan()
    for root in roots:
        skill_path = root / f"ltc-{args.name}" / "SKILL.md"
        parent = skill_path.parent
        # A registration cannot claim an unrelated skill, even with --force.
        if parent.is_symlink():
            raise ValueError(f"preserving an existing skill symlink: {parent}")
        current = _read(skill_path)
        owner = f"<!-- ltc:template-owner:{_digest(str(directory.resolve()))} -->"
        if current is not None and (f"<!-- ltc:template-entry:{args.name} -->" not in current or owner not in current):
            raise ValueError(f"preserving an unrelated skill: {skill_path}")
        if parent.exists() and current is None and any(parent.iterdir()):
            raise ValueError(f"preserving an existing skill directory: {parent}")
        files[skill_path] = _skill(args.name, entry, root)
        if str(root) == parent_homes.get("codex"):
            if (parent / "agents").is_symlink():
                raise ValueError(f"preserving an existing metadata symlink: {parent / 'agents'}")
            interface = {"display_name": f"{args.name.replace('-', ' ').title()} ({worker.title()})",
                         "short_description": f"Delegate {args.name[:30]} to {worker} with LTC",
                         "default_prompt": f"Use $ltc-{args.name} to delegate this task to the registered {worker} child agent."}
            files[parent / "agents" / "openai.yaml"] = yaml.safe_dump({"interface": interface}, sort_keys=False, allow_unicode=True)
    for path, text in files.items():
        _owned_write(plan, path, text, old, force=args.force)
        entry["files"][str(path)] = _digest(text)
    for alias_root, root in alias_roots:
        alias, destination = alias_root / f"ltc-{args.name}", root / f"ltc-{args.name}"
        plan.link(alias, destination)
        entry["aliases"][str(alias)] = str(destination)
    data["templates"][args.name] = entry
    _refresh_indexes(plan, data["templates"], roots, install_missing=True)
    _save_registry(plan, data)
    return plan, entry


def _unregister_plan(name: str) -> tuple[_Plan, dict | None]:
    _name(name)
    data = _registry()
    entry = data["templates"].get(name)
    plan = _Plan()
    if entry is None:
        return plan, None
    roots = list(map(Path, entry["skill_roots"]))
    for root in roots:
        previous = entry.get("root_identities", {}).get(str(root))
        if previous is not None and previous != str(root.resolve()):
            raise ValueError(f"preserving a changed skill directory: {root}")
        path = root / f"ltc-{name}" / "SKILL.md"
        if path.parent.is_symlink():
            raise ValueError(f"preserving a changed entrypoint directory: {path.parent}")
        current = _read(path)
        if current is not None and _digest(current) != entry["files"].get(str(path)):
            raise ValueError(f"preserving edited entrypoint: {path}; restore it before unregistering")
        plan.write(path, None)
        metadata = path.parent / "agents" / "openai.yaml"
        if str(metadata) in entry["files"]:
            if metadata.parent.is_symlink():
                raise ValueError(f"preserving a changed metadata directory: {metadata.parent}")
            current = _read(metadata)
            if current is not None and _digest(current) != entry["files"][str(metadata)]:
                raise ValueError(f"preserving edited entrypoint metadata: {metadata}; restore it before unregistering")
            plan.write(metadata, None)
    for path_text, destination in entry.get("aliases", {}).items():
        path = Path(path_text)
        if path.is_symlink() and path.resolve() == Path(destination).resolve():
            plan.unlink.append(path)
        elif path.exists() or path.is_symlink():
            raise ValueError(f"preserving changed skill alias: {path}")
    del data["templates"][name]
    _refresh_indexes(plan, data["templates"], roots)
    _save_registry(plan, data)
    return plan, entry


def _lock_paths(roots) -> list[Path]:
    paths = {user_template_dir().resolve() / ".ltc-registration.lock"}
    for root in roots:
        root = Path(root).resolve()
        paths.add(root / ".ltc-registration.lock")
        # A main workflow directory may itself link to another shared skill root.
        workflow_root = (root / "long-task-callback" / "SKILL.md").resolve().parent.parent
        paths.add(workflow_root / ".ltc-registration.lock")
    return sorted(paths, key=str)


@contextlib.contextmanager
def _lock(roots=()):
    platform = windows_io if os.name == "nt" else posix
    leases = []
    paths = _lock_paths(roots)
    try:
        for path in paths:
            if path.is_symlink():
                raise ValueError(f"preserving an existing registration lock symlink: {path}")
            lease = platform.acquire_path_lock(path, blocking=False)
            if lease is None:
                raise ValueError("another template registration is running; retry after it completes")
            leases.append(lease)
        yield frozenset(paths)
    finally:
        for lease in reversed(leases):
            lease[0].close()


def _require_locked_roots(roots, locked_paths) -> None:
    if not set(_lock_paths(roots)).issubset(locked_paths):
        raise ValueError("registered parent homes changed during registration; retry with the current profile")


def register(args) -> dict:
    # Validate the whole plan before creating even the lock file.
    plan, entry = _register_plan(args)
    if not args.dry_run:
        with _lock(entry["skill_roots"]) as locked_paths:
            plan, entry = _register_plan(args)
            _require_locked_roots(entry["skill_roots"], locked_paths)
            plan.apply()
    return {"name": args.name, "skill": f"ltc-{args.name}", "worker": entry["worker"],
            "template_file": entry["template_file"], "dry_run": args.dry_run,
            "changes": plan.summary()}


def unregister(args) -> dict:
    plan, entry = _unregister_plan(args.name)
    if entry is not None and not args.dry_run:
        with _lock(entry["skill_roots"]) as locked_paths:
            plan, entry = _unregister_plan(args.name)
            if entry is not None:
                _require_locked_roots(entry["skill_roots"], locked_paths)
            plan.apply()
    return {"name": args.name, "registered": False, "dry_run": args.dry_run,
            "template_files_preserved": True, "changes": plan.summary()}


def list_templates() -> list[dict]:
    return [{"name": name, "skill": f"ltc-{name}", **entry}
            for name, entry in sorted(_registry()["templates"].items())]


@contextlib.contextmanager
def preserve_installed_index(target: Path):
    """Hold shared locks across installation and retain every registry's index."""
    root = target.parent.resolve()
    with _lock([root]) as locked_paths:
        # Reject unreadable state before the caller replaces the installed tree.
        entries = _registry()["templates"]
        skill_path = target / "SKILL.md"
        previous = _read(skill_path)
        previous_range = _index_range(previous) if previous is not None else None
        preserved = previous[slice(*previous_range)] if previous_range is not None else None
        if previous is not None:
            _index(previous, entries, root)
        yield
        _require_locked_roots([root], locked_paths)
        text = _read(skill_path)
        if text is None:
            raise ValueError(f"installed skill is missing: {skill_path}")
        if preserved is not None:
            current_range = _index_range(text)
            if current_range is not None:
                start, end = current_range
                text = text[:start] + preserved + text[end:]
            else:
                text += (("\n" if text.endswith("\n") else "\n\n")
                         + "## Registered task templates\n\n"
                         + INDEX_BEGIN + preserved + INDEX_END + "\n")
        entries = _registry()["templates"]
        updated = _index(text, entries, root)
        if updated != _read(skill_path):
            storage.write_private_text(skill_path, updated, preserve_newlines=True)


def refresh_installed_skill(target: Path) -> None:
    """Reapply the current registry's index without changing other namespaces."""
    if not _registry()["templates"]:
        return
    with preserve_installed_index(target):
        pass
