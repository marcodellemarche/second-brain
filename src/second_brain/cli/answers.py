"""The answers file: everything `sb init` needs, and nothing that is a secret.

`sb init` is not interactive, so one YAML file describes the brain it builds and the same file
builds the same brain again. Secrets are never here: the CLI generates the internal ones and
leaves a file under `secrets/` for every one that has to come from outside.

An unknown key is refused, because a misspelled `vault_dir` would otherwise be dropped and the
vault scaffold would land in a directory the owner never named.
"""

from __future__ import annotations

import re
import urllib.parse
from pathlib import Path, PurePosixPath
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from second_brain.core.schema import SchemaError, load_schema
from second_brain.tools.archive import ArchiveConfig
from second_brain.tools.config import _reject_secret_fields
from second_brain.tools.links import LinkConfig
from second_brain.tools.notify import NotifyConfig

Language = Literal["it", "en"]
LANGUAGE_NAMES: dict[str, str] = {"it": "Italian", "en": "English"}
TEMPLATES = Path(__file__).resolve().parents[3] / "templates"


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

    initiative_path: str | None = None
    default_daily: int = Field(default=6, ge=0)


class LinksAnswers(BaseModel):
    model_config = ConfigDict(extra="forbid")

    base_url: str = Field(description="Nextcloud base URL, https only.")
    vault_folder: str = "Vault"
    archive_folder: str = "Archive"


class ScheduleAnswers(BaseModel):
    """The local times the scheduler fires the recurring tasks at, on every weekday."""

    model_config = ConfigDict(extra="forbid")

    digest: list[str] = Field(default_factory=lambda: ["08:00", "20:00"])
    lint: str = "03:30"
    brief: list[str] = Field(default_factory=lambda: ["07:00", "13:00"])

    @field_validator("lint")
    @classmethod
    def _check_time(cls, value: str) -> str:
        return _clock_time(value)

    @field_validator("digest", "brief")
    @classmethod
    def _check_times(cls, value: list[str]) -> list[str]:
        return [_clock_time(item) for item in value]


class CallsAnswers(BaseModel):
    """Where the call notes land and who counts as the team in a call."""

    model_config = ConfigDict(extra="forbid")

    capture_dir: str | None = Field(
        default=None,
        description="Vault-relative folder the call router watches; the inbox calls folder by default.",
    )
    team_domains: list[str] = Field(default_factory=list)
    personal_markers: list[str] = Field(default_factory=list)

    @field_validator("capture_dir")
    @classmethod
    def _check_capture_dir(cls, value: str | None) -> str | None:
        if value is None:
            return None
        path = PurePosixPath(value.strip())
        if not value.strip():
            raise ValueError("capture_dir must not be empty")
        if path.is_absolute():
            raise ValueError(f"capture_dir must be relative to the vault: {value!r}")
        if ".." in path.parts:
            raise ValueError(f"capture_dir must not leave the vault: {value!r}")
        return str(path)


class ToolsAnswers(BaseModel):
    """Which optional tool groups this brain has. A group left out is not built at all."""

    model_config = ConfigDict(extra="forbid")

    calendar: CalendarAnswers | None = None
    weather: bool = False
    links: LinksAnswers | None = None
    archive: ArchiveAnswers | None = None
    jobs: bool = False
    notify: NotifyAnswers | None = None
    # Entry point name to that instance package's settings. Free-form, but the same rule as the
    # tools config applies: a secret is a file, never a value in here.
    extensions: dict[str, dict[str, Any]] = Field(default_factory=dict)

    @field_validator("extensions")
    @classmethod
    def _sections(cls, value: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
        for name, section in value.items():
            if not name or name != name.strip():
                raise ValueError(f"not an entry point name: {name!r}")
            _reject_secret_fields(section, f"tools.extensions.{name}")
        return value


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
    docker_gid: int | None = Field(
        default=None,
        ge=0,
        description="Group that owns the docker socket, when it is not the one on this machine.",
    )
    model_endpoint: str = Field(description="Base URL of an OpenAI-compatible endpoint.")
    model_name: str = Field(min_length=1)
    model_context_length: int = Field(default=131072, ge=1024)
    brain_image: str | None = Field(
        default=None,
        description="Image for the brain services, when the instance builds its own with its tools.",
    )
    tools: ToolsAnswers = Field(default_factory=ToolsAnswers)
    job_kinds: dict[str, JobKindAnswers] = Field(default_factory=dict)
    schedule: ScheduleAnswers = Field(default_factory=ScheduleAnswers)
    calls: CallsAnswers = Field(default_factory=CallsAnswers)

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

    @field_validator("brain_image")
    @classmethod
    def _image_reference(cls, value: str | None) -> str | None:
        if value is None:
            return None
        text = value.strip()
        if not text or any(character.isspace() for character in text):
            raise ValueError(f"not an image reference: {value!r}")
        return text

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
        if self.tools.jobs and all(kind.web for kind in self.job_kinds.values()):
            raise ValueError(
                "the research follow-up needs a job kind with web: false to write its note; "
                "every kind here can reach the web"
            )
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
            initiative_path=section.initiative_path or self._brain_page("initiative"),
            default_daily=section.default_daily,
            timezone=self.timezone,
        )

    def _brain_page(self, name: str) -> str:
        """A `_brain` page named by the schema of this brain's language."""
        try:
            return load_schema(TEMPLATES / "vault" / self.language).page(name)
        except SchemaError as exc:
            raise AnswersError(f"cannot resolve the brain page {name!r}: {exc}") from exc


def _clock_time(value: str) -> str:
    """A local time of day as HH:MM, the only shape the scheduler accepts."""
    text = value.strip()
    if not re.fullmatch(r"([01][0-9]|2[0-3]):[0-5][0-9]", text):
        raise ValueError(f"not a local time of day in HH:MM: {value!r}")
    return text


def _absolute_path(value: Path) -> Path:
    if not value.is_absolute():
        raise ValueError(f"must be an absolute path, got {value}")
    return value
