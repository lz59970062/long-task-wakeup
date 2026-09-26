"""Versioned child-agent task profiles; resolve user files only at submission."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .agents import AGENT_NAMES, get_agent

TEMPLATES = {"test": {"version": 1, "codex_model": "gpt-5.6-luna", "codex_effort": "max"}}
REASONING_EFFORTS = ("minimal", "low", "medium", "high", "xhigh", "max", "ultra")


@dataclass(frozen=True)
class AgentTemplate:
    name: str
    version: int
    prompt: str
    source: str
    codex_model: str | None = None
    codex_effort: str | None = None
    claude_model: str | None = None
    handoff: str | None = None
    agent_defaults: dict[str, dict[str, str]] = field(default_factory=dict)

    def render(self, requirements: str) -> str:
        return self.prompt.rstrip() + "\n\n## Task requirements supplied by the parent\n\n" + requirements + "\n"

    def model_for(self, agent: str) -> str | None:
        get_agent(agent)
        # Keep the original fields for existing callers and built-in profiles.
        legacy = {"codex": self.codex_model, "claude": self.claude_model}
        return self.agent_defaults.get(agent, {}).get("model", legacy.get(agent))

    def reasoning_effort_for(self, agent: str) -> str | None:
        if not get_agent(agent).supports_reasoning_effort:
            return None
        legacy = self.codex_effort if agent == "codex" else None
        return self.agent_defaults.get(agent, {}).get("reasoning_effort", legacy)


class _UniqueSafeLoader(yaml.SafeLoader):
    """Reject duplicate keys rather than silently choosing a configuration."""

    def construct_mapping(self, node, deep=False):
        if isinstance(node, yaml.MappingNode):
            seen = set()
            for key_node, _ in node.value:
                key = self.construct_object(key_node, deep=deep)
                if not isinstance(key, str):
                    raise ValueError("template mapping keys must be strings")
                if key in seen:
                    raise ValueError(f"duplicate template key {key!r}")
                seen.add(key)
        return super().construct_mapping(node, deep=deep)


def user_template_dir() -> Path:
    override = os.environ.get("LTC_TEMPLATE_DIR")
    if override:
        return Path(override).expanduser().absolute()
    config = os.environ.get("XDG_CONFIG_HOME")
    root = Path(config).expanduser() if config else Path.home() / ".config"
    return (root / "ltc" / "templates").absolute()


def _validate_name(name: str) -> None:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", name):
        raise ValueError("template name must start with a letter or digit and contain only letters, digits, _ or -")


def _mapping(value: object, allowed: set[str], label: str) -> dict:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a mapping")
    unknown = value.keys() - allowed
    if unknown:
        raise ValueError(f"unknown {label} fields: {', '.join(sorted(unknown))}")
    return value


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty string")
    return value


def _load_user_template(path: Path, name: str) -> AgentTemplate:
    path = path.expanduser().absolute()
    if path.suffix not in (".yaml", ".yml"):
        raise ValueError("custom template files must use .yaml or .yml")
    try:
        data = yaml.load(path.read_text(encoding="utf-8"), Loader=_UniqueSafeLoader)
        data = _mapping(data, {"version", "prompt", "handoff", *AGENT_NAMES}, "template")
        version = data.get("version")
        if type(version) is not int or version < 1:
            raise ValueError("template version must be a positive integer")
        prompt = _text(data.get("prompt"), "template prompt")
        agent_defaults = {}
        for agent in AGENT_NAMES:
            allowed = {"model"}
            if get_agent(agent).supports_reasoning_effort:
                allowed.add("reasoning_effort")
            settings = _mapping(data.get(agent, {}), allowed, agent)
            defaults = {key: _text(value, f"{agent} {key}") for key, value in settings.items()}
            effort = defaults.get("reasoning_effort")
            if effort is not None and effort not in REASONING_EFFORTS:
                raise ValueError(f"unsupported {agent} reasoning_effort {effort!r}")
            agent_defaults[agent] = defaults
        codex = agent_defaults.get("codex", {})
        claude = agent_defaults.get("claude", {})
        handoff = _text(data["handoff"], "template handoff") if "handoff" in data else None
        return AgentTemplate(
            name, version, prompt, str(path),
            codex.get("model"), codex.get("reasoning_effort"), claude.get("model"), handoff,
            agent_defaults=agent_defaults,
        )
    except (OSError, ValueError, yaml.YAMLError) as exc:
        raise ValueError(f"cannot load template {path}: {exc}") from exc


def load_template(name: str | None = None, *, template_file: str | None = None) -> AgentTemplate:
    if template_file is not None:
        if name is not None:
            raise ValueError("use either --template or --template-file, not both")
        path = Path(template_file).expanduser()
        _validate_name(path.stem)
        return _load_user_template(path, path.stem)
    if name is None:
        raise ValueError("a template name or file is required")
    _validate_name(name)
    directory = user_template_dir()
    # lstat catches dangling symlinks without hiding permission errors: a broken
    # or unreadable override must not silently fall back to the built-in.
    candidates = [directory / f"{name}{suffix}" for suffix in (".yaml", ".yml")]
    existing = []
    for path in candidates:
        try:
            path.lstat()
        except FileNotFoundError:
            continue
        existing.append(path)
    if len(existing) > 1:
        raise ValueError(f"ambiguous template {name!r}: both .yaml and .yml exist in {directory}")
    if existing:
        return _load_user_template(existing[0], name)
    if name not in TEMPLATES:
        raise ValueError(f"unknown agent template {name!r}; searched {directory} and built-in templates")
    defaults = TEMPLATES[name]
    prompt = (Path(__file__).parent / "templates" / f"{name}.md").read_text(encoding="utf-8")
    return AgentTemplate(name, defaults["version"], prompt, f"builtin:{name}",
                         defaults.get("codex_model"), defaults.get("codex_effort"))


def render_template(name: str, requirements: str) -> str:
    return load_template(name).render(requirements)
