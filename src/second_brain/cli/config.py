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
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from second_brain.cli.answers import Answers
from second_brain.cli.render import render_template
from second_brain.cli.secrets import (
    CALENDAR_TOKEN,
    OUTBOX_SECRET,
    SEARXNG_KEY,
    TOOLS_TOKEN,
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
from second_brain.tools.vault import VaultConfig

VAULT_IN_CONTAINER = Path("/vault")
DATA_IN_CONTAINER = Path("/data")
SECRETS_IN_CONTAINER = Path("/run/secrets")
TEMPLATES = Path(__file__).resolve().parents[3] / "templates"
HERMES_WEBHOOK_BASE_URL = "http://hermes:8644/webhooks"
SCHEDULE_STATE_FILE = DATA_IN_CONTAINER / "scheduler.json"
DOCKER_SOCKET = Path("/var/run/docker.sock")
SEARXNG_IMAGE = "searxng/searxng:2026.10.19-81371861f"

SCHEDULE_ROUTES: dict[str, str] = {
    "digest": "scheduled-digest",
    "lint": "scheduled-lint",
    "brief": "scheduled-brief",
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


def tools_config(answers: Answers) -> ToolsConfig:
    """The tools config this instance runs with. Secrets are paths, never values."""
    tools = answers.tools
    calendar = None
    if tools.calendar is not None:
        calendar = CalendarToolsConfig(
            assistant_calendar_id=tools.calendar.assistant_calendar_id,
            main_calendar_id=tools.calendar.main_calendar_id,
            timezone=answers.timezone,
            token_file=SECRETS_IN_CONTAINER / "calendar_token",
        )
    return ToolsConfig(
        vault=VaultConfig(root=VAULT_IN_CONTAINER),
        token_file=SECRETS_IN_CONTAINER / TOOLS_TOKEN,
        listen=Listen(host="0.0.0.0", port=8765),
        calendar=calendar,
        weather=WeatherConfig(enabled=True) if tools.weather else None,
        links=answers.link_config(),
        archive=answers.archive_config,
        jobs=jobs_config(answers),
        notify=answers.notify_config(DATA_IN_CONTAINER),
        outbox=outbox_config(answers),
    )


def export_registry(config: ToolsConfig) -> Registry:
    """The registry to export the guard table from, built without touching the real paths.

    The tools' specs are what the guard needs, but building the registry also opens the job and
    notification stores and a `Vault`, which want `/vault` and `/data` to exist. On the host they
    do not, so the paths are redirected into a throwaway directory: the specs are the same ones
    the containers will serve.
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
        return build_registry(redirected, load_entry_points=False)


def git_init(root: Path) -> None:
    """A vault is a git repository: brain_tools commits every write and undo walks that history."""
    root.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(root)], capture_output=True, text=True, check=False)


def guard_config(answers: Answers, registry: Registry) -> GuardConfig:
    """The guard config: the tool table exported from the registry, plus the vault paths."""
    schema = vault_schema(answers.language)
    return GuardConfig(
        vault_root=str(VAULT_IN_CONTAINER),
        guard_dir="/guard",
        hermes_config_path="/etc/hermes/config.yaml",
        private_prefixes=(schema.folder("_brain"), schema.folder("documents")),
        untrusted_prefixes=(schema.folder("raw"),),
        media_dir=str(answers.media_dir),
        mcp_tools=export_guard_classes(registry),
        admin_senders=(answers.owner_id,),
    )


def capture_dir(answers: Answers) -> str:
    """The vault-relative folder the call router watches: the inbox calls folder unless set."""
    if answers.calls.capture_dir is not None:
        return answers.calls.capture_dir
    return f"{vault_schema(answers.language).folder(Zone.INBOX)}/{Zone.CALLS.value}"


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
    return entries


def egress_hosts(answers: Answers, registry: Registry) -> list[str]:
    """Every host the egress proxy allows: the model, the chat API, and the enabled tools."""
    hosts: list[str] = [answers.endpoint_host]
    hosts += list(SLACK_HOSTS if answers.slack is not None else TELEGRAM_HOSTS)
    for spec in registry.specs():
        hosts += list(spec.hosts)
    ordered: dict[str, None] = {}
    for host in hosts:
        ordered.setdefault(host, None)
    return list(ordered)


def docker_gid(answers: Answers) -> int:
    """The group that owns the docker socket: the answer, else this machine's, else the root group.

    The watchdog talks to the socket as the brain image's group, so compose has to give it a
    group id the socket accepts. `sb init` runs on the host, where the socket can be looked at.
    """
    if answers.docker_gid is not None:
        return answers.docker_gid
    try:
        return DOCKER_SOCKET.stat().st_gid
    except OSError:
        return 0


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
    root has to be there under its own name rather than under `/vault`.
    """
    archive = answers.archive_config
    if archive is None:
        return []
    return [f"{archive.root}:{archive.root}"]


def optional_secrets(answers: Answers) -> list[str]:
    """The secret files that exist only when their tool group is on."""
    return [CALENDAR_TOKEN] if answers.tools.calendar is not None else []


def template_values(answers: Answers, registry: Registry, generated: Mapping[str, str]) -> dict[str, Any]:
    """Every placeholder the deploy and Hermes templates use, and nothing else."""
    schema = vault_schema(answers.language)
    return {
        "instance_name": answers.instance_name,
        "vault_dir": str(answers.vault_dir),
        "media_dir": str(answers.media_dir),
        "terminal_cwd": "/workspace",
        "hermes_image": "second-brain/hermes:0.1.0",
        "terminal_image": "second-brain/terminal:0.1.0",
        "brain_image": "second-brain/brain:0.1.0",
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
        "brain_tools_secrets": ["tools_token", "outbox_secret", *optional_secrets(answers)],
        "skills_dir": "/data/skills",
        "terminal_ssh_key": str(SECRETS_IN_CONTAINER / "terminal_ssh_key"),
        "brain_tools_url": "http://brain-tools:8765/mcp",
        "brain_tools_outbox_url": "http://brain-tools:8765/outbox",
        "egress_proxy_url": "http://egress:3128",
        "owner_telegram_id": answers.owner_id,
        "owner_slack_id": answers.owner_id,
        "slack_home_channel": answers.slack.home_channel if answers.slack is not None else "",
        "owner_name": answers.owner_name,
        "language": answers.language_name,
        "languages": answers.languages,
        "channel": answers.channel,
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
    return {
        "compose.yaml": render_template(deploy / "compose.yaml.j2", values),
        "config/squid.conf": render_template(deploy / "squid.conf.j2", values),
        "config/searxng/settings.yml": render_template(deploy / "searxng" / "settings.yml.j2", values),
    }


def render_hermes(answers: Answers, registry: Registry, generated: Mapping[str, str]) -> dict[str, str]:
    values = template_values(answers, registry, generated)
    hermes = TEMPLATES / "hermes"
    return {
        "config/hermes/config.yaml": render_template(hermes / "config.yaml.j2", values),
        "config/hermes/SOUL.md": render_template(hermes / "SOUL.md.j2", values),
        "config/hermes/USER.md": render_template(hermes / "USER.md.j2", values),
    }
