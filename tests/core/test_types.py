import pytest
from pydantic import BaseModel, ValidationError

from second_brain.core.types import Autonomy, Effect, ToolSpec, stricter


class EmptyParams(BaseModel):
    pass


def make_spec(**overrides: object) -> ToolSpec:
    fields: dict[str, object] = {
        "name": "weather",
        "description": "Forecast for a place.",
        "params": EmptyParams,
        "effect": Effect.READ,
        "autonomy": Autonomy.AUTO,
    }
    fields.update(overrides)
    return ToolSpec(**fields)


@pytest.mark.parametrize("a", list(Autonomy))
@pytest.mark.parametrize("b", list(Autonomy))
def test_stricter_returns_the_more_restrictive(a: Autonomy, b: Autonomy) -> None:
    order = [Autonomy.AUTO, Autonomy.NOTIFY, Autonomy.ASK, Autonomy.NEVER]
    expected = order[max(order.index(a), order.index(b))]
    assert stricter(a, b) is expected
    assert stricter(b, a) is expected


def test_effect_and_autonomy_values() -> None:
    assert [e.value for e in Effect] == ["read", "internal", "visible", "external"]
    assert [a.value for a in Autonomy] == ["auto", "notify", "ask", "never"]


def test_tool_spec_is_frozen() -> None:
    spec = make_spec()
    with pytest.raises(ValidationError):
        spec.autonomy = Autonomy.NEVER


def test_tool_spec_defaults_are_empty_tuples() -> None:
    spec = make_spec()
    assert spec.hosts == ()
    assert spec.credentials == ()


def test_tool_spec_taints_default_to_false() -> None:
    spec = make_spec()
    assert spec.taints_untrusted is False
    assert spec.taints_private is False


def test_tool_spec_accepts_taints() -> None:
    spec = make_spec(taints_untrusted=True, taints_private=True)
    assert spec.taints_untrusted is True
    assert spec.taints_private is True


@pytest.mark.parametrize("name", ["weather", "vault_write", "a1"])
def test_tool_spec_accepts_valid_names(name: str) -> None:
    assert make_spec(name=name).name == name


@pytest.mark.parametrize("name", ["", "Weather", "1weather", "_weather", "vault-write", "vault write"])
def test_tool_spec_rejects_invalid_names(name: str) -> None:
    with pytest.raises(ValidationError):
        make_spec(name=name)
