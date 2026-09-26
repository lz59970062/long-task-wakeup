"""An explicit source registry, with no dynamic imports or shell templates."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from .base import AgentAdapter


class AgentRegistry:
    """Resolve adapter metadata and commands without owning task lifecycle.

    Adding an adapter is a reviewed source change. Automatic environment
    detection is deliberately separate from resolving a persisted request:
    stored agent names must never fall back to a different agent.
    """

    def __init__(self, adapters: Sequence[AgentAdapter], *, default: str = "codex") -> None:
        self._adapters = {adapter.name: adapter for adapter in adapters}
        if len(self._adapters) != len(adapters):
            raise ValueError("duplicate agent adapter name")
        if default not in self._adapters:
            raise ValueError(f"default agent {default!r} is not registered")
        self.default = default

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(self._adapters)

    def get(self, name: str) -> AgentAdapter:
        try:
            return self._adapters[name]
        except KeyError:
            raise ValueError(f"unsupported agent: {name!r}") from None

    def detect(self, environment: Mapping[str, str]) -> str:
        candidates = sorted(self._adapters.values(), key=lambda item: item.detection_priority, reverse=True)
        return next((adapter.name for adapter in candidates if adapter.matches_environment(environment)), self.default)

    def child_environment(self, environment: Mapping[str, str]) -> dict[str, str]:
        child = dict(environment)
        for adapter in self._adapters.values():
            for name in adapter.parent_env_names:
                child.pop(name, None)
        return child
