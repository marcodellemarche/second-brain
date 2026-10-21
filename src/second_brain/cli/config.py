"""The config files `sb init` renders, and the values the templates need.

Everything here is derived from the answers: the tools config is the `ToolsConfig` the server
reads, the guard config is that same registry exported through `export_guard_classes` so the two
cannot disagree about a tool, and the squid allowlist is the union of the hosts the enabled tools
declare plus the model endpoint and the chat API.

Paths in these files are the container's, never the host's: the vault is `/vault`, its state is
`/data`, and a secret is a file under `/run/secrets`.
"""

from __future__ import annotations

import subprocess
import tempfile
import urllib.parse
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from second_brain.cli.answers import (
    MAIL_RULES_IN_CONTAINER,
    Answers,
    AnswersError,
    StackLimitsAnswers,
)
from second_brain.cli.render import render_template
from second_brain.cli.secrets import (
    CALENDAR_TOKEN,
    OUTBOX_SECRET,
    SEARXNG_KEY,
    TOOLS_TOKEN,
    VAULT_REMOTE_TOKEN,
    WEBHOOK_IDEA,
    WEBHOOK_INGEST,
    WEBHOOK_RESEARCH,
    WEBHOOK_SCHEDULE,
)
from second_brain.core.schema import VaultSchema, load_schema
from second_brain.core.types import Zone
from second_brain.guard.config import GuardConfig
from second_brain.sidecars.call_router import RouterConfig
from second_brain.sidecars.config import (
    CallRouterSection,
    JobRunnerSection,
    SchedulerSection,
    SidecarsConfig,
    VaultWatcherSection,
)
from second_brain.sidecars.scheduler import ScheduleConfig, ScheduleEntry
from second_brain.sidecars.vault_watcher import DEFAULT_IGNORE
from second_brain.tools.config import (
    CalendarToolsConfig,
    JobsConfig,
    Listen,
    ToolsConfig,
    WeatherConfig,
    build_registry,
)
from second_brain.tools.export import export_guard_classes
from second_brain.tools.jobs import JobKind
from second_brain.tools.outbox import OutboxConfig
from second_brain.tools.registry import Registry
from second_brain.tools.remote import RemoteConfig
from second_brain.tools.staging import StagingConfig
from second_brain.tools.vault import VaultConfig

VAULT_IN_CONTAINER = Path("/vault")
DATA_IN_CONTAINER = Path("/data")
SECRETS_IN_CONTAINER = Path("/run/secrets")
STAGING_IN_CONTAINER = Path("/staging")
TEMPLATES = Path(__file__).resolve().parents[3] / "templates"
HERMES_WEBHOOK_BASE_URL = "http://hermes:8644/webhooks"
SCHEDULE_STATE_FILE = DATA_IN_CONTAINER / "scheduler.json"
DOCKER_SOCKET = Path("/var/run/docker.sock")
SEARXNG_IMAGE = "searxng/searxng:2026.10.19-81371861f"
TTS_IMAGE = "second-brain/tts:0.1.0"

SCHEDULE_ROUTES: dict[str, str] = {
    "digest": "scheduled-digest",
    "lint": "scheduled-lint",
    "brief": "scheduled-brief",
    "triage": "scheduled-triage",
}
INGEST_ROUTE = "ingest"

TELEGRAM_HOSTS: tuple[str, ...] = ("api.telegram.org",)
SLACK_HOSTS: tuple[str, ...] = ("slack.com", "wss-primary.slack.com")
# The engines the SearXNG settings enable: its container reaches the internet, so squid has to
# allow them. A name missing here is a search that silently returns nothing.
SEARXNG_UPSTREAM_HOSTS: tuple[str, ...] = (
    "duckduckgo.com",
    "html.duckduckgo.com",
    "lite.duckduckgo.com",
    "search.brave.com",
    "wikipedia.org",
    "api.qwant.com",
)


def vault_schema(language: str) -> VaultSchema:
    """The zone map of the vault template this instance is scaffolded from."""
    return load_schema(TEMPLATES / "vault" / language)


def jobs_config(answers: Answers) -> JobsConfig | None:
    """The job kinds this brain can queue, or None when jobs are off."""
    if not answers.tools.jobs:
        return None
    return JobsConfig(
        store_path=DATA_IN_CONTAINER / "jobs.sqlite3",
        kinds={
            name: JobKind(route=kind.route, description=kind.description, web=kind.web)
            for name, kind in answers.job_kinds.items()
        },
    )


def outbox_config(answers: Answers) -> OutboxConfig | None:
    """Where a research run's reply lands, and the vault kind that writes the final note.

    A web run cannot write its result anywhere, so a brain whose kinds can all reach the web has
    nothing to hand it back to: `Answers` refuses that case, and there is no outbox here.
    """
    if not answers.tools.jobs:
        return None
    followup = next((name for name in sorted(answers.job_kinds) if not answers.job_kinds[name].web), None)
    if followup is None:
        return None
    return OutboxConfig(
        dir=DATA_IN_CONTAINER / "outbox",
        secret_file=SECRETS_IN_CONTAINER / OUTBOX_SECRET,
        research_followup_kind=followup,
    )


def staging_config(answers: Answers) -> StagingConfig | None:
    """Where candidates from another brain land, or None when this brain takes none."""
    if answers.staging is None:
        return None
    return StagingConfig(dir=STAGING_IN_CONTAINER, poll_seconds=answers.staging.poll_seconds)


def remote_config(answers: Answers) -> RemoteConfig | None:
    """The repo the vault is published into, or None when this brain has no remote."""
    if answers.vault_remote is None:
        return None
    return RemoteConfig(
        url=answers.vault_remote.url,
        branch=answers.vault_remote.branch,
        prefix=answers.vault_remote.prefix,
        pull=answers.vault_remote.pull,
        token_file=SECRETS_IN_CONTAINER / VAULT_REMOTE_TOKEN,
        workdir=DATA_IN_CONTAINER / "remote",
        state_file=DATA_IN_CONTAINER / "remote.json",
        poll_seconds=answers.vault_remote.poll_seconds,
    )


def tools_config(answers: Answers) -> ToolsConfig:
    """The tools config this instance runs with. Secrets are paths, never values."""
    tools = answers.tools
    calendar = None
    if tools.calendar is not None:
        if tools.calendar.service_account:
            calendar = CalendarToolsConfig(
                assistant_calendar_id=tools.calendar.assistant_calendar_id,
                main_calendar_id=tools.calendar.main_calendar_id,
                timezone=answers.timezone,
                service_account_file=SECRETS_IN_CONTAINER / "calendar_token",
            )
        else:
            calendar = CalendarToolsConfig(
                assistant_calendar_id=tools.calendar.assistant_calendar_id,
                main_calendar_id=tools.calendar.main_calendar_id,
                timezone=answers.timezone,
                token_file=SECRETS_IN_CONTAINER / "calendar_token",
            )
    return ToolsConfig(
        vault=VaultConfig(
            root=VAULT_IN_CONTAINER,
            immutable=[vault_schema(answers.language).folder(Zone.RAW)],
            # A shared vault is written by another service too. 0o664 would be enough if that
            # service could join the group, but Apache drops supplementary groups when it switches
            # to www-data, so the file has to be world-writable. The folder lives inside the other
            # service's own data directory, which only that service and root can enter.
            file_mode=0o666 if answers.vault_group_gid is not None else 0o644,
        ),
        token_file=SECRETS_IN_CONTAINER / TOOLS_TOKEN,
        listen=Listen(host="0.0.0.0", port=8765),
        calendar=calendar,
        weather=WeatherConfig(enabled=True) if tools.weather else None,
        links=answers.link_config(),
        archive=answers.archive_config,
        jobs=jobs_config(answers),
        notify=answers.notify_config(DATA_IN_CONTAINER),
        outbox=outbox_config(answers),
        staging=staging_config(answers),
        remote=remote_config(answers),
        extensions={**tools.extensions, **instance_sections(answers)},
    )


def instance_sections(answers: Answers) -> dict[str, dict[str, Any]]:
    """The `extensions.personal` blocks the core knows how to fill, merged under the answers.

    The tts and mail blocks are not free-form like the rest of `extensions`: their paths are the
    ones the containers actually have, so a typo in the answers file cannot point a tool at a
    folder nobody mounted. A brain without the block gets no tool.
    """
    sections: dict[str, dict[str, Any]] = {}
    personal: dict[str, Any] = {}
    tts = answers.tts_config()
    if tts is not None:
        personal["tts"] = tts
    mail = answers.mail_config()
    if mail is not None:
        personal["mail"] = mail
    if personal:
        sections["personal"] = {**personal, **answers.tools.extensions.get("personal", {})}
    return sections


def export_registry(config: ToolsConfig) -> Registry:
    """The registry to export the guard table from, built without touching the real paths.

    The tools' specs are what the guard needs, but building the registry also opens the job and
    notification stores and a `Vault`, which want `/vault` and `/data` to exist. On the host they
    do not, so the paths are redirected into a throwaway directory: the specs are the same ones
    the containers will serve.

    The instance tools are loaded, because they are tools the guard has to decide on and hosts the
    egress proxy has to allow. Their secrets are not checked: `sb init` runs before they exist.
    """
    with tempfile.TemporaryDirectory(prefix="sb-export-") as scratch:
        root = Path(scratch)
        vault_root = root / "vault"
        git_init(vault_root)
        data = root / "data"
        media = root / "media"
        media.mkdir()
        archive = root / "archive"
        archive.mkdir()
        redirected = config.model_copy(
            update={
                "vault": config.vault.model_copy(update={"root": vault_root}),
                "jobs": config.jobs.model_copy(update={"store_path": data / "jobs.sqlite3"})
                if config.jobs is not None
                else None,
                "notify": config.notify.model_copy(update={"store_path": data / "notifications.sqlite3"})
                if config.notify is not None
                else None,
                "archive": config.archive.model_copy(update={"root": archive, "inbox_media_dir": media})
                if config.archive is not None
                else None,
            }
        )
        return build_registry(redirected, load_entry_points=True, check_credentials=False)


def git_init(root: Path) -> None:
    """A vault is a git repository: brain_tools commits every write and undo walks that history."""
    root.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(root)], capture_output=True, text=True, check=False)


def guard_config(answers: Answers, registry: Registry) -> GuardConfig:
    """The guard config: the tool table exported from the registry, plus the vault paths."""
    exported = export_guard_classes(registry)
    for name in answers.policy.tools:
        if name not in exported:
            raise AnswersError(f"policy names an unknown tool: {name}")
    schema = vault_schema(answers.language)
    return GuardConfig(
        vault_root=str(VAULT_IN_CONTAINER),
        guard_dir="/guard",
        hermes_config_path="/etc/hermes/config.yaml",
        policy=answers.policy,
        private_prefixes=(schema.folder("_brain"), schema.folder("documents")),
        untrusted_prefixes=(schema.folder("raw"),),
        shape=answers.shape,
        media_dir=str(answers.media_dir),
        mcp_tools=exported,
        admin_senders=(answers.owner_id,),
        language=answers.language,
    )


def capture_dir(answers: Answers) -> str:
    """The vault-relative folder the call router watches: the inbox calls folder unless set."""
    if answers.calls.capture_dir is not None:
        return answers.calls.capture_dir
    return f"{vault_schema(answers.language).folder(Zone.INBOX)}/{Zone.CALLS.value}"


def scheduled_skill_route(skill: str) -> str:
    """The webhook route a scheduled skill posts to: one route per skill, named after it."""
    return f"scheduled-{skill}"


def sidecars_config(answers: Answers) -> SidecarsConfig:
    """The sidecars config: what each sidecar watches, and the secret file of every route it posts to.

    A call note lands in the capture folder and the call router is the only sidecar allowed to
    report it, so the vault watcher ignores that folder: one call, one event.
    """
    route_secrets: dict[str, Path] = {INGEST_ROUTE: SECRETS_IN_CONTAINER / WEBHOOK_INGEST}
    for kind in answers.job_kinds.values():
        secret = WEBHOOK_RESEARCH if kind.web else WEBHOOK_IDEA
        route_secrets[kind.route] = SECRETS_IN_CONTAINER / secret
    for route in SCHEDULE_ROUTES.values():
        route_secrets[route] = SECRETS_IN_CONTAINER / WEBHOOK_SCHEDULE
    for skill in answers.schedule.skills:
        route_secrets[scheduled_skill_route(skill)] = SECRETS_IN_CONTAINER / WEBHOOK_SCHEDULE

    calls = answers.calls
    jobs = jobs_config(answers)
    return SidecarsConfig(
        vault_dir=VAULT_IN_CONTAINER,
        webhook_base_url=HERMES_WEBHOOK_BASE_URL,
        route_secrets=route_secrets,
        vault_watcher=VaultWatcherSection(ignore=[*DEFAULT_IGNORE, capture_dir(answers)]),
        call_router=CallRouterSection(
            watch_dir=capture_dir(answers),
            rules=RouterConfig(
                team_domains=calls.team_domains,
                personal_markers=calls.personal_markers,
            ),
        ),
        job_runner=JobRunnerSection(jobs=jobs) if jobs is not None else None,
        scheduler=SchedulerSection(
            schedule=ScheduleConfig(
                timezone=answers.timezone,
                entries=_schedule_entries(answers),
            ),
            state_file=SCHEDULE_STATE_FILE,
        ),
    )


def _schedule_entries(answers: Answers) -> list[ScheduleEntry]:
    """The recurring tasks: one entry per local time, numbered because names cannot repeat."""
    schedule = answers.schedule
    entries = [
        ScheduleEntry(name=f"digest-{index}", route=SCHEDULE_ROUTES["digest"], at=at)
        for index, at in enumerate(schedule.digest, start=1)
    ]
    entries.append(ScheduleEntry(name="lint", route=SCHEDULE_ROUTES["lint"], at=schedule.lint))
    entries += [
        ScheduleEntry(name=f"brief-{index}", route=SCHEDULE_ROUTES["brief"], at=at)
        for index, at in enumerate(schedule.brief, start=1)
    ]
    entries += [
        ScheduleEntry(name=f"triage-{index}", route=SCHEDULE_ROUTES["triage"], at=at)
        for index, at in enumerate(schedule.triage, start=1)
    ]
    for skill in sorted(schedule.skills):
        entries += [
            ScheduleEntry(name=f"{skill}-{index}", route=scheduled_skill_route(skill), at=at)
            for index, at in enumerate(schedule.skills[skill], start=1)
        ]
    return entries


def egress_hosts(answers: Answers, registry: Registry) -> list[str]:
    """Every host the egress proxy allows: the model, the chat API, and the enabled tools."""
    hosts: list[str] = [answers.endpoint_host]
    hosts += list(SLACK_HOSTS if answers.slack is not None else TELEGRAM_HOSTS)
    # The mail sidecar reads IMAP through the proxy, so its host has to be on the allowlist even
    # though no tool declares it: the sidecar is not a tool.
    if answers.tools.mail is not None:
        hosts.append(answers.tools.mail.imap_host)
    # The sync reaches the git repo through the proxy, and git is not a tool that declares hosts.
    if answers.vault_remote is not None:
        remote_host = urllib.parse.urlparse(answers.vault_remote.url).hostname
        if remote_host:
            hosts.append(remote_host)
    for spec in registry.specs():
        hosts += list(spec.hosts)
    ordered: dict[str, None] = {}
    for host in hosts:
        ordered.setdefault(host, None)
    return list(ordered)


def docker_gid(answers: Answers) -> int:
    """The group that owns the docker socket: the answer, else this machine's.

    The watchdog talks to the socket as the brain image's group, so compose has to give it a
    group id the socket accepts. `sb init` runs on the host, where the socket can be looked at;
    run in a container with no socket, there is nothing to look at and the answer has to say it.
    """
    if answers.docker_gid is not None:
        return answers.docker_gid
    try:
        return DOCKER_SOCKET.stat().st_gid
    except OSError as exc:
        raise AnswersError(
            "cannot find the docker socket here: set docker_gid in the answers (the group of "
            "/var/run/docker.sock on the Docker host, `stat -c %g /var/run/docker.sock`)"
        ) from exc


def model_upstream(answers: Answers) -> str:
    """The endpoint as the gate needs it: no trailing `/v1`, because it forwards the path as is.

    The answers hold the OpenAI base URL, `/v1` included. A request to the gate arrives as
    `/v1/chat/completions`, so an upstream that already ends in `/v1` would make it `/v1/v1/...`.
    """
    endpoint = answers.model_endpoint.rstrip("/")
    if endpoint.endswith("/v1"):
        return endpoint[: -len("/v1")]
    return endpoint


def brain_tools_extra_volumes(answers: Answers) -> list[str]:
    """The host paths brain_tools needs mounted at the same path inside the container.

    The archive config carries host paths, and the tool moves files between them, so the archive
    root has to be there under its own name rather than under `/vault`. The mounts from the
    answers are always read-only: a tool that has to write belongs in the core.
    """
    volumes: list[str] = []
    archive = answers.archive_config
    if archive is not None:
        volumes.append(f"{archive.root}:{archive.root}")
    volumes.extend(f"{mount.host}:{mount.container}:ro" for mount in answers.brain_tools_mounts)
    if answers.staging is not None:
        # Read-write: a file that was imported is moved to done/, a refused one to failed/.
        volumes.append(f"{answers.staging.host_dir}:{STAGING_IN_CONTAINER}")
    return volumes


def instance_credentials(registry: Registry) -> dict[str, str]:
    """The secrets the instance tools read, mapped to the first tool that named each one."""
    credentials: dict[str, str] = {}
    instance = registry.instance_tools()
    for spec in registry.specs():
        if spec.name not in instance:
            continue
        for name in spec.credentials:
            credentials.setdefault(name, spec.name)
    return credentials


def mail_rules_mount(answers: Answers) -> list[str]:
    """The host filter file, mounted where the sidecar expects it, or nothing."""
    path = mail_rules_path(answers)
    return [f"{path}:{MAIL_RULES_IN_CONTAINER}:ro"] if path is not None else []


def tts_image(answers: Answers) -> str:
    """The image that synthesises the audio: the one the answers name, else the product default."""
    if answers.tools.tts is not None and answers.tools.tts.image:
        return answers.tools.tts.image
    return TTS_IMAGE


def mail_rules_path(answers: Answers) -> Path | None:
    """The filter the mail sidecar reads, or None when this brain has no mail section."""
    if answers.tools.mail is None:
        return None
    return answers.tools.mail.rules_file


def optional_secrets(answers: Answers) -> list[str]:
    """The secret files that exist only when their tool group is on."""
    names = [CALENDAR_TOKEN] if answers.tools.calendar is not None else []
    if answers.vault_remote is not None:
        names.append(VAULT_REMOTE_TOKEN)
    return names


def all_secrets(answers: Answers, registry: Registry) -> list[str]:
    """Every secret brain_tools reads: the calendar token and the ones the instance tools declared."""
    names = optional_secrets(answers)
    for name in instance_credentials(registry):
        if name not in names:
            names.append(name)
    return names


def instance_secrets(answers: Answers, registry: Registry) -> list[str]:
    """The secrets brain_tools reads only because an instance tool declared them."""
    optional = optional_secrets(answers)
    return [name for name in all_secrets(answers, registry) if name not in optional]


def template_values(answers: Answers, registry: Registry, generated: Mapping[str, str]) -> dict[str, Any]:
    """Every placeholder the deploy and Hermes templates use, and nothing else."""
    schema = vault_schema(answers.language)
    return {
        "instance_name": answers.instance_name,
        "vault_dir": str(answers.vault_dir),
        # The renderer knows substitution and one loop, so an optional line is a list with zero or
        # one entry rather than a conditional block.
        "vault_group_gids": [str(answers.vault_group_gid)] if answers.vault_group_gid else [],
        "media_dir": str(answers.media_dir),
        "terminal_cwd": "/workspace",
        "hermes_image": "second-brain/hermes:0.1.0",
        "terminal_image": "second-brain/terminal:0.1.0",
        "brain_image": answers.brain_image or "second-brain/brain:0.1.0",
        "egress_image": "ubuntu/squid:6.6-24.04_edge",
        "searxng_image": SEARXNG_IMAGE,
        "model_upstream": model_upstream(answers),
        "model_gate_url": "http://gate:8080/v1",
        "model_name": answers.model_name,
        "model_context_length": answers.model_context_length,
        "timezone": answers.timezone,
        "docker_gid": docker_gid(answers),
        "brain_tools_extra_volumes": brain_tools_extra_volumes(answers),
        "optional_secrets": optional_secrets(answers),
        "instance_secrets": instance_secrets(answers, registry),
        "brain_tools_secrets": ["tools_token", "outbox_secret", *all_secrets(answers, registry)],
        "skills_dir": "/data/skills",
        "terminal_ssh_key": str(SECRETS_IN_CONTAINER / "terminal_ssh_key"),
        "brain_tools_url": "http://brain-tools:8765/mcp",
        "brain_tools_outbox_url": "http://brain-tools:8765/outbox",
        "egress_proxy_url": "http://egress:3128",
        "owner_telegram_id": answers.owner_id,
        # The owner's id on whichever platform this brain answers on: the webhook routes and the
        # admin scopes are the same list whether that is Telegram or Slack.
        "owner_id": answers.owner_id,
        "owner_slack_id": answers.owner_id,
        "slack_home_channel": answers.slack.home_channel if answers.slack is not None else "",
        "owner_name": answers.owner_name,
        "language": answers.language_name,
        "language_code": answers.language,
        "languages": answers.languages,
        "channel": answers.channel,
        # A template decides between the Telegram block and the Slack one with this flag.
        "is_slack": answers.slack is not None,
        # The long-audio worker and the mail sidecar are services of their own, so the compose file
        # decides whether to render them and the images they run.
        "has_tts": answers.tools.tts is not None,
        "tts_image": tts_image(answers),
        "has_mail": answers.tools.mail is not None,
        # The filter file, when the answers name one: a list with zero or one entry, because the
        # renderer has no nested conditional block.
        "mail_rules_mount": mail_rules_mount(answers),
        # The skills this instance schedules by name, so the Hermes config gets a route for each.
        "scheduled_skills": sorted(answers.schedule.skills),
        "deliver_platform": answers.channel.lower(),
        "example_note_path": f"{schema.folder('knowledge')}/example.md",
        "egress_hosts": egress_hosts(answers, registry),
        "searxng_upstream_hosts": " ".join(SEARXNG_UPSTREAM_HOSTS),
        "searxng_secret_key": generated[SEARXNG_KEY],
    }


def render_deploy(answers: Answers, registry: Registry, generated: Mapping[str, str]) -> dict[str, str]:
    """The deploy files, keyed by their path inside the instance directory."""
    values = template_values(answers, registry, generated)
    deploy = TEMPLATES / "deploy"
    compose = render_template(deploy / "compose.yaml.j2", values)
    files = {
        "config/squid.conf": render_template(deploy / "squid.conf.j2", values),
        "config/searxng/settings.yml": render_template(deploy / "searxng" / "settings.yml.j2", values),
    }
    limits = answers.stack_limits
    if limits is None:
        return {"compose.yaml": compose, **files}
    return {
        "compose.yaml": _under_slice(compose, limits.slice),
        f"systemd/{limits.slice}": _slice_unit(answers.instance_name, limits),
        **files,
    }


def _under_slice(compose: str, slice_name: str) -> str:
    """Put every service under the slice, by a text insertion so the comments survive."""
    return compose.replace("\n    mem_limit:", f"\n    cgroup_parent: {slice_name}\n    mem_limit:")


def _slice_unit(instance_name: str, limits: StackLimitsAnswers) -> str:
    return (
        "[Unit]\n"
        f"Description=Ceiling for every container of {instance_name}\n"
        "\n"
        "[Slice]\n"
        f"MemoryMax={limits.memory_mb}M\n"
        f"CPUQuota={int(limits.cpus * 100)}%\n"
    )


def render_hermes(answers: Answers, registry: Registry, generated: Mapping[str, str]) -> dict[str, str]:
    values = template_values(answers, registry, generated)
    hermes = TEMPLATES / "hermes"
    return {
        "config/hermes/config.yaml": render_template(hermes / "config.yaml.j2", values),
        "config/hermes/SOUL.md": render_template(hermes / "SOUL.md.j2", values),
        "config/hermes/USER.md": render_template(hermes / "USER.md.j2", values),
    }
