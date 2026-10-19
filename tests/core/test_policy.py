from pathlib import Path

import pytest
from pydantic import BaseModel, ValidationError

from second_brain.core.policy import PolicyConfig, ToolPolicy, effective_autonomy
from second_brain.core.types import Autonomy, Effect, ToolSpec


class EmptyParams(BaseModel):
    pass


def make_spec(autonomy: Autonomy) -> ToolSpec:
    return ToolSpec(
        name="weather",
        description="Forecast for a place.",
        params=EmptyParams,
        effect=Effect.READ,
        autonomy=autonomy,
    )


def policy_for(autonomy: Autonomy | None, name: str = "weather") -> PolicyConfig:
    return PolicyConfig(tools={name: ToolPolicy(autonomy=autonomy)})


def test_defaults() -> None:
    config = PolicyConfig()
    assert config.tools == {}
    assert config.max_steps == 25
    assert config.max_seconds == 600
    assert config.max_repeat == 2
    assert config.max_empty_searches == 3
    assert config.proactive_per_day == 6


def test_policy_without_entry_keeps_spec_default() -> None:
    assert effective_autonomy(make_spec(Autonomy.ASK), PolicyConfig()) is Autonomy.ASK


def test_policy_cannot_loosen() -> None:
    assert effective_autonomy(make_spec(Autonomy.ASK), policy_for(Autonomy.AUTO)) is Autonomy.ASK


def test_policy_can_tighten() -> None:
    assert effective_autonomy(make_spec(Autonomy.AUTO), policy_for(Autonomy.ASK)) is Autonomy.ASK


def test_policy_without_autonomy_keeps_spec_default() -> None:
    assert effective_autonomy(make_spec(Autonomy.NOTIFY), policy_for(None)) is Autonomy.NOTIFY


def test_policy_for_another_tool_does_not_apply() -> None:
    policy = policy_for(Autonomy.NEVER, name="vault_write")
    assert effective_autonomy(make_spec(Autonomy.AUTO), policy) is Autonomy.AUTO


def test_never_stays_never() -> None:
    assert effective_autonomy(make_spec(Autonomy.NEVER), PolicyConfig()) is Autonomy.NEVER
    for level in Autonomy:
        assert effective_autonomy(make_spec(Autonomy.NEVER), policy_for(level)) is Autonomy.NEVER


def test_from_yaml(tmp_path: Path) -> None:
    path = tmp_path / "policy.yaml"
    path.write_text(
        """
tools:
  weather:
    autonomy: ask
  vault_delete:
    autonomy: never
max_steps: 10
max_seconds: 30.5
proactive_per_day: 2
""",
        encoding="utf-8",
    )
    config = PolicyConfig.from_yaml(path)
    assert config.tools["weather"].autonomy is Autonomy.ASK
    assert config.tools["vault_delete"].autonomy is Autonomy.NEVER
    assert config.max_steps == 10
    assert config.max_seconds == 30.5
    assert config.max_repeat == 2
    assert config.max_empty_searches == 3
    assert config.proactive_per_day == 2


def test_from_yaml_empty_file(tmp_path: Path) -> None:
    path = tmp_path / "policy.yaml"
    path.write_text("", encoding="utf-8")
    assert PolicyConfig.from_yaml(path) == PolicyConfig()


def test_from_yaml_rejects_unknown_autonomy(tmp_path: Path) -> None:
    path = tmp_path / "policy.yaml"
    path.write_text("tools:\n  weather:\n    autonomy: sometimes\n", encoding="utf-8")
    with pytest.raises(ValueError, match="sometimes"):
        PolicyConfig.from_yaml(path)


def test_unknown_keys_are_rejected(tmp_path):
    path = tmp_path / "policy.yaml"
    path.write_text("max_stpes: 10\n", encoding="utf-8")
    with pytest.raises(ValidationError):
        PolicyConfig.from_yaml(path)
