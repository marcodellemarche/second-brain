"""brain_tools configuration: one YAML file, read once when the server starts.

The file says which tool groups this brain has and where their inputs live. It never holds a
secret: a section that needs one points at a file instead (`calendar.token_file`), and a field
that carries a secret value is refused before anything is read. A typo stops the server from
starting, because a silently missing section would look like a tool that is simply not there.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from second_brain.core.schema import SchemaError, load_schema
from second_brain.core.types import Zone
from second_brain.tools.archive import ArchiveConfig, archive_tools
from second_brain.tools.calendar import (
    SERVICE_ACCOUNT_HOSTS,
    CalendarBackend,
    CalendarConfig,
    GoogleCalendarBackend,
    ServiceAccountToken,
    calendar_tools,
)
from second_brain.tools.calls import HandoffConfig, call_tools
from second_brain.tools.core_vault import vault_tools
from second_brain.tools.jobs import JobKind, JobStore, job_tools
from second_brain.tools.links import LinkConfig, link_tools
from second_brain.tools.memory_export import MemoryExportConfig
from second_brain.tools.notify import NotifyConfig, notify_tools
from second_brain.tools.outbox import OutboxConfig
from second_brain.tools.registry import ExtensionError, ExtensionSettings, Registry, Tool
from second_brain.tools.remote import RemoteConfig
from second_brain.tools.staging import StagingConfig
from second_brain.tools.vault import Vault, VaultConfig
from second_brain.tools.weather import weather_tools

log = logging.getLogger(__name__)

CONFIG_ENV = "BRAIN_TOOLS_CONFIG"

# Zones a result note never goes into: raw is someone else's words verbatim, the other three are
# ours or out of the way.
_NOTE_EXCLUDED_ZONES = (Zone.RAW, Zone.BRAIN, Zone.META, Zone.ARCHIVE)

# A field whose name ends in one of these holds a secret value, so it may only be a path to a file.
# Instance sections name their secrets after the service (`parcel_api_key`), so the match is on the
# trailing word and not on the whole key.
_SECRET_NAMES = frozenset(
    {"token", "access_token", "refresh_token", "client_secret", "secret", "password", "api_key"}
)


class ToolsConfigError(RuntimeError):
    """brain_tools cannot start without a usable configuration."""


class WeatherConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True


class CalendarToolsConfig(BaseModel):
    """The calendar section: the calendar ids and how the access token is obtained.

    `token_file` is a ready bearer token, which expires after an hour and then has to be replaced by
    hand. `service_account_file` is the key of a Google service account: the brain mints its own
    tokens from it, so nothing has to be refreshed. Exactly one of the two.
    """

    model_config = ConfigDict(extra="forbid")

    assistant_calendar_id: str = Field(min_length=1)
    main_calendar_id: str = Field(min_length=1)
    timezone: str = Field(min_length=1)
    token_file: Path | None = None
    service_account_file: Path | None = None

    @model_validator(mode="after")
    def _one_token_source(self) -> CalendarToolsConfig:
        if (self.token_file is None) == (self.service_account_file is None):
            raise ValueError("calendar needs exactly one of token_file and service_account_file")
        return self

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
    outbox: OutboxConfig | None = None
    # The folder the owner moves candidates from another brain into. Absent, nothing is imported.
    staging: StagingConfig | None = None
    # The folder a call handed to the team brain is written into. Absent, there is no team_handoff.
    handoff: HandoffConfig | None = None
    # The repo the vault is published into, and what is merged there. Absent, nothing syncs.
    remote: RemoteConfig | None = None
    # A read-only copy of the brain's own memory, written to one vault page. Absent, no copy.
    memory_export: MemoryExportConfig | None = None
    # Entry point name to that instance package's settings. Free-form on purpose: the core does not
    # know what an instance tool needs, it only refuses a secret value in there.
    extensions: dict[str, dict[str, Any]] = Field(default_factory=dict)
    secrets_dir: Path = Path("/run/secrets")
    # The brain's timezone, handed to the instance tools with their section.
    timezone: str = "UTC"

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
            if isinstance(key, str) and _names_a_secret(key):
                raise ValueError(f"{here}: secrets are never in this file, point at a *_file instead")
            _reject_secret_fields(item, here)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _reject_secret_fields(item, f"{path}[{index}]")


def _names_a_secret(key: str) -> bool:
    normalized = key.strip().lower()
    return any(normalized == name or normalized.endswith(f"_{name}") for name in _SECRET_NAMES)


def build_registry(
    config: ToolsConfig,
    *,
    calendar_backend: CalendarBackend | None = None,
    load_entry_points: bool = True,
    check_credentials: bool = True,
) -> Registry:
    """Every tool this brain has: the vault always, each optional group only when configured.

    The calendar needs an access token, so its backend is built here from `token_file` unless the
    caller passes one (the tests do). After the instance tools are loaded, the secrets they declare
    are read once: a brain that starts without them would only fail on the first call. `sb init`
    passes `check_credentials=False` because it builds the registry before the secrets exist.
    """
    registry = Registry()
    vault = Vault(config.vault)
    for tool in vault_tools(vault):
        registry.add(tool)
    for tool in call_tools(vault, config.handoff):
        registry.add(tool)
    for tool in _optional_tools(config, calendar_backend):
        registry.add(tool)
    if load_entry_points:
        registry.load_entry_points(settings_for=_settings_for(config))
        if check_credentials:
            _check_credentials(registry)
    return registry


def _settings_for(config: ToolsConfig) -> Callable[[str], ExtensionSettings]:
    def settings(name: str) -> ExtensionSettings:
        return ExtensionSettings(name, config.extensions.get(name, {}), config.secrets_dir, config.timezone)

    return settings


def _check_credentials(registry: Registry) -> None:
    """Read every secret an instance tool declared, so a missing one fails at startup."""
    for tool_name, settings in registry.instance_tools().items():
        spec = registry.get(tool_name).spec
        for name in spec.credentials:
            try:
                settings.secret(name)
            except ExtensionError as exc:
                raise ExtensionError(f"tool {tool_name}: {exc}") from None


def _optional_tools(config: ToolsConfig, backend: CalendarBackend | None) -> list[Tool]:
    tools: list[Tool] = []
    if config.calendar is not None:
        tools.extend(
            calendar_tools(
                backend or _calendar_backend(config),
                config.calendar.calendar_config(),
                extra_hosts=SERVICE_ACCOUNT_HOSTS if config.calendar.service_account_file else (),
            )
        )
    if config.weather is not None and config.weather.enabled:
        tools.extend(weather_tools())
    if config.links is not None:
        tools.extend(link_tools(config.links))
    if config.archive is not None:
        tools.extend(archive_tools(config.archive))
    if config.jobs is not None:
        tools.extend(job_tools(config.jobs.store(), config.jobs.kinds, _note_folders(config.vault.root)))
    if config.notify is not None:
        tools.extend(notify_tools(config.notify, config.vault.root))
    return tools


def _note_folders(root: Path) -> list[str] | None:
    """The vault folders a job result note may live in, or None when the schema does not load."""
    try:
        schema = load_schema(root)
    except SchemaError as exc:
        log.warning("no vault schema, job note_path is not checked against the zones: %s", exc)
        return None
    # Only the zones this vault actually has: the team zones are optional, so iterating the enum
    # asks a personal vault for a folder it does not carry.
    return sorted(schema.folder(zone) for zone in schema.zones if zone not in _NOTE_EXCLUDED_ZONES)


def _calendar_backend(config: ToolsConfig) -> CalendarBackend:
    """The Google backend: a service account mints its own tokens, a token file is read as it is."""
    assert config.calendar is not None
    if config.calendar.service_account_file is not None:
        return GoogleCalendarBackend(
            ServiceAccountToken(config.calendar.service_account_file), config.calendar.timezone
        )
    token_file = config.calendar.token_file
    assert token_file is not None

    def read_token() -> str:
        return token_file.read_text(encoding="utf-8").strip()

    return GoogleCalendarBackend(read_token, config.calendar.timezone)
