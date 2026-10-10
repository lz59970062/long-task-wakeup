"""Native agent integrations and their public extension contract.

To integrate another child, implement ``ChildAgentAdapter`` and verify its
noninteractive result contract. Callback transports additionally implement
``AgentAdapter`` with verified session routing; child support alone never makes
an agent a callback target.
"""

from .base import AgentAdapter, ChildAgentAdapter, ChildOptions
from .claude import ClaudeAdapter
from .codex import CodexAdapter
from .pi import PiAdapter
from .registry import AgentRegistry

AGENTS = AgentRegistry((CodexAdapter(), ClaudeAdapter(), PiAdapter()))
AGENT_NAMES = AGENTS.names
CHILD_AGENT_NAMES = AGENTS.child_names


def get_agent(name: str) -> AgentAdapter:
    return AGENTS.get(name)


def get_child_agent(name: str) -> ChildAgentAdapter:
    return AGENTS.get_child(name)


__all__ = [
    "AGENTS", "AGENT_NAMES", "CHILD_AGENT_NAMES", "AgentAdapter", "ChildAgentAdapter",
    "AgentRegistry", "ChildOptions", "get_agent", "get_child_agent",
]
