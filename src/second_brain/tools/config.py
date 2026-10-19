"""brain_tools configuration: one YAML file, read once when the server starts.

The file says which tool groups this brain has and where their inputs live. It never holds a
secret: a section that needs one points at a file instead (`calendar.token_file`), and a field
that carries a secret value is refused before anything is read. A typo stops the server from
starting, because a silently missing section would look like a tool that is simply not there.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from second_brain.tools.archive import ArchiveConfig, archive_tools
from second_brain.tools.calendar import (
    CalendarBackend,
    CalendarConfig,
    GoogleCalendarBackend,
    calendar_tools,
)
from second_brain.tools.core_vault import vault_tools
from second_brain.tools.jobs import JobKind, JobStore, job_tools
from second_brain.tools.links import LinkConfig, link_tools
from second_brain.tools.notify import NotifyConfig, notify_tools
from second_brain.tools.registry import Registry, Tool
from second_brain.tools.vault import Vault, VaultConfig
from second_brain.tools.weather import weather_tools

CONFIG_ENV = "BRAIN_TOOLS_CONFIG"

# A field whose name is one of these holds a secret value, so it may only be a path to a file.
_SECRET_NAMES = frozenset(
    {"token", "access_token", "refresh_token", "client_secret", "secret", "password", "api_key"}
)


class ToolsConfigError(RuntimeError):
    """brain_tools cannot start without a usable configuration."""


class WeatherConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True


class CalendarToolsConfig(BaseModel):
    """The calendar section: the calendar ids and a path to the file holding the OAuth token."""

    model_config = ConfigDict(extra="forbid")

    assistant_calendar_id: str = Field(min_length=1)
    main_calendar_id: str = Field(min_length=1)
    timezone: str = Field(min_length=1)
    token_file: Path

    def calendar_config(self) -> CalendarConfig:
        return CalendarConfig(
            assistant_calendar_id=self.assistant_calendar_id,
            main_calendar_id=self.main_calendar_id,
            timezone=self.timezone,
        )


class JobsConfig(BaseModel):
    """The background job section: the queue file and the kinds this brain can queue.

    A kind says whether its webhook route can reach the web, which decides whether enqueueing it
    is an internal call or an external one. A kind whose route is not listed here cannot be
    queued at all, so the model cannot invent a route.
    """

    model_config = ConfigDict(extra="forbid")

    store_path: Path
    kinds: dict[str, JobKind] = Field(min_length=1)

    @field_validator("kinds")
    @classmethod
    def _check_kinds(cls, value: dict[str, JobKind]) -> dict[str, JobKind]:
        for name in value:
            if not name or name != name.strip() or len(name) > 64:
                raise ValueError(f"not a job kind name: {name!r}")
        return value

    def store(self) -> JobStore:
        return JobStore(self.store_path)


class Listen(BaseModel):
    """Where the server listens, written in the file as one `host:port` string."""

    model_config = ConfigDict(extra="forbid")

    host: str = Field(default="127.0.0.1", min_length=1)
    port: int = Field(default=8765, ge=1, le=65535)

    @model_validator(mode="before")
    @classmethod
    def _from_string(cls, raw: Any) -> Any:
        if not isinstance(raw, str):
            return raw
        host, _, port = raw.strip().rpartition(":")
        if not host:
            raise ValueError(f"listen must be host:port, got {raw!r}")
        return {"host": host, "port": int(port)}

    def __str__(self) -> str:
        return f"{self.host}:{self.port}"


class ToolsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    vault: VaultConfig
    token_file: Path
    listen: Listen = Field(default_factory=Listen)
    human_commit_seconds: float = Field(default=300, gt=0)
    calendar: CalendarToolsConfig | None = None
    weather: WeatherConfig | None = None
    links: LinkConfig | None = None
    archive: ArchiveConfig | None = None
    jobs: JobsConfig | None = None
    notify: NotifyConfig | None = None

    @model_validator(mode="before")
    @classmethod
    def _no_inline_secrets(cls, raw: Any) -> Any:
        """Refuse a secret value anywhere in the file: only a path to a file is allowed."""
        if isinstance(raw, Mapping):
            _reject_secret_fields(raw, "config")
        return raw

    @classmethod
    def load(cls, environ: Mapping[str, str] | None = None) -> ToolsConfig:
        env = os.environ if environ is None else environ
        path = env.get(CONFIG_ENV, "").strip()
        if not path:
            raise ToolsConfigError(f"{CONFIG_ENV} is not set")
        return cls.from_yaml(path)

    @classmethod
    def from_yaml(cls, path: str | Path) -> ToolsConfig:
        source = Path(path)
        try:
            raw = yaml.safe_load(source.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as exc:
            raise ToolsConfigError(f"cannot read tools config {source}: {exc}") from exc
        if raw is None:
            raw = {}
        if not isinstance(raw, dict):
            raise ToolsConfigError(f"tools config must be a mapping: {source}")
        try:
            return cls.model_validate(raw)
        except ValueError as exc:
            raise ToolsConfigError(f"invalid tools config {source}: {exc}") from exc

    def read_token(self) -> str:
        """The bearer token, read from its file at startup so it never lands in the config."""
        try:
            token = self.token_file.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise ToolsConfigError(f"cannot read token file {self.token_file}: {exc}") from exc
        if not token:
            raise ToolsConfigError(f"token file {self.token_file} is empty")
        return token


def _reject_secret_fields(value: Any, path: str) -> None:
    """Walk the raw mapping, refusing a secret name unless it only points at a file."""
    if isinstance(value, Mapping):
        for key, item in value.items():
            here = f"{path}.{key}"
            if isinstance(key, str) and key.strip().lower() in _SECRET_NAMES:
                raise ValueError(f"{here}: secrets are never in this file, point at a *_file instead")
            _reject_secret_fields(item, here)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _reject_secret_fields(item, f"{path}[{index}]")


def build_registry(
    config: ToolsConfig,
    *,
    calendar_backend: CalendarBackend | None = None,
    load_entry_points: bool = True,
) -> Registry:
    """Every tool this brain has: the vault always, each optional group only when configured.

    The calendar needs an access token, so its backend is built here from `token_file` unless the
    caller passes one (the tests do).
    """
    registry = Registry()
    vault = Vault(config.vault)
    for tool in vault_tools(vault):
        registry.add(tool)
    for tool in _optional_tools(config, calendar_backend):
        registry.add(tool)
    if load_entry_points:
        registry.load_entry_points()
    return registry


def _optional_tools(config: ToolsConfig, backend: CalendarBackend | None) -> list[Tool]:
    tools: list[Tool] = []
    if config.calendar is not None:
        tools.extend(calendar_tools(backend or _calendar_backend(config), config.calendar.calendar_config()))
    if config.weather is not None and config.weather.enabled:
        tools.extend(weather_tools())
    if config.links is not None:
        tools.extend(link_tools(config.links))
    if config.archive is not None:
        tools.extend(archive_tools(config.archive))
    if config.jobs is not None:
        tools.extend(job_tools(config.jobs.store(), config.jobs.kinds))
    if config.notify is not None:
        tools.extend(notify_tools(config.notify, config.vault.root))
    return tools


def _calendar_backend(config: ToolsConfig) -> CalendarBackend:
    """The Google backend, reading the token from its file on every call."""
    assert config.calendar is not None
    token_file = config.calendar.token_file

    def read_token() -> str:
        return token_file.read_text(encoding="utf-8").strip()

    return GoogleCalendarBackend(read_token, config.calendar.timezone)
