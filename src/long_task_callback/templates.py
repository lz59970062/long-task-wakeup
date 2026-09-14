"""Versioned child-agent task profiles; resolved prompts are saved at submission."""

from pathlib import Path

TEMPLATES = {"test": {"version": 1, "codex_model": "gpt-5.6-luna", "codex_effort": "max"}}


def render_template(name: str, requirements: str) -> str:
    if name not in TEMPLATES:
        raise ValueError(f"unknown agent template {name!r}")
    instructions = (Path(__file__).parent / "templates" / f"{name}.md").read_text(encoding="utf-8")
    return instructions.rstrip() + "\n\n## Task requirements supplied by the parent\n\n" + requirements + "\n"
