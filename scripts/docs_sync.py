"""Check translation coverage and export the public guides for GitBook."""

import argparse
import json
from pathlib import Path
import re
import subprocess

from mkdocs.config import load_config


ROOT = Path(__file__).resolve().parents[1]


def pages_in(nav):
    for item in nav:
        for title, target in item.items():
            if isinstance(target, list):
                yield from pages_in(target)
            elif target.endswith(".md") and "://" not in target:
                yield title, target


def source_path(path, locale):
    source = ROOT / "docs" / path
    return source if locale == "zh" else source.with_suffix(f".{locale}.md")


def check(config):
    pages = list(pages_in(config.nav))
    missing = [
        str(source_path(path, locale).relative_to(ROOT))
        for _, path in pages
        for locale in ("zh", "en")
        if not source_path(path, locale).is_file()
    ]
    if missing:
        raise SystemExit("Missing translations:\n" + "\n".join(missing))
    print(f"Translation coverage: {len(pages)} pages × 2 languages")
    return pages


def portable_markdown(content):
    """Convert Material buttons and content tabs into plain Markdown."""
    content = re.sub(r"\{\s*\.md-button[^}]*\}", "", content)
    lines = []
    in_tab = False
    for line in content.splitlines():
        tab = re.fullmatch(r'=== "(.+)"', line)
        if tab:
            lines.append("### " + tab.group(1))
            in_tab = True
        elif in_tab and line.startswith("    "):
            lines.append(line[4:])
        else:
            if line.strip():
                in_tab = False
            lines.append(line)
    return "\n".join(lines).rstrip() + "\n"


def summary_lines(nav, translations, depth=0):
    for item in nav:
        for title, target in item.items():
            title = translations.get(title, title)
            if isinstance(target, list):
                yield f"\n{'#' * (depth + 2)} {title}\n"
                yield from summary_lines(target, translations, depth + 1)
            else:
                yield f"- [{title}]({target})"


def export(config, output):
    pages = check(config)
    revision = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
    ).strip()
    for locale in ("zh", "en"):
        folder = output / locale
        folder.mkdir(parents=True, exist_ok=True)
        for _, path in pages:
            destination = folder / path
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(
                portable_markdown(source_path(path, locale).read_text(encoding="utf-8")),
                encoding="utf-8",
            )
        lang = next(
            item for item in config.plugins["i18n"].config.languages
            if item.locale == locale
        )
        summary = "# Summary\n\n" + "\n".join(
            summary_lines(config.nav, lang.nav_translations or {})
        ) + "\n"
        (folder / "SUMMARY.md").write_text(summary, encoding="utf-8")
        (folder / ".gitbook.yaml").write_text(
            "root: ./\nstructure:\n  readme: index.md\n  summary: SUMMARY.md\n",
            encoding="utf-8",
        )
        (folder / "sync-manifest.json").write_text(
            json.dumps({"source_commit": revision, "language": locale,
                        "pages": [path for _, path in pages]}, indent=2) + "\n",
            encoding="utf-8",
        )
        print(f"GitBook {locale}: {folder}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("check", "export"))
    parser.add_argument("--output", type=Path, default=ROOT / "build" / "gitbook")
    args = parser.parse_args()
    config = load_config(str(ROOT / "mkdocs.yml"))
    if args.command == "check":
        check(config)
    else:
        export(config, args.output)


if __name__ == "__main__":
    main()
