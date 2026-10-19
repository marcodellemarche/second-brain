"""Instance policy: per-tool autonomy overrides and run budgets."""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field

from second_brain.core.types import Autonomy, ToolSpec, stricter


class ToolPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    autonomy: Autonomy | None = None


class PolicyConfig(BaseModel):
    # A typo in an instance policy must fail loudly, not be silently ignored.
    model_config = ConfigDict(extra="forbid")

    tools: dict[str, ToolPolicy] = Field(default_factory=dict)
    max_steps: int = 25
    max_seconds: float = 600
    max_repeat: int = 2
    max_empty_searches: int = 3
    proactive_per_day: int = 6

    @classmethod
    def from_yaml(cls, path: str | Path) -> PolicyConfig:
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        return cls.model_validate({} if raw is None else raw)


def effective_autonomy(spec: ToolSpec, policy: PolicyConfig) -> Autonomy:
    """Policy may only tighten the spec default, and `never` can never be loosened."""
    if spec.autonomy is Autonomy.NEVER:
        return Autonomy.NEVER
    tool_policy = policy.tools.get(spec.name)
    if tool_policy is None or tool_policy.autonomy is None:
        return spec.autonomy
    return stricter(spec.autonomy, tool_policy.autonomy)
