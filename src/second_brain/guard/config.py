"""brain_guard configuration: one YAML file, read once when the plugin loads.

The file is the only thing an instance has to provide for the guard to start. Everything in it
is required to be explicit: a typo must stop the guard from loading, not silently widen a rule.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field

from second_brain.core.policy import PolicyConfig
from second_brain.core.types import Autonomy, Effect
from second_brain.guard.classify import ToolClass

CONFIG_ENV = "BRAIN_GUARD_CONFIG"
DEFAULT_SEARCH_TOOLS = ("web_search", "search_files", "session_search")


class GuardConfigError(RuntimeError):
    """The guard cannot start without a usable configuration."""


class McpToolPolicy(BaseModel):
    """Effect and default autonomy of one brain_tools tool, as the instance declares it."""

    model_config = ConfigDict(extra="forbid")

    effect: Effect
    autonomy: Autonomy


class GuardConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    vault_root: str
    guard_dir: str
    policy: PolicyConfig = Field(default_factory=PolicyConfig)
    private_prefixes: tuple[str, ...] = ()
    untrusted_prefixes: tuple[str, ...] = ()
    media_dir: str | None = None
    mcp_tools: dict[str, McpToolPolicy] = Field(default_factory=dict)
    admin_senders: tuple[str, ...] = ()
    search_tools: frozenset[str] = frozenset(DEFAULT_SEARCH_TOOLS)

    @classmethod
    def load(cls, environ: Mapping[str, str] | None = None) -> GuardConfig:
        env = os.environ if environ is None else environ
        path = env.get(CONFIG_ENV, "").strip()
        if not path:
            raise GuardConfigError(f"{CONFIG_ENV} is not set")
        return cls.from_yaml(path)

    @classmethod
    def from_yaml(cls, path: str | Path) -> GuardConfig:
        source = Path(path)
        try:
            raw = yaml.safe_load(source.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as exc:
            raise GuardConfigError(f"cannot read guard config {source}: {exc}") from exc
        if raw is None:
            raw = {}
        if not isinstance(raw, dict):
            raise GuardConfigError(f"guard config must be a mapping: {source}")
        try:
            return cls.model_validate(raw)
        except ValueError as exc:
            raise GuardConfigError(f"invalid guard config {source}: {exc}") from exc

    def tool_classes(self) -> dict[str, ToolClass]:
        """The brain_tools table `classify` looks tools up in, keyed by name without the prefix."""
        return {name: ToolClass(tool.effect, tool.autonomy) for name, tool in self.mcp_tools.items()}
