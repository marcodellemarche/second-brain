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
from second_brain.cli.secrets import SEARXNG_KEY, TOOLS_TOKEN
from second_brain.core.schema import VaultSchema, load_schema
from second_brain.guard.config import GuardConfig
from second_brain.tools.config import (
    CalendarToolsConfig,
    JobsConfig,
    ToolsConfig,
    WeatherConfig,
    build_registry,
)
from second_brain.tools.export import export_guard_classes
from second_brain.tools.jobs import JobKind
from second_brain.tools.registry import Registry
from second_brain.tools.vault import VaultConfig

VAULT_IN_CONTAINER = Path("/vault")
DATA_IN_CONTAINER = Path("/data")
SECRETS_IN_CONTAINER = Path("/run/secrets")
TEMPLATES = Path(__file__).resolve().parents[3] / "templates"

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
    jobs = None
    if tools.jobs:
        jobs = JobsConfig(
            store_path=DATA_IN_CONTAINER / "jobs.sqlite3",
            kinds={
                name: JobKind(route=kind.route, description=kind.description, web=kind.web)
                for name, kind in answers.job_kinds.items()
            },
        )
    return ToolsConfig(
        vault=VaultConfig(root=VAULT_IN_CONTAINER),
        token_file=SECRETS_IN_CONTAINER / TOOLS_TOKEN,
        calendar=calendar,
        weather=WeatherConfig(enabled=True) if tools.weather else None,
        links=answers.link_config(),
        archive=answers.archive_config,
        jobs=jobs,
        notify=answers.notify_config(DATA_IN_CONTAINER),
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
        "searxng_image": "searxng/searxng:2025.1.1",
        "model_upstream": answers.model_endpoint,
        "model_gate_url": "http://gate:8080/v1",
        "model_name": answers.model_name,
        "model_context_length": answers.model_context_length,
        "timezone": answers.timezone,
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
