# Contributing to the docs

Documentation and code live in the same repository. Edit the relevant Markdown file and build the site to preview your changes.

## Preview locally

Run from the repository root:

```bash
python3 -m venv .venv-docs
source .venv-docs/bin/activate
python -m pip install -r docs/requirements.txt
mkdocs serve
```

Open the local address printed in the terminal. The page refreshes when you edit a file.

## Add a tutorial

| Directory | Content |
| --- | --- |
| `docs/guides/` | Steps for completing a specific task |
| `docs/integrations/` | Agent setup and usage |
| `docs/reference/` | Options, configuration, and engineering references |

Use `topic.md` for Chinese and `topic.en.md` in the same directory for English. Add the base filename to `nav` in `mkdocs.yml`, then add its English title to `nav_translations`. Use the base filename in links in both languages, such as `../installation.md`. The build resolves each link to the matching language.

Link to the new page from related tutorials. Chinese keeps the site's root paths; English uses `/en/`. The language menu at the top opens the other-language version of the current page.

A tutorial starts with what the reader wants to do, gives runnable commands, and explains the expected output and next step. Express parameter requirements as concrete setup instructions and give repair steps for common problems.

## Check and publish

Before submitting, run:

```bash
python scripts/docs_sync.py check
mkdocs build --strict
python scripts/docs_sync.py export
```

The build checks configuration and documentation links. Changes merged into `main` are built and published to GitHub Pages by GitHub Actions.

The coverage check requires Chinese and English versions of every page in the navigation. The export command generates GitBook Markdown, tables of contents, and configuration for both languages. See [Publishing and synchronization](publishing.md) for initial platform setup and automatic updates.
