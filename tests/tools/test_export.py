"""Tests for the guard class export: the shape, the CLI, and the round trip into GuardConfig."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import BaseModel

from second_brain.core.types import Autonomy, Effect, ToolSpec
from second_brain.guard.classify import MCP_PREFIX, ToolClass, classify
from second_brain.guard.config import GuardConfig
from second_brain.tools import registry as registry_module
from second_brain.tools.config import ToolsConfig, build_registry
from second_brain.tools.export import export_guard_classes, main, render
from second_brain.tools.registry import Registry, Tool
from second_brain.tools.vault import Provenance


class FakeBackend:
    def list_events(self, calendar_id: str, date_from: Any, date_to: Any) -> list[Any]:
        return []

    def get_event(self, calendar_id: str, event_id: str) -> Any:
        raise NotImplementedError

    def create_event(self, calendar_id: str, draft: Any, *, send_updates: str = "none") -> Any:
        raise NotImplementedError

    def update_event(self, calendar_id: str, event_id: str, draft: Any, *, send_updates: str = "none") -> Any:
        raise NotImplementedError

    def delete_event(self, calendar_id: str, event_id: str, *, send_updates: str = "none") -> None:
        raise NotImplementedError

    def add_attendees(
        self, calendar_id: str, event_id: str, attendees: Any, *, send_updates: str = "none"
    ) -> Any:
        raise NotImplementedError


@pytest.fixture
def vault_root(tmp_path: Path) -> Path:
    root = tmp_path / "vault"
    root.mkdir()
    subprocess.run(["git", "-C", str(root), "init", "-q"], check=True)
    return root


@pytest.fixture
def config_path(vault_root: Path, tmp_path: Path) -> Path:
    (tmp_path / "archive").mkdir()
    (tmp_path / "media").mkdir()
    payload = {
        "vault": {"root": str(vault_root)},
        "token_file": str(tmp_path / "token"),
        "weather": {"enabled": True},
        "calendar": {
            "assistant_calendar_id": "assistant-calendar@example.test",
            "main_calendar_id": "main-calendar@example.test",
            "timezone": "Europe/Rome",
            "token_file": str(tmp_path / "calendar-token"),
        },
    }
    path = tmp_path / "tools.yaml"
    path.write_text(yaml.safe_dump(payload), encoding="utf-8")
    return path


@pytest.fixture
def registry(config_path: Path, monkeypatch: pytest.MonkeyPatch) -> Registry:
    monkeypatch.setattr(registry_module, "entry_points", lambda group: [])
    return build_registry(ToolsConfig.from_yaml(config_path), calendar_backend=FakeBackend())


def _guard_config(exported: dict[str, Any], tmp_path: Path) -> GuardConfig:
    hermes = tmp_path / "hermes" / "config.yaml"
    hermes.parent.mkdir(parents=True, exist_ok=True)
    hermes.write_text("approvals:\n  mode: manual\n", encoding="utf-8")
    return GuardConfig.model_validate(
        {
            "vault_root": str(tmp_path / "vault"),
            "guard_dir": str(tmp_path / "guard"),
            "hermes_config_path": str(hermes),
            "mcp_tools": exported,
        }
    )


def test_export_has_one_entry_per_tool(registry: Registry) -> None:
    exported = export_guard_classes(registry)
    assert set(exported) == {spec.name for spec in registry.specs()}


def test_export_entry_has_the_four_fields(registry: Registry) -> None:
    exported = export_guard_classes(registry)
    assert exported["vault_write"] == {
        "effect": "internal",
        "autonomy": "notify",
        "taints_untrusted": False,
        "taints_private": False,
    }


def test_export_carries_the_taints(registry: Registry) -> None:
    exported = export_guard_classes(registry)
    assert exported["vault_read"]["taints_private"] is True
    assert exported["vault_read"]["taints_untrusted"] is False
    assert exported["calendar_list"]["taints_untrusted"] is True
    assert exported["calendar_list"]["taints_private"] is True


def test_export_round_trips_into_guard_config(registry: Registry, tmp_path: Path) -> None:
    exported = export_guard_classes(registry)
    text = render(exported)
    loaded = yaml.safe_load(text)
    config = _guard_config(loaded, tmp_path)
    assert set(config.mcp_tools) == set(exported)
    assert config.tool_classes()["calendar_list"] == ToolClass(Effect.READ, Autonomy.AUTO, True, True)


def test_classify_uses_the_exported_taints(registry: Registry, tmp_path: Path) -> None:
    config = _guard_config(export_guard_classes(registry), tmp_path)
    classes = config.tool_classes()
    assert classify(MCP_PREFIX + "vault_read", classes).taints_private is True
    assert classify(MCP_PREFIX + "calendar_list", classes).taints_untrusted is True
    assert classify(MCP_PREFIX + "vault_write", classes).taints_private is False
    assert classify(MCP_PREFIX + "weather", classes) == ToolClass(Effect.READ, Autonomy.AUTO)


def test_classify_does_not_taint_a_tool_that_does_not_declare_it(registry: Registry, tmp_path: Path) -> None:
    config = _guard_config(export_guard_classes(registry), tmp_path)
    tool_class = classify(MCP_PREFIX + "vault_undo", config.tool_classes())
    assert (tool_class.taints_untrusted, tool_class.taints_private) == (False, False)


def test_guard_config_rejects_an_unknown_field_in_a_tool(registry: Registry, tmp_path: Path) -> None:
    exported = export_guard_classes(registry)
    exported["vault_read"]["taints_private"] = "yes-please"
    with pytest.raises(ValueError):
        _guard_config(exported, tmp_path)


def test_cli_writes_the_mapping(config_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(registry_module, "entry_points", lambda group: [])
    result = subprocess.run(
        ["uv", "run", "python", "-m", "second_brain.tools.export", str(config_path)],
        capture_output=True,
        text=True,
        check=True,
    )
    exported = yaml.safe_load(result.stdout)
    assert exported["vault_read"]["taints_private"] is True
    assert exported["weather"]["effect"] == "read"


def test_cli_without_an_argument_fails() -> None:
    assert main([]) == 2


def test_export_of_an_empty_registry_is_empty() -> None:
    assert export_guard_classes(Registry()) == {}
    assert yaml.safe_load(render({})) == {}


def test_export_keeps_the_tool_spec_values() -> None:
    class Params(BaseModel):
        path: str

    def handler(params: Any, provenance: Provenance) -> dict[str, Any]:
        return {"ok": True}

    spec = ToolSpec(
        name="ask_team",
        description="Ask the team.",
        params=Params,
        effect=Effect.EXTERNAL,
        autonomy=Autonomy.ASK,
        taints_untrusted=True,
    )
    registry = Registry()
    registry.add(Tool(spec=spec, handler=handler))
    assert export_guard_classes(registry) == {
        "ask_team": {
            "effect": "external",
            "autonomy": "ask",
            "taints_untrusted": True,
            "taints_private": False,
        }
    }
