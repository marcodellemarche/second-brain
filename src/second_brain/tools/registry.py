"""The tool registry: core tools and instance tools behind one lookup.

A tool is its spec plus a handler. The spec is what brain_guard decides on, the handler is what
runs once the call is allowed. Instance repos add tools through the `second_brain.tools` entry
point group, so a brain can grow tools without touching this repo.
"""

from __future__ import annotations

import inspect
import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from importlib.metadata import entry_points
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from second_brain.core.types import ToolSpec
from second_brain.tools.vault import Provenance

log = logging.getLogger(__name__)

ENTRY_POINT_GROUP = "second_brain.tools"

Handler = Callable[[BaseModel, Provenance], dict]


class ExtensionError(RuntimeError):
    """An instance tool cannot be built or run with the settings and secrets it has."""


@dataclass(frozen=True, slots=True)
class ExtensionSettings:
    """What one instance package gets: its name, its config section, the secrets and the timezone.

    The section is what the instance repo wrote under `extensions: <name>` in the tools config.
    Secrets are never in there: a tool names them in its spec and reads them by file, so a
    rotation only needs the file rewritten, not a restart.
    """

    name: str
    config: Mapping[str, Any] = field(default_factory=dict)
    secrets_dir: Path = Path("/run/secrets")
    timezone: str = "UTC"

    def secret(self, name: str) -> str:
        """Read one secret from `<secrets_dir>/<name>`, on every call, so rotation needs no restart."""
        if not name or "/" in name or "\\" in name or ".." in name:
            raise ExtensionError(f"not a secret name: {name!r}")
        path = self.secrets_dir / name
        try:
            value = path.read_text(encoding="utf-8").strip()
        except OSError:
            raise ExtensionError(f"secret {name} is missing or empty") from None
        if not value:
            raise ExtensionError(f"secret {name} is missing or empty")
        return value


@dataclass(frozen=True, slots=True)
class Tool:
    spec: ToolSpec
    handler: Handler


class Registry:
    """Every tool the server exposes, keyed by name."""

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}
        self._instance: dict[str, ExtensionSettings] = {}

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

    def instance_tools(self) -> dict[str, ExtensionSettings]:
        """The tools that came from an entry point, each with the settings it was built with."""
        return dict(self._instance)

    def load_entry_points(
        self,
        group: str = ENTRY_POINT_GROUP,
        settings_for: Callable[[str], ExtensionSettings] | None = None,
    ) -> None:
        """Register the tools of every installed package that provides the group.

        Each entry point returns a list of tools, or a callable that returns one. A callable that
        takes a positional parameter gets that package's `ExtensionSettings`, so an instance tool
        can read its own options and secrets; a zero-argument one is called as it is. A failure is
        logged and re-raised: a brain with a broken instance tool must not start with that tool
        silently missing.
        """
        for entry_point in entry_points(group=group):
            try:
                loaded = entry_point.load()
                settings = (
                    settings_for(entry_point.name)
                    if settings_for is not None
                    else ExtensionSettings(entry_point.name)
                )
                if not callable(loaded):
                    tools = loaded
                elif settings_for is not None and _takes_a_parameter(loaded):
                    tools = loaded(settings)
                else:
                    tools = loaded()
                for tool in tools:
                    self.add(tool)
                    self._instance[tool.spec.name] = settings
            except Exception:
                log.exception("loading instance tools from entry point %r failed", entry_point.name)
                raise


def _takes_a_parameter(factory: Callable[..., Any]) -> bool:
    """Whether the factory declares one positional parameter it can be called with."""
    try:
        parameters = list(inspect.signature(factory).parameters.values())
    except (TypeError, ValueError):
        return False
    positional = [
        parameter
        for parameter in parameters
        if parameter.kind in (parameter.POSITIONAL_ONLY, parameter.POSITIONAL_OR_KEYWORD)
    ]
    required = [parameter for parameter in positional if parameter.default is parameter.empty]
    return bool(positional) and len(required) <= 1
