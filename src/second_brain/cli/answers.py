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
RESERVED_CONTAINER_PATHS: tuple[Path, ...] = (Path("/vault"), Path("/data"), Path("/run/secrets"))
VAULT_IN_CONTAINER = Path("/vault")
DATA_IN_CONTAINER = Path("/data")
HERMES_WEBHOOK_BASE_URL = "http://hermes:8644/webhooks"
# The long-audio queue and work folders: under the data volume, which brain_tools and the audio
# worker share, so the tool writes a request and the worker picks it up without a host folder.
TTS_QUEUE_IN_CONTAINER = DATA_IN_CONTAINER / "tts" / "queue"
TTS_WORK_IN_CONTAINER = DATA_IN_CONTAINER / "tts" / "work"
TTS_OUTPUT_FOLDER = "tts"
# The mail sidecar stages under the data volume and reads its filter from there.
MAIL_DIR_IN_CONTAINER = DATA_IN_CONTAINER / "mail"
MAIL_STAGING_IN_CONTAINER = MAIL_DIR_IN_CONTAINER / "inbox"
MAIL_STATE_IN_CONTAINER = MAIL_DIR_IN_CONTAINER / "state.json"
MAIL_RULES_IN_CONTAINER = MAIL_DIR_IN_CONTAINER / "rules.json"
# The scheduled tasks the product ships: a skill with one of these names would collide with the
# builtin route of the same name, so it cannot be scheduled.
SCHEDULED_TASK_NAMES: tuple[str, ...] = ("digest", "lint", "brief", "triage")


class AnswersError(RuntimeError):
    """The answers file is missing, unreadable, or describes a brain that cannot be built."""


class SlackAnswers(BaseModel):
    """A team brain on Slack: the bot's user id and the channel the home messages go to."""

    model_config = ConfigDict(extra="forbid")

    owner_id: str = Field(min_length=1, description="Slack member id of the person the brain answers.")
    home_channel: str = Field(min_length=1, description="Channel id the assistant speaks in by default.")


class CalendarAnswers(BaseModel):
    """The calendar section, minus the token: that is a secret file.

    `service_account: true` says the secret is a Google service-account key, and the brain mints its
    own access tokens from it. Left out, `secrets/calendar_token` is a bearer token that expires
    after an hour and has to be replaced by hand.
    """

    model_config = ConfigDict(extra="forbid")

    assistant_calendar_id: str = Field(min_length=1)
    main_calendar_id: str = Field(min_length=1)
    service_account: bool = False


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


class TtsAnswers(BaseModel):
    """The long-audio section: where the queue and the finished files live, inside the brain."""

    model_config = ConfigDict(extra="forbid")

    image: str | None = Field(
        default=None,
        description="Image that synthesises the audio, when it is not the default second-brain one.",
    )
    voices: list[str] = Field(
        default_factory=lambda: ["claribel", "damien"],
        description="The voices the tool accepts, by name. Must match the ones the image knows.",
    )
    max_chars: int = Field(default=150_000, ge=1_000)

    @field_validator("image")
    @classmethod
    def _image_reference(cls, value: str | None) -> str | None:
        if value is None:
            return None
        text = value.strip()
        if not text or any(character.isspace() for character in text):
            raise ValueError(f"not an image reference: {value!r}")
        return text

    @field_validator("voices")
    @classmethod
    def _voice_names(cls, value: list[str]) -> list[str]:
        cleaned = [item.strip() for item in value if item.strip()]
        if not cleaned:
            raise ValueError("at least one voice is needed")
        for name in cleaned:
            if not re.fullmatch(r"[a-z][a-z0-9_]*", name):
                raise ValueError(f"not a voice name: {name!r}")
        return cleaned


class MailAnswers(BaseModel):
    """The Gmail sidecar: it stages the filtered mail, the brain writes the notes."""

    model_config = ConfigDict(extra="forbid")

    imap_host: str = Field(
        default="imap.gmail.com",
        description="The IMAP server the sidecar reads. It is also the one host the egress "
        "allowlist opens port 993 to.",
    )
    rules_file: Path | None = Field(
        default=None,
        description="The filter, as a JSON file on the host. Left out, `mail-rules.json` next to "
        "the answers file is used when it is there.",
    )
    interval_seconds: float = Field(default=900.0, gt=0)
    quiet_hours: str = Field(
        default="",
        description='A range like "22:00-07:30" where the sidecar does not run, empty for always.',
    )
    body_max: int = Field(default=8000, ge=1_000)

    @field_validator("imap_host")
    @classmethod
    def _imap_host(cls, value: str) -> str:
        host = value.strip().lower()
        if not re.fullmatch(r"[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+", host):
            raise ValueError(f"not a host name: {value!r}")
        return host

    @field_validator("rules_file")
    @classmethod
    def _rules_path(cls, value: Path | None) -> Path | None:
        return None if value is None else _absolute_path(value)

    @field_validator("quiet_hours")
    @classmethod
    def _quiet_range(cls, value: str) -> str:
        text = value.strip()
        if not text:
            return text
        try:
            start, _, end = text.partition("-")
            _clock_time(start)
            _clock_time(end)
        except ValueError as exc:
            raise ValueError(f"quiet_hours must be HH:MM-HH:MM, got {value!r}: {exc}") from None
        return text


class ScheduleAnswers(BaseModel):
    """The local times the scheduler fires the recurring tasks at, on every weekday."""

    model_config = ConfigDict(extra="forbid")

    digest: list[str] = Field(default_factory=lambda: ["08:00", "20:00"])
    lint: str = "03:30"
    brief: list[str] = Field(default_factory=lambda: ["07:00", "13:00"])
    triage: list[str] = Field(default_factory=lambda: ["21:00"])
    # Skills this instance schedules by name, each with the local times it fires at. The name is
    # both the skill folder and the route, so it is held to a name the scheduler accepts.
    skills: dict[str, list[str]] = Field(default_factory=dict)

    @field_validator("lint")
    @classmethod
    def _check_time(cls, value: str) -> str:
        return _clock_time(value)

    @field_validator("digest", "brief", "triage")
    @classmethod
    def _check_times(cls, value: list[str]) -> list[str]:
        return [_clock_time(item) for item in value]

    @field_validator("skills")
    @classmethod
    def _check_skill_times(cls, value: dict[str, list[str]]) -> dict[str, list[str]]:
        checked: dict[str, list[str]] = {}
        for skill, times in value.items():
            if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,40}", skill):
                raise ValueError(f"not a skill name: {skill!r}")
            if skill in SCHEDULED_TASK_NAMES:
                raise ValueError(f"{skill!r} is a scheduled task of its own, not a skill this file schedules")
            checked[skill] = [_clock_time(item) for item in times]
        return checked


class StagingAnswers(BaseModel):
    """The folder the owner moves candidates from another brain into.

    It is a plain host directory, mounted read-only-where-possible into brain_tools: the importer is
    the only thing that reads it, and it writes into the vault, never back into the folder except to
    move a file to `done/` or `failed/`. Absent, nothing is imported and the folder is not mounted.
    """

    model_config = ConfigDict(extra="forbid")

    host_dir: Path = Field(description="Host folder the owner copies candidates into.")
    poll_seconds: float = Field(default=60, gt=0, le=3600)

    @field_validator("host_dir")
    @classmethod
    def _absolute(cls, value: Path) -> Path:
        return _absolute_path(value)


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
    tts: TtsAnswers | None = None
    mail: MailAnswers | None = None
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


class MountAnswers(BaseModel):
    """A host folder an instance tool reads, mounted read-only inside brain_tools.

    The paths the brain owns already have a place in the container, and a mount on top of one of
    them would shadow it, so those are refused.
    """

    model_config = ConfigDict(extra="forbid")

    host: Path
    container: Path

    @field_validator("host", "container")
    @classmethod
    def _plain_absolute(cls, value: Path) -> Path:
        path = _absolute_path(value)
        if ".." in path.parts:
            raise ValueError(f"must not climb out of itself, got {value}")
        return path

    @field_validator("container")
    @classmethod
    def _not_a_brain_path(cls, value: Path) -> Path:
        for reserved in RESERVED_CONTAINER_PATHS:
            if value == reserved or reserved in value.parents:
                raise ValueError(f"container may not be {reserved} or inside it, got {value}")
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
    # A team vault is a shared repository written through pull requests: it has the systems,
    # incidents, decisions and runbooks zones, no agent memory of its own, and no documents.
    shape: Literal["personal", "team"] = "personal"
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
    instance_skills_dir: Path | None = Field(
        default=None,
        description="A folder of skills this instance adds, copied next to the product ones. "
        "Each skill is a `<name>/SKILL.md`, and `sb init` refuses one that is not valid.",
    )
    brain_image: str | None = Field(
        default=None,
        description="Image for the brain services, when the instance builds its own with its tools.",
    )
    tools: ToolsAnswers = Field(default_factory=ToolsAnswers)
    job_kinds: dict[str, JobKindAnswers] = Field(default_factory=dict)
    # Read-only host folders for instance tools: the vault, the data dir and the secrets are the
    # brain's own, and a tool that writes belongs in the core.
    brain_tools_mounts: list[MountAnswers] = Field(default_factory=list)
    # When the vault lives inside a folder another service owns (a Nextcloud data directory), both
    # writers have to be in one group: this is that group's gid on the host, and every brain-image
    # service that touches the vault joins it.
    vault_group_gid: int | None = Field(
        default=None,
        ge=0,
        description="gid of the group that owns the vault, when a second service writes it too.",
    )
    schedule: ScheduleAnswers = Field(default_factory=ScheduleAnswers)
    calls: CallsAnswers = Field(default_factory=CallsAnswers)
    # A team brain takes candidates from another brain through a folder the owner moves them into.
    staging: StagingAnswers | None = None

    @field_validator("instance_skills_dir")
    @classmethod
    def _skills_dir(cls, value: Path | None) -> Path | None:
        return None if value is None else _absolute_path(value)

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

    def tts_config(self) -> dict[str, Any] | None:
        """The `tts` block the instance tools read: the queue the tool writes and the vault to read.

        The queue lives under the brain's own data volume, the output under the media folder, so
        the audio lands where the chat can attach it and nothing has to be shared with the host.
        """
        if self.tools.tts is None:
            return None
        section = self.tools.tts
        return {
            "queue_dir": str(TTS_QUEUE_IN_CONTAINER),
            "vault_dir": str(VAULT_IN_CONTAINER),
            "max_chars": section.max_chars,
            "voices": section.voices,
            "timezone": self.timezone,
        }

    def mail_config(self) -> dict[str, Any] | None:
        """The `mail` block the instance tools read: where the sidecar stages and archives."""
        if self.tools.mail is None:
            return None
        return {
            "staging_dir": str(MAIL_STAGING_IN_CONTAINER),
            "list_limit": 20,
        }

    def mail_sidecar_config(self, secret_file: Path) -> dict[str, Any]:
        """The sidecar's own settings file, rendered into the instance: no secret value in here."""
        assert self.tools.mail is not None
        section = self.tools.mail
        return {
            "staging_dir": str(MAIL_STAGING_IN_CONTAINER),
            "state_file": str(MAIL_STATE_IN_CONTAINER),
            "imap_host": section.imap_host,
            "rules_file": str(MAIL_RULES_IN_CONTAINER),
            "interval_seconds": section.interval_seconds,
            "quiet_hours": section.quiet_hours,
            "timezone": self.timezone,
            "webhook_url": HERMES_WEBHOOK_BASE_URL,
            "webhook_secret_file": str(secret_file),
        }

    def tts_worker_config(self) -> dict[str, Any] | None:
        """The audio worker's settings: the same queue, the work folders under data, the media out."""
        if self.tools.tts is None:
            return None
        return {
            "queue_dir": str(TTS_QUEUE_IN_CONTAINER),
            "work_dir": str(TTS_WORK_IN_CONTAINER),
            "output_dir": str(self.media_dir / TTS_OUTPUT_FOLDER),
            "max_jobs": 1,
            "keep_days": 14,
            "timezone": self.timezone,
        }

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
