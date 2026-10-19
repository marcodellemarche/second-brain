"""The tool registry: core tools and instance tools behind one lookup.

A tool is its spec plus a handler. The spec is what brain_guard decides on, the handler is what
runs once the call is allowed. Instance repos add tools through the `second_brain.tools` entry
point group, so a brain can grow tools without touching this repo.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from importlib.metadata import entry_points

from pydantic import BaseModel

from second_brain.core.types import ToolSpec
from second_brain.tools.vault import Provenance

log = logging.getLogger(__name__)

ENTRY_POINT_GROUP = "second_brain.tools"

Handler = Callable[[BaseModel, Provenance], dict]


@dataclass(frozen=True, slots=True)
class Tool:
    spec: ToolSpec
    handler: Handler


class Registry:
    """Every tool the server exposes, keyed by name."""

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def add(self, tool: Tool) -> None:
        """Register a tool. Two tools with one name would shadow each other, so it raises."""
        name = tool.spec.name
        if name in self._tools:
            raise ValueError(f"duplicate tool name: {name}")
        self._tools[name] = tool

    def get(self, name: str) -> Tool:
        try:
            return self._tools[name]
        except KeyError:
            raise KeyError(f"unknown tool: {name}") from None

    def specs(self) -> list[ToolSpec]:
        return [tool.spec for tool in self._tools.values()]

    def load_entry_points(self, group: str = ENTRY_POINT_GROUP) -> None:
        """Register the tools of every installed package that provides the group.

        Each entry point returns a list of tools. A failure is logged and re-raised: a brain
        with a broken instance tool must not start with that tool silently missing.
        """
        for entry_point in entry_points(group=group):
            try:
                loaded = entry_point.load()
                tools = loaded() if callable(loaded) else loaded
                for tool in tools:
                    self.add(tool)
            except Exception:
                log.exception("loading instance tools from entry point %r failed", entry_point.name)
                raise
