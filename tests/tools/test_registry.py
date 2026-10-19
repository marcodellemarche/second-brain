"""Tests for the tool registry and the instance entry point loader."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import BaseModel

from second_brain.core.types import Autonomy, Effect, ToolSpec
from second_brain.tools import registry as registry_module
from second_brain.tools.registry import Registry, Tool
from second_brain.tools.vault import Provenance


class _Params(BaseModel):
    path: str


def _tool(name: str) -> Tool:
    def handler(params: BaseModel, provenance: Provenance) -> dict[str, Any]:
        return {"ok": True}

    return Tool(
        spec=ToolSpec(
            name=name,
            description=f"{name} for the tests",
            params=_Params,
            effect=Effect.READ,
            autonomy=Autonomy.AUTO,
        ),
        handler=handler,
    )


class _EntryPoint:
    def __init__(self, name: str, load: Any) -> None:
        self.name = name
        self._load = load

    def load(self) -> Any:
        return self._load


def test_add_and_get() -> None:
    registry = Registry()
    registry.add(_tool("vault_read"))
    assert registry.get("vault_read").spec.name == "vault_read"
    assert [spec.name for spec in registry.specs()] == ["vault_read"]


def test_duplicate_names_are_rejected() -> None:
    registry = Registry()
    registry.add(_tool("vault_read"))
    with pytest.raises(ValueError, match="duplicate tool name"):
        registry.add(_tool("vault_read"))


def test_unknown_tool_raises() -> None:
    with pytest.raises(KeyError):
        Registry().get("nope")


def test_entry_point_loads_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    points = [_EntryPoint("instance", lambda: [_tool("weather")])]
    monkeypatch.setattr(registry_module, "entry_points", lambda group: points)
    registry = Registry()
    registry.load_entry_points()
    assert [spec.name for spec in registry.specs()] == ["weather"]


def test_failing_entry_point_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken() -> list[Tool]:
        raise RuntimeError("instance package is broken")

    points = [_EntryPoint("broken", broken)]
    monkeypatch.setattr(registry_module, "entry_points", lambda group: points)
    registry = Registry()
    with pytest.raises(RuntimeError, match="instance package is broken"):
        registry.load_entry_points()
