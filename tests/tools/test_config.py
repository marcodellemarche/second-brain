"""Tests for the brain_tools config: what it accepts, what it refuses, which tools it enables."""

from __future__ import annotations

import copy
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import BaseModel

from second_brain.tools import registry as registry_module
from second_brain.tools.config import (
    CONFIG_ENV,
    ToolsConfig,
    ToolsConfigError,
    build_registry,
)
from second_brain.tools.registry import Tool
from second_brain.tools.vault import Provenance

CALENDAR = {
    "assistant_calendar_id": "assistant-calendar@example.test",
    "main_calendar_id": "main-calendar@example.test",
    "timezone": "Europe/Rome",
    "token_file": "/run/secrets/calendar-token",
}
LINKS = {
    "base_url": "https://cloud.example.test",
    "vault_folder": "vault",
    "archive_folder": "archive",
}
JOB_KINDS = {
    "research": {
        "route": "background-research",
        "description": "Research a question on the web.",
        "web": True,
    },
    "develop_idea": {
        "route": "background-idea",
        "description": "Develop an idea into a note.",
        "web": False,
    },
}


def _jobs(tmp_path: Path, kinds: dict[str, Any] | None = None) -> dict[str, Any]:
    """A jobs section whose queue file lives under tmp_path, so the registry can create it."""
    return {
        "store_path": str(tmp_path / "state" / "jobs.sqlite3"),
        "kinds": copy.deepcopy(JOB_KINDS if kinds is None else kinds),
    }


class FakeBackend:
    """Enough of a calendar backend for the tools to be built; no test here calls it."""

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


def _minimal(vault_root: Path, tmp_path: Path) -> dict[str, Any]:
    return {
        "vault": {"root": str(vault_root)},
        "token_file": str(tmp_path / "token"),
        "listen": "127.0.0.1:8765",
    }


def _full(vault_root: Path, tmp_path: Path) -> dict[str, Any]:
    (tmp_path / "archive").mkdir(exist_ok=True)
    (tmp_path / "media").mkdir(exist_ok=True)
    return {
        **_minimal(vault_root, tmp_path),
        "calendar": copy.deepcopy(CALENDAR),
        "weather": {"enabled": True},
        "links": copy.deepcopy(LINKS),
        "archive": {
            "root": str(tmp_path / "archive"),
            "inbox_media_dir": str(tmp_path / "media"),
        },
        "jobs": _jobs(tmp_path),
    }


def _write(tmp_path: Path, payload: dict[str, Any]) -> Path:
    path = tmp_path / "tools.yaml"
    path.write_text(yaml.safe_dump(payload), encoding="utf-8")
    return path


def _no_entry_points(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(registry_module, "entry_points", lambda group: [])


def _names(config: ToolsConfig, monkeypatch: pytest.MonkeyPatch) -> set[str]:
    _no_entry_points(monkeypatch)
    registry = build_registry(config, calendar_backend=FakeBackend())
    return {spec.name for spec in registry.specs()}


# --- loading -------------------------------------------------------------------------------


def test_minimal_config_loads(vault_root: Path, tmp_path: Path) -> None:
    config = ToolsConfig.from_yaml(_write(tmp_path, _minimal(vault_root, tmp_path)))
    assert config.vault.root == vault_root
    assert config.calendar is None
    assert config.weather is None
    assert config.links is None
    assert config.archive is None
    assert config.jobs is None


def test_listen_defaults_to_loopback(vault_root: Path, tmp_path: Path) -> None:
    payload = _minimal(vault_root, tmp_path)
    payload.pop("listen")
    config = ToolsConfig.from_yaml(_write(tmp_path, payload))
    assert (config.listen.host, config.listen.port) == ("127.0.0.1", 8765)


def test_full_config_loads(vault_root: Path, tmp_path: Path) -> None:
    config = ToolsConfig.from_yaml(_write(tmp_path, _full(vault_root, tmp_path)))
    assert config.calendar is not None
    assert config.calendar.token_file == Path("/run/secrets/calendar-token")
    assert config.weather is not None and config.weather.enabled is True
    assert config.links is not None
    assert config.archive is not None
    assert config.jobs is not None
    assert config.jobs.store_path == tmp_path / "state" / "jobs.sqlite3"
    assert set(config.jobs.kinds) == {"research", "develop_idea"}
    assert config.jobs.kinds["research"].web is True
    assert config.jobs.kinds["develop_idea"].web is False


def test_unknown_key_is_rejected(vault_root: Path, tmp_path: Path) -> None:
    payload = _minimal(vault_root, tmp_path)
    payload["telepathy"] = {"enabled": True}
    with pytest.raises(ToolsConfigError, match="invalid tools config"):
        ToolsConfig.from_yaml(_write(tmp_path, payload))


def test_unknown_key_in_a_section_is_rejected(vault_root: Path, tmp_path: Path) -> None:
    payload = _full(vault_root, tmp_path)
    payload["links"]["password"] = "hunter2"
    with pytest.raises(ToolsConfigError, match="secrets are never in this file"):
        ToolsConfig.from_yaml(_write(tmp_path, payload))


@pytest.mark.parametrize("name", ["token", "secret", "password", "api_key"])
def test_inline_secret_is_rejected(vault_root: Path, tmp_path: Path, name: str) -> None:
    payload = _full(vault_root, tmp_path)
    payload["calendar"][name] = "not-in-a-file"
    with pytest.raises(ToolsConfigError, match="secrets are never in this file"):
        ToolsConfig.from_yaml(_write(tmp_path, payload))


def test_token_file_path_is_accepted(vault_root: Path, tmp_path: Path) -> None:
    payload = _full(vault_root, tmp_path)
    payload["calendar"]["token_file"] = "/run/secrets/token"
    config = ToolsConfig.from_yaml(_write(tmp_path, payload))
    assert config.calendar is not None
    assert config.calendar.token_file == Path("/run/secrets/token")


def test_missing_vault_is_rejected(tmp_path: Path) -> None:
    payload = _minimal(tmp_path / "vault", tmp_path)
    payload.pop("vault")
    with pytest.raises(ToolsConfigError, match="invalid tools config"):
        ToolsConfig.from_yaml(_write(tmp_path, payload))


def test_config_path_comes_from_the_environment(vault_root: Path, tmp_path: Path) -> None:
    path = _write(tmp_path, _minimal(vault_root, tmp_path))
    config = ToolsConfig.load({CONFIG_ENV: str(path)})
    assert config.vault.root == vault_root


def test_missing_config_env_is_an_error() -> None:
    with pytest.raises(ToolsConfigError, match=f"{CONFIG_ENV} is not set"):
        ToolsConfig.load({})


def test_unreadable_file_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(ToolsConfigError, match="cannot read tools config"):
        ToolsConfig.from_yaml(tmp_path / "nope.yaml")


def test_token_is_read_from_its_file(vault_root: Path, tmp_path: Path) -> None:
    token_file = tmp_path / "token"
    token_file.write_text("  test-token  \n", encoding="utf-8")
    config = ToolsConfig.from_yaml(_write(tmp_path, _minimal(vault_root, tmp_path)))
    assert config.read_token() == "test-token"


def test_empty_token_file_is_an_error(vault_root: Path, tmp_path: Path) -> None:
    (tmp_path / "token").write_text("\n", encoding="utf-8")
    config = ToolsConfig.from_yaml(_write(tmp_path, _minimal(vault_root, tmp_path)))
    with pytest.raises(ToolsConfigError, match="is empty"):
        config.read_token()


# --- registry ------------------------------------------------------------------------------


def test_minimal_registry_has_only_the_vault_tools(
    vault_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = ToolsConfig.from_yaml(_write(tmp_path, _minimal(vault_root, tmp_path)))
    assert _names(config, monkeypatch) == {
        "vault_read",
        "vault_log",
        "vault_write",
        "vault_move",
        "vault_delete",
        "vault_undo",
    }


def test_full_registry_has_every_group(
    vault_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = ToolsConfig.from_yaml(_write(tmp_path, _full(vault_root, tmp_path)))
    assert _names(config, monkeypatch) == {
        "vault_read",
        "vault_log",
        "vault_write",
        "vault_move",
        "vault_delete",
        "vault_undo",
        "calendar_list",
        "calendar_create",
        "calendar_update",
        "calendar_delete",
        "calendar_main_write",
        "calendar_invite",
        "weather",
        "nextcloud_link",
        "archive_categories",
        "archive_store",
        "archive_discard",
        "research_enqueue",
        "job_enqueue",
    }


def test_a_jobs_section_registers_one_tool_per_effect_class(
    vault_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = _minimal(vault_root, tmp_path)
    payload["jobs"] = _jobs(tmp_path)
    config = ToolsConfig.from_yaml(_write(tmp_path, payload))
    assert _names(config, monkeypatch) == {
        "vault_read",
        "vault_log",
        "vault_write",
        "vault_move",
        "vault_delete",
        "vault_undo",
        "research_enqueue",
        "job_enqueue",
    }


def test_only_local_kinds_register_only_the_job_tool(
    vault_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = _minimal(vault_root, tmp_path)
    payload["jobs"] = _jobs(tmp_path, {"develop_idea": JOB_KINDS["develop_idea"]})
    config = ToolsConfig.from_yaml(_write(tmp_path, payload))
    names = _names(config, monkeypatch)
    assert "job_enqueue" in names
    assert "research_enqueue" not in names


def test_only_web_kinds_register_only_the_research_tool(
    vault_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = _minimal(vault_root, tmp_path)
    payload["jobs"] = _jobs(tmp_path, {"research": JOB_KINDS["research"]})
    config = ToolsConfig.from_yaml(_write(tmp_path, payload))
    names = _names(config, monkeypatch)
    assert "research_enqueue" in names
    assert "job_enqueue" not in names


def test_a_job_kind_must_say_whether_it_has_the_web(vault_root: Path, tmp_path: Path) -> None:
    payload = _minimal(vault_root, tmp_path)
    payload["jobs"] = _jobs(
        tmp_path, {"research": {"route": "background-research", "description": "Research."}}
    )
    with pytest.raises(ToolsConfigError, match="invalid tools config"):
        ToolsConfig.from_yaml(_write(tmp_path, payload))


def test_a_jobs_section_needs_at_least_one_kind(vault_root: Path, tmp_path: Path) -> None:
    payload = _minimal(vault_root, tmp_path)
    payload["jobs"] = _jobs(tmp_path, {})
    with pytest.raises(ToolsConfigError, match="invalid tools config"):
        ToolsConfig.from_yaml(_write(tmp_path, payload))


def test_an_unknown_key_in_a_job_kind_is_rejected(vault_root: Path, tmp_path: Path) -> None:
    payload = _minimal(vault_root, tmp_path)
    payload["jobs"] = _jobs(tmp_path)
    payload["jobs"]["kinds"]["research"]["toolset"] = "everything"
    with pytest.raises(ToolsConfigError, match="invalid tools config"):
        ToolsConfig.from_yaml(_write(tmp_path, payload))


def test_the_jobs_section_builds_its_queue_file(vault_root: Path, tmp_path: Path) -> None:
    payload = _minimal(vault_root, tmp_path)
    payload["jobs"] = _jobs(tmp_path)
    config = ToolsConfig.from_yaml(_write(tmp_path, payload))
    assert config.jobs is not None
    store = config.jobs.store()
    assert store.path == tmp_path / "state" / "jobs.sqlite3"
    assert store.list() == []


def test_disabled_weather_is_not_registered(
    vault_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = _full(vault_root, tmp_path)
    payload["weather"] = {"enabled": False}
    config = ToolsConfig.from_yaml(_write(tmp_path, payload))
    assert "weather" not in _names(config, monkeypatch)


def test_each_optional_section_is_independent(
    vault_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = _minimal(vault_root, tmp_path)
    payload["links"] = LINKS
    config = ToolsConfig.from_yaml(_write(tmp_path, payload))
    assert _names(config, monkeypatch) == {
        "vault_read",
        "vault_log",
        "vault_write",
        "vault_move",
        "vault_delete",
        "vault_undo",
        "nextcloud_link",
    }


def test_the_shipped_specs_declare_their_taints(
    vault_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _no_entry_points(monkeypatch)
    config = ToolsConfig.from_yaml(_write(tmp_path, _full(vault_root, tmp_path)))
    registry = build_registry(config, calendar_backend=FakeBackend())
    taints = {spec.name: (spec.taints_untrusted, spec.taints_private) for spec in registry.specs()}
    assert taints.pop("vault_read") == (False, True)
    assert taints.pop("vault_log") == (False, True)
    assert taints.pop("calendar_list") == (True, True)
    assert set(taints.values()) == {(False, False)}


def test_instance_entry_points_are_loaded(
    vault_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class _Params(BaseModel):
        path: str

    class _EntryPoint:
        name = "instance"

        def load(self) -> Any:
            def handler(params: BaseModel, provenance: Provenance) -> dict[str, Any]:
                return {"ok": True}

            from second_brain.core.types import Autonomy, Effect, ToolSpec

            spec = ToolSpec(
                name="ask_team",
                description="Ask the team.",
                params=_Params,
                effect=Effect.EXTERNAL,
                autonomy=Autonomy.ASK,
            )
            return [Tool(spec=spec, handler=handler)]

    monkeypatch.setattr(registry_module, "entry_points", lambda group: [_EntryPoint()])
    config = ToolsConfig.from_yaml(_write(tmp_path, _minimal(vault_root, tmp_path)))
    registry = build_registry(config)
    assert "ask_team" in {spec.name for spec in registry.specs()}
