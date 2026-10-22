"""brain_guard configuration: one YAML file, read once when the plugin loads.

The file is the only thing an instance has to provide for the guard to start. Everything in it
is required to be explicit: a typo must stop the guard from loading, not silently widen a rule.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

from second_brain.core.policy import PolicyConfig
from second_brain.core.types import Autonomy, Effect
from second_brain.guard.classify import ToolClass

CONFIG_ENV = "BRAIN_GUARD_CONFIG"
DEFAULT_SEARCH_TOOLS = ("web_search", "search_files", "session_search")
APPROVAL_MODES = ("manual", "smart", "off")
REQUIRED_APPROVAL_MODE = "manual"


class GuardConfigError(RuntimeError):
    """The guard cannot start without a usable configuration."""


class McpToolPolicy(BaseModel):
    """Effect, default autonomy and taints of one brain_tools tool, as the instance declares it."""

    model_config = ConfigDict(extra="forbid")

    effect: Effect
    autonomy: Autonomy
    taints_untrusted: bool = False
    taints_private: bool = False
    approval: str = ""


class GuardConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    vault_root: str
    guard_dir: str
    # Hermes' own config.yaml. The guard refuses to start unless it says `approvals.mode: manual`.
    hermes_config_path: str
    policy: PolicyConfig = Field(default_factory=PolicyConfig)
    private_prefixes: tuple[str, ...] = ()
    untrusted_prefixes: tuple[str, ...] = ()
    # Which vault this guard watches. A team brain is the destination of its own team calls, so the
    # call router's team_domains mean something there and nothing in a personal brain.
    shape: Literal["personal", "team"] = "personal"
    media_dir: str | None = None
    mcp_tools: dict[str, McpToolPolicy] = Field(default_factory=dict)
    admin_senders: tuple[str, ...] = ()
    search_tools: frozenset[str] = frozenset(DEFAULT_SEARCH_TOOLS)
    # The language the owner reads, so the approval card is written in it and not in policy codes.
    language: Literal["it", "en"] = "en"

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
        return {
            name: ToolClass(
                tool.effect, tool.autonomy, tool.taints_untrusted, tool.taints_private, tool.approval
            )
            for name, tool in self.mcp_tools.items()
        }

    def check_approvals(self) -> None:
        """Raise unless Hermes asks a human about every dangerous command.

        The guard stamps `approval: human:<sender>` when it escalates a call, and that is only
        true while a person really answers the prompt. Under `smart` an auxiliary model can
        approve it, under `off` nothing is asked at all.
        """
        path = Path(self.hermes_config_path)
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as exc:
            raise GuardConfigError(f"cannot read Hermes config {path}: {exc}") from exc
        approvals = raw.get("approvals") if isinstance(raw, dict) else None
        mode = approvals.get("mode") if isinstance(approvals, dict) else None
        if _approval_mode(mode) != REQUIRED_APPROVAL_MODE:
            raise GuardConfigError(
                f"Hermes config {path} must set approvals.mode: {REQUIRED_APPROVAL_MODE} (found {mode!r})"
            )


def _approval_mode(value: object) -> str:
    """Hermes' own normalization: YAML reads a bare `off` as False, and anything unknown is smart."""
    if isinstance(value, bool):
        return "off" if value is False else REQUIRED_APPROVAL_MODE
    if isinstance(value, str):
        mode = value.strip().lower()
        return mode if mode in APPROVAL_MODES else "smart"
    return "smart"
