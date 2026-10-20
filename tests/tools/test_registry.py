"""Tests for the tool registry and the instance entry point loader."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel

from second_brain.core.types import Autonomy, Effect, ToolSpec
from second_brain.tools import registry as registry_module
from second_brain.tools.registry import ExtensionError, ExtensionSettings, Registry, Tool
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


# --- extension settings --------------------------------------------------------------------


def _extension_tool(name: str, credentials: tuple[str, ...] = ()) -> Tool:
    def handler(params: BaseModel, provenance: Provenance) -> dict[str, Any]:
        return {"ok": True}

    return Tool(
        spec=ToolSpec(
            name=name,
            description=f"{name} for the tests",
            params=_Params,
            effect=Effect.READ,
            autonomy=Autonomy.AUTO,
            credentials=credentials,
        ),
        handler=handler,
    )


def _settings(name: str = "instance", secrets_dir: Path = Path("/run/secrets")) -> ExtensionSettings:
    return ExtensionSettings(
        name=name, config={"base_url": "https://api.example.test"}, secrets_dir=secrets_dir
    )


def test_a_one_parameter_entry_point_receives_its_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[ExtensionSettings] = []

    def factory(settings: ExtensionSettings) -> list[Tool]:
        seen.append(settings)
        return [_extension_tool("parcel_status")]

    monkeypatch.setattr(registry_module, "entry_points", lambda group: [_EntryPoint("instance", factory)])
    registry = Registry()
    registry.load_entry_points(settings_for=lambda name: _settings(name, Path("/tmp/secrets")))
    assert [spec.name for spec in registry.specs()] == ["parcel_status"]
    assert seen == [
        ExtensionSettings("instance", {"base_url": "https://api.example.test"}, Path("/tmp/secrets"))
    ]


def test_a_zero_parameter_entry_point_still_works(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[int] = []

    def factory() -> list[Tool]:
        calls.append(1)
        return [_extension_tool("weather")]

    monkeypatch.setattr(registry_module, "entry_points", lambda group: [_EntryPoint("instance", factory)])
    registry = Registry()
    registry.load_entry_points(settings_for=lambda name: _settings(name))
    assert [spec.name for spec in registry.specs()] == ["weather"]
    assert calls == [1]


def test_a_non_callable_entry_point_is_loaded_as_it_is(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        registry_module, "entry_points", lambda group: [_EntryPoint("instance", [_extension_tool("weather")])]
    )
    registry = Registry()
    registry.load_entry_points(settings_for=lambda name: _settings(name))
    assert [spec.name for spec in registry.specs()] == ["weather"]


def test_a_one_parameter_entry_point_without_settings_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    def factory(settings: ExtensionSettings) -> list[Tool]:
        return [_extension_tool("parcel_status")]

    monkeypatch.setattr(registry_module, "entry_points", lambda group: [_EntryPoint("instance", factory)])
    with pytest.raises(TypeError):
        Registry().load_entry_points()


def test_secret_reads_a_file(tmp_path: Path) -> None:
    (tmp_path / "parcel_api_key").write_text("  test-key\n", encoding="utf-8")
    settings = ExtensionSettings(name="instance", config={}, secrets_dir=tmp_path)
    assert settings.secret("parcel_api_key") == "test-key"


def test_secret_is_read_on_every_call(tmp_path: Path) -> None:
    path = tmp_path / "parcel_api_key"
    path.write_text("first", encoding="utf-8")
    settings = ExtensionSettings(name="instance", config={}, secrets_dir=tmp_path)
    assert settings.secret("parcel_api_key") == "first"
    path.write_text("second", encoding="utf-8")
    assert settings.secret("parcel_api_key") == "second"


def test_secret_refuses_a_path(tmp_path: Path) -> None:
    (tmp_path.parent / "outside").write_text("leaked", encoding="utf-8")
    settings = ExtensionSettings(name="instance", config={}, secrets_dir=tmp_path)
    with pytest.raises(ExtensionError, match="not a secret name"):
        settings.secret("../outside")


def test_a_missing_secret_raises(tmp_path: Path) -> None:
    settings = ExtensionSettings(name="instance", config={}, secrets_dir=tmp_path)
    with pytest.raises(ExtensionError, match="secret parcel_api_key is missing or empty"):
        settings.secret("parcel_api_key")


def test_an_empty_secret_raises(tmp_path: Path) -> None:
    (tmp_path / "parcel_api_key").write_text("  \n", encoding="utf-8")
    settings = ExtensionSettings(name="instance", config={}, secrets_dir=tmp_path)
    with pytest.raises(ExtensionError, match="secret parcel_api_key is missing or empty"):
        settings.secret("parcel_api_key")
