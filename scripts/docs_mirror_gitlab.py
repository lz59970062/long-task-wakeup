"""Push the documentation source commit to a configured GitLab repository."""

import os
from pathlib import Path
import subprocess
import tempfile
from urllib.parse import urlsplit


def main():
    repository = os.environ["DOCS_GITLAB_REPOSITORY"]
    parsed = urlsplit(repository)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username
            or parsed.password or parsed.query or parsed.fragment):
        raise SystemExit("DOCS_GITLAB_REPOSITORY must be an HTTPS repository URL without credentials")
    if not os.environ.get("DOCS_GITLAB_TOKEN"):
        raise SystemExit("Configure the DOCS_GITLAB_TOKEN Actions secret with write_repository access")
    with tempfile.TemporaryDirectory(prefix="ltc-docs-gitlab-") as directory:
        askpass = Path(directory) / "askpass"
        askpass.write_text(
            '#!/bin/sh\ncase "$1" in\n'
            '  *Username*) printf "%s\\n" oauth2 ;;\n'
            '  *) printf "%s\\n" "$DOCS_GITLAB_TOKEN" ;;\nesac\n',
            encoding="utf-8",
        )
        askpass.chmod(0o700)
        environment = dict(os.environ, GIT_ASKPASS=str(askpass), GIT_TERMINAL_PROMPT="0")
        subprocess.run(
            ["git", "-c", "credential.helper=", "push", repository, "HEAD:refs/heads/main"],
            env=environment, check=True,
        )


if __name__ == "__main__":
    main()
