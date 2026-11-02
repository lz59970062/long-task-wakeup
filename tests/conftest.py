"""Test isolation from the agent environment that launched the test runner."""
from __future__ import annotations

import pytest

PARENT_MARKERS = (
    "DSH_SESSION_ID", "DSH_SHELL", "DSH_WEB_URL", "DSH_PROFILE", "DSH_HOME",
    "CLAUDE_CODE_SESSION_ID", "CLAUDECODE", "PI_SESSION_ID", "PI_SESSION_FILE",
    "PI_CODING_AGENT", "CODEX_THREAD_ID",
)


@pytest.fixture(autouse=True)
def isolate_parent_agent_markers(monkeypatch):
    for name in PARENT_MARKERS:
        monkeypatch.delenv(name, raising=False)
