"""The answers file: everything `sb init` needs, and nothing that is a secret.

`sb init` is not interactive, so one YAML file describes the brain it builds and the same file
builds the same brain again. Secrets are never here: the CLI generates the internal ones and
leaves a file under `secrets/` for every one that has to come from outside.

An unknown key is refused, because a misspelled `vault_dir` would otherwise be dropped and the
vault scaffold would land in a directory the owner never named.
"""

from __future__ import annotations

import ipaddress
import re
import urllib.parse
from pathlib import Path, PurePosixPath
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from second_brain.core.policy import PolicyConfig
from second_brain.core.schema import SchemaError, load_schema
from second_brain.core.types import Zone
from second_brain.tools.archive import ATTACHMENT_INBOX, ArchiveConfig
from second_brain.tools.config import _reject_secret_fields
from second_brain.tools.links import LinkConfig
from second_brain.tools.notify import NotifyConfig

Language = Literal["it", "en"]
LANGUAGE_NAMES: dict[str, str] = {"it": "Italian", "en": "English"}
TEMPLATES = Path(__file__).resolve().parents[3] / "templates"
RESERVED_CONTAINER_PATHS: tuple[Path, ...] = (Path("/vault"), Path("/data"), Path("/run/secrets"))
VAULT_IN_CONTAINER = Path("/vault")
DATA_IN_CONTAINER = Path("/data")
# The scheduled tasks the product ships: a skill with one of these names would collide with the
# builtin route of the same name, so it cannot be scheduled.
SCHEDULED_TASK_NAMES: tuple[str, ...] = ("digest", "lint", "brief", "triage")


class AnswersError(RuntimeError):
    """The answers file is missing, unreadable, or describes a brain that cannot be built."""


SLACK_MEMBER_ID = re.compile(r"[UW][A-Z0-9]{2,}")


class SlackAnswers(BaseModel):
    """A team brain on Slack: the bot's user id and the channel the home messages go to."""

    model_config = ConfigDict(extra="forbid")

    owner_id: str = Field(min_length=1, description="Slack member id of the person the brain answers.")
    home_channel: str = Field(min_length=1, description="Channel id the assistant speaks in by default.")
    members: list[str] = Field(
        default_factory=list,
        description="Slack member ids of colleagues who may write to the brain in a direct message.",
    )
    approvers: list[str] = Field(
        default_factory=list,
        description="Slack member ids of colleagues who may also ask for actions and approve them, "
        "in a direct message.",
    )

    @field_validator("members", "approvers")
    @classmethod
    def _check_ids(cls, value: list[str]) -> list[str]:
        for member in value:
            if not SLACK_MEMBER_ID.fullmatch(member):
                raise ValueError(f"not a Slack member id: {member!r}")
        if len(set(value)) != len(value):
            raise ValueError(f"a Slack member is listed twice: {value}")
        return value

    @model_validator(mode="after")
    def _one_role_each(self) -> SlackAnswers:
        if self.owner_id in self.members or self.owner_id in self.approvers:
            raise ValueError("the owner is neither a member nor an approver: leave them out of both lists")
        both = sorted(set(self.members) & set(self.approvers))
        if both:
            raise ValueError(f"one role per person: {', '.join(both)} is both a member and an approver")
        return self


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


class HandoffAnswers(BaseModel):
    """The folder a call handed to the team brain is written into, on the personal brain's host.

    The owner moves what lands there to the team brain's staging folder: `move_hint` is the command
    the brain repeats to them after each hand-off. Absent, the brain has no team_handoff tool.
    """

    model_config = ConfigDict(extra="forbid")

    host_dir: Path = Field(description="Host folder the handed-off calls are written into.")
    move_hint: str = Field(default="", description="How the owner moves the folder to the team brain.")

    @field_validator("host_dir")
    @classmethod
    def _absolute(cls, value: Path) -> Path:
        return _absolute_path(value)


class MemoryExportAnswers(BaseModel):
    """A copy of what the brain remembers, written into one vault page.

    Hermes keeps `MEMORY.md` and `USER.md` under its own home and nowhere else. This copies them into
    the vault on a timer, so the memory has a history and the owner can read it. The page is
    overwritten at every tick: it is a copy, and editing it changes nothing. A personal memory can
    hold private facts, so its page lands in `_brain` unless the answers name another one.
    """

    model_config = ConfigDict(extra="forbid")

    page: str | None = Field(
        default=None,
        description="Vault-relative page of the copy; `_meta/brain-memory.md` for a team brain, "
        "`_brain/hermes-memory.md` for a personal one.",
    )
    poll_seconds: float = Field(default=3600, gt=0, le=86400)

    @field_validator("page")
    @classmethod
    def _page(cls, value: str | None) -> str | None:
        if value is None:
            return None
        text = value.strip()
        if not text:
            raise ValueError("memory_export.page must not be empty")
        path = PurePosixPath(text)
        if path.is_absolute():
            raise ValueError(f"memory_export.page must be relative to the vault: {value!r}")
        if ".." in path.parts:
            raise ValueError(f"memory_export.page must not leave the vault: {value!r}")
        if path.suffix.lower() != ".md":
            raise ValueError(f"memory_export.page must be a markdown file: {value!r}")
        return str(path)


class VaultRemoteAnswers(BaseModel):
    """The repo the vault is published into: one folder of it, and the token in a secret file.

    The vault stays the local git repository it is. brain_tools merges its commits into `<prefix>/`
    of the branch, and with `pull: true` it also writes back what was merged there. Nothing outside
    the prefix is ever touched, so the code that lives in the same repo is safe.
    """

    model_config = ConfigDict(extra="forbid")

    url: str = Field(description="https URL of the git repo the vault is published into.")
    branch: str = Field(default="main", min_length=1)
    prefix: str = Field(default="vault", min_length=1)
    pull: bool = Field(default=False, description="Also write back what was merged under the prefix.")
    poll_seconds: float = Field(default=300, gt=0, le=86400)

    @field_validator("url")
    @classmethod
    def _https(cls, value: str) -> str:
        text = value.strip()
        if not text.startswith("https://") or len(text) == len("https://"):
            raise ValueError(f"vault_remote.url must start with https://, got {value!r}")
        return text


class CallsAnswers(BaseModel):
    """Where the call notes land and who counts as the team in a call."""

    model_config = ConfigDict(extra="forbid")

    capture_dir: str | None = Field(
        default=None,
        description="Vault-relative folder the call router watches; the inbox calls folder by default.",
    )
    team_domains: list[str] = Field(default_factory=list)
    personal_markers: list[str] = Field(default_factory=list)
    # Words in the event title that make a call among colleagues team work, a 1:1 included.
    team_markers: list[str] = Field(default_factory=list)

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


# Carrier-grade NAT, where a mesh VPN puts its peers: the proxy denies it, Python does not call it
# private.
_SHARED_ADDRESS_SPACE = ipaddress.ip_network("100.64.0.0/10")


class LanEgressAnswers(BaseModel):
    """An allowlisted host that lives on the local network, let through at one private address.

    The proxy denies every private address, so a self-hosted service whose name resolves to the
    LAN is refused like an internal container would be. This opens exactly one name at exactly one
    address: the proxy allows the CONNECT only when both match.
    """

    model_config = ConfigDict(extra="forbid")

    host: str = Field(
        max_length=253,
        pattern=r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)+$",
    )
    address: ipaddress.IPv4Address | ipaddress.IPv6Address

    @field_validator("address")
    @classmethod
    def _a_lan_address(
        cls, value: ipaddress.IPv4Address | ipaddress.IPv6Address
    ) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
        # Loopback is where Docker's DNS lives and link-local is the cloud metadata service: never a
        # service on the LAN. A public address needs no exception, the allowlist already covers it.
        if value.is_loopback or value.is_link_local or value.is_multicast or value.is_unspecified:
            raise ValueError(f"not a LAN address: {value}")
        if not (value.is_private or value in _SHARED_ADDRESS_SPACE):
            raise ValueError(f"{value} is public: list the host in the allowlist, not here")
        return value


class JobKindAnswers(BaseModel):
    model_config = ConfigDict(extra="forbid")

    route: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")
    description: str = Field(min_length=1, max_length=200)
    web: bool = Field(description="True when the route can reach the web. There is no default.")


class StackLimitsAnswers(BaseModel):
    """A ceiling for every container of the instance, enforced by the kernel through a slice.

    The per-service limits are what one container may use; this is what the whole stack may use,
    for a host that runs other things. Docker puts each container under `<slice>` with
    `cgroup_parent`, and systemd holds `MemoryMax` and `CPUQuota` on that slice, so the total is
    the kernel's job and not a sum somebody has to keep in step with the compose file.
    """

    model_config = ConfigDict(extra="forbid")

    slice: str = Field(pattern=r"^[a-zA-Z0-9_-]+\.slice$")
    memory_mb: int = Field(ge=256)
    cpus: float = Field(gt=0)


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
    model_endpoint: str = Field(description="Base URL of an OpenAI-compatible endpoint.")
    model_name: str = Field(min_length=1)
    model_context_length: int = Field(default=131072, ge=1024)
    # What Hermes asks the endpoint for. `none` sends thinking off, which every endpoint accepts;
    # a level the model does not know is refused by the endpoint on every turn.
    # `auto` leaves the choice to an endpoint that lets the model decide.
    model_reasoning_effort: Literal["none", "minimal", "low", "medium", "high", "xhigh", "max", "auto"] = (
        "none"
    )
    instance_skills_dir: Path | None = Field(
        default=None,
        description="A folder of skills this instance adds, copied next to the product ones. "
        "Each skill is a `<name>/SKILL.md`, and `sb init` refuses one that is not valid.",
    )
    brain_image: str | None = Field(
        default=None,
        description="Image for the brain services, when the instance builds its own with its tools.",
    )
    stack_limits: StackLimitsAnswers | None = None
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
    # Per-tool autonomy overrides and the run budget, copied into the guard config as it is.
    policy: PolicyConfig = Field(default_factory=PolicyConfig)
    schedule: ScheduleAnswers = Field(default_factory=ScheduleAnswers)
    calls: CallsAnswers = Field(default_factory=CallsAnswers)
    # A team brain takes candidates from another brain through a folder the owner moves them into.
    staging: StagingAnswers | None = None
    # A personal brain hands team calls over through a folder the owner moves to the team brain.
    handoff: HandoffAnswers | None = None
    # The repo the vault is published into, and what is merged back from it.
    vault_remote: VaultRemoteAnswers | None = None
    # A one-way copy of the memory the brain keeps for itself, so it has a history and can be read.
    memory_export: MemoryExportAnswers | None = None
    # Allowlisted hosts that resolve to the local network, each at the one address it may have.
    egress_lan: list[LanEgressAnswers] = Field(default_factory=list)

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
        if self.slack is not None and self.colleague_ids and self.shape != "team":
            raise ValueError(
                "slack.members and slack.approvers are for a team brain: a personal brain answers only "
                "its owner"
            )
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
    def member_ids(self) -> tuple[str, ...]:
        """Colleagues a team brain also answers, in direct messages only."""
        return tuple(self.slack.members) if self.slack is not None else ()

    @property
    def approver_ids(self) -> tuple[str, ...]:
        """Colleagues who may also ask for actions and approve them, in direct messages only."""
        return tuple(self.slack.approvers) if self.slack is not None else ()

    @property
    def colleague_ids(self) -> tuple[str, ...]:
        return self.member_ids + self.approver_ids

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
            attachment_inbox=ATTACHMENT_INBOX,
            discard_dir=section.discard_dir,
            allowed_extensions=section.allowed_extensions,
            # A shared vault means a shared data folder, and the archive sits in it too.
            file_mode=0o666 if self.vault_group_gid is not None else 0o644,
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

    def memory_page(self) -> str | None:
        """The page the memory copy goes to, or None when this brain exports no memory.

        A team vault has no `_brain` of its own, so the copy is a `_meta` page the whole team reads.
        A personal memory can hold private facts, so its copy stays in the private zone.
        """
        if self.memory_export is None:
            return None
        if self.memory_export.page:
            return self.memory_export.page
        schema = load_schema(TEMPLATES / "vault" / self.language)
        if self.shape == "team":
            return f"{schema.folder(Zone.META)}/brain-memory.md"
        return f"{schema.folder(Zone.BRAIN)}/hermes-memory.md"

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
