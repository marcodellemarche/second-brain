"""The answers file: everything `sb init` needs, and nothing that is a secret.

`sb init` is not interactive, so one YAML file describes the brain it builds and the same file
builds the same brain again. Secrets are never here: the CLI generates the internal ones and
leaves a file under `secrets/` for every one that has to come from outside.

An unknown key is refused, because a misspelled `vault_dir` would otherwise be dropped and the
vault scaffold would land in a directory the owner never named.
"""

from __future__ import annotations

import urllib.parse
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from second_brain.tools.archive import ArchiveConfig
from second_brain.tools.links import LinkConfig
from second_brain.tools.notify import NotifyConfig

Language = Literal["it", "en"]
LANGUAGE_NAMES: dict[str, str] = {"it": "Italian", "en": "English"}


class AnswersError(RuntimeError):
    """The answers file is missing, unreadable, or describes a brain that cannot be built."""


class SlackAnswers(BaseModel):
    """A team brain on Slack: the bot's user id and the channel the home messages go to."""

    model_config = ConfigDict(extra="forbid")

    owner_id: str = Field(min_length=1, description="Slack member id of the person the brain answers.")
    home_channel: str = Field(min_length=1, description="Channel id the assistant speaks in by default.")


class CalendarAnswers(BaseModel):
    """The calendar section, minus the token: that is a secret file."""

    model_config = ConfigDict(extra="forbid")

    assistant_calendar_id: str = Field(min_length=1)
    main_calendar_id: str = Field(min_length=1)


class ArchiveAnswers(BaseModel):
    """The archive section: `archive_dir` says where it is, this says how it is organized."""

    model_config = ConfigDict(extra="forbid")

    discard_dir: str = "_discard"
    allowed_extensions: tuple[str, ...] = ("pdf", "jpg", "jpeg", "png", "heic", "webp")


class NotifyAnswers(BaseModel):
    model_config = ConfigDict(extra="forbid")

    initiative_path: str = "_brain/initiative.md"
    default_daily: int = Field(default=6, ge=0)


class LinksAnswers(BaseModel):
    model_config = ConfigDict(extra="forbid")

    base_url: str = Field(description="Nextcloud base URL, https only.")
    vault_folder: str = "Vault"
    archive_folder: str = "Archive"


class ToolsAnswers(BaseModel):
    """Which optional tool groups this brain has. A group left out is not built at all."""

    model_config = ConfigDict(extra="forbid")

    calendar: CalendarAnswers | None = None
    weather: bool = False
    links: LinksAnswers | None = None
    archive: ArchiveAnswers | None = None
    jobs: bool = False
    notify: NotifyAnswers | None = None


class JobKindAnswers(BaseModel):
    model_config = ConfigDict(extra="forbid")

    route: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")
    description: str = Field(min_length=1, max_length=200)
    web: bool = Field(description="True when the route can reach the web. There is no default.")


class Answers(BaseModel):
    model_config = ConfigDict(extra="forbid")

    instance_name: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,62}$")
    language: Language = "en"
    owner_name: str = Field(min_length=1)
    owner_telegram_id: str | None = None
    slack: SlackAnswers | None = None
    timezone: str = "UTC"
    vault_dir: Path
    media_dir: Path
    archive_dir: Path | None = None
    model_endpoint: str = Field(description="Base URL of an OpenAI-compatible endpoint.")
    model_name: str = Field(min_length=1)
    model_context_length: int = Field(default=131072, ge=1024)
    tools: ToolsAnswers = Field(default_factory=ToolsAnswers)
    job_kinds: dict[str, JobKindAnswers] = Field(default_factory=dict)

    @field_validator("vault_dir", "media_dir", "archive_dir")
    @classmethod
    def _absolute(cls, value: Path | None) -> Path | None:
        return None if value is None else _absolute_path(value)

    @field_validator("timezone")
    @classmethod
    def _known_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value.strip())
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"unknown timezone: {value!r}") from exc
        return value.strip()

    @field_validator("model_endpoint")
    @classmethod
    def _endpoint(cls, value: str) -> str:
        parsed = urllib.parse.urlsplit(value.strip())
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise ValueError(f"model_endpoint must be an http(s) URL, got {value!r}")
        if parsed.query or parsed.fragment:
            raise ValueError("model_endpoint may not carry a query or a fragment")
        return value.strip().rstrip("/")

    @model_validator(mode="after")
    def _one_channel_and_consistent_tools(self) -> Answers:
        if (self.owner_telegram_id is None) == (self.slack is None):
            raise ValueError("set either owner_telegram_id or a slack section, not both and not neither")
        if self.tools.archive is not None and self.archive_dir is None:
            raise ValueError("tools.archive needs archive_dir")
        if self.tools.jobs and not self.job_kinds:
            raise ValueError("tools.jobs needs at least one job kind")
        if self.job_kinds and not self.tools.jobs:
            raise ValueError("job_kinds are set but tools.jobs is false")
        for name in self.job_kinds:
            if not name or name != name.strip() or len(name) > 64:
                raise ValueError(f"not a job kind name: {name!r}")
        return self

    @classmethod
    def from_yaml(cls, path: str | Path) -> Answers:
        source = Path(path)
        try:
            raw = yaml.safe_load(source.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as exc:
            raise AnswersError(f"cannot read answers {source}: {exc}") from exc
        if not isinstance(raw, dict):
            raise AnswersError(f"answers must be a mapping: {source}")
        try:
            return cls.model_validate(raw)
        except ValueError as exc:
            raise AnswersError(f"invalid answers {source}: {exc}") from exc

    @property
    def endpoint_host(self) -> str:
        host = urllib.parse.urlsplit(self.model_endpoint).hostname
        assert host is not None
        return host

    @property
    def channel(self) -> str:
        return "Slack" if self.slack is not None else "Telegram"

    @property
    def owner_id(self) -> str:
        return self.slack.owner_id if self.slack is not None else str(self.owner_telegram_id)

    @property
    def language_name(self) -> str:
        return LANGUAGE_NAMES[self.language]

    @property
    def languages(self) -> str:
        return "Italian, English" if self.language == "it" else "English, Italian"

    @property
    def archive_config(self) -> ArchiveConfig | None:
        """The archive section of the tools config: `archive_dir` is the switch."""
        if self.archive_dir is None:
            return None
        section = self.tools.archive or ArchiveAnswers()
        return ArchiveConfig(
            root=self.archive_dir,
            inbox_media_dir=self.media_dir,
            discard_dir=section.discard_dir,
            allowed_extensions=section.allowed_extensions,
        )

    def link_config(self) -> LinkConfig | None:
        if self.tools.links is None:
            return None
        section = self.tools.links
        return LinkConfig(
            base_url=section.base_url,
            vault_folder=section.vault_folder,
            archive_folder=section.archive_folder,
        )

    def notify_config(self, data_dir: Path) -> NotifyConfig | None:
        if self.tools.notify is None:
            return None
        section = self.tools.notify
        return NotifyConfig(
            store_path=data_dir / "notifications.sqlite3",
            initiative_path=section.initiative_path,
            default_daily=section.default_daily,
            timezone=self.timezone,
        )


def _absolute_path(value: Path) -> Path:
    if not value.is_absolute():
        raise ValueError(f"must be an absolute path, got {value}")
    return value
