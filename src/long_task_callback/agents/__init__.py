"""Native agent integrations and their public extension contract.

To integrate another agent, implement ``AgentAdapter``, register an instance
here, and verify its session routing and child-result contract. PI and DSH are
not registered: their native integration still requires platform validation.
"""

from .base import AgentAdapter, ChildOptions
from .claude import ClaudeAdapter
from .codex import CodexAdapter
from .registry import AgentRegistry

AGENTS = AgentRegistry((CodexAdapter(), ClaudeAdapter()))
AGENT_NAMES = AGENTS.names


def get_agent(name: str) -> AgentAdapter:
    return AGENTS.get(name)


__all__ = ["AGENTS", "AGENT_NAMES", "AgentAdapter", "AgentRegistry", "ChildOptions", "get_agent"]
