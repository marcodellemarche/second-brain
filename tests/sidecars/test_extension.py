"""The sidecars an instance package declares: the spec, and how they are loaded."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from second_brain.sidecars import extension
from second_brain.sidecars.extension import SidecarContext, SidecarSpec, load_sidecars
from second_brain.tools.registry import ExtensionError, ExtensionSettings

CONTEXT = SidecarContext(timezone="Europe/Rome", media_dir=Path("/srv/brain/media"))


class _EntryPoint:
    def __init__(self, name: str, factory: Any) -> None:
        self.name = name
        self._factory = factory

    def load(self) -> Any:
        return self._factory


def _installed(monkeypatch: pytest.MonkeyPatch, *points: _EntryPoint) -> None:
    monkeypatch.setattr(extension, "entry_points", lambda group: list(points))


def _spec(name: str = "worker", **fields: Any) -> SidecarSpec:
    return SidecarSpec(name=name, command=["python", "-m", "example_instance.worker"], **fields)


def test_a_factory_gets_its_own_section_and_the_timezone(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[ExtensionSettings] = []

    def factory(settings: ExtensionSettings, context: SidecarContext) -> list[SidecarSpec]:
        seen.append(settings)
        return [_spec()] if "worker" in settings.config else []

    _installed(monkeypatch, _EntryPoint("example", factory))
    specs = load_sidecars({"example": {"worker": {}}, "other": {"x": 1}}, CONTEXT, timezone="Europe/Rome")
    assert [spec.name for spec in specs] == ["worker"]
    assert seen[0].config == {"worker": {}}
    assert seen[0].timezone == "Europe/Rome"


def test_a_factory_without_its_section_declares_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    _installed(monkeypatch, _EntryPoint("example", lambda settings, context: []))
    assert load_sidecars({}, CONTEXT, timezone="UTC") == []


def test_two_sidecars_with_one_name_are_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    _installed(
        monkeypatch,
        _EntryPoint("one", lambda settings, context: [_spec()]),
        _EntryPoint("two", lambda settings, context: [_spec()]),
    )
    with pytest.raises(ExtensionError, match="declared by one and two"):
        load_sidecars({}, CONTEXT, timezone="UTC")


def test_a_factory_that_returns_something_else_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    _installed(monkeypatch, _EntryPoint("example", lambda settings, context: [{"name": "worker"}]))
    with pytest.raises(ExtensionError, match="returned dict"):
        load_sidecars({}, CONTEXT, timezone="UTC")


def test_a_failing_factory_is_raised_not_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    def factory(settings: ExtensionSettings, context: SidecarContext) -> list[SidecarSpec]:
        raise ExtensionError("no worker settings")

    _installed(monkeypatch, _EntryPoint("example", factory))
    with pytest.raises(ExtensionError, match="no worker settings"):
        load_sidecars({}, CONTEXT, timezone="UTC")


def test_the_config_path_and_the_health_marker() -> None:
    spec = _spec()
    assert str(spec.config_path) == "/etc/second-brain/worker.json"
    assert spec.health_marker == "example_instance.worker"
    assert SidecarSpec(name="relay", command=["/usr/local/bin/relay", "--quiet"]).health_marker == "relay"


@pytest.mark.parametrize(
    "fields",
    [
        {"hosts": ["not a host"]},
        {"hosts": ["https://imap.example.test"]},
        {"credentials": ["../model_api_key"]},
        {"mem_limit": "lots"},
        {"image": "an image"},
        {"files": {"relative/path": Path("/srv/x")}},
        {"files": {"/etc/example/x": Path("relative")}},
    ],
)
def test_a_spec_that_does_not_validate_is_refused(fields: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        _spec(**fields)


@pytest.mark.parametrize("argument", ["", "line\nbreak", "caffè"])
def test_a_command_argument_must_be_plain_text(argument: str) -> None:
    with pytest.raises(ValidationError):
        SidecarSpec(name="worker", command=["python", argument])
