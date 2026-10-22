"""Sidecars an instance package adds next to the product ones.

An instance repo registers a factory under the `second_brain.sidecars` entry point group, with the
same name as its tools section. `sb init` calls it with that section and a `SidecarContext`, and
renders one compose service and one settings file per `SidecarSpec` it returns. The spec says
everything the stack has to open for the sidecar: hosts, secrets, volumes. Anything it does not
declare, it does not get.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from importlib.metadata import entry_points
from pathlib import Path, PurePosixPath
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from second_brain.tools.registry import ExtensionError, ExtensionSettings

log = logging.getLogger(__name__)

ENTRY_POINT_GROUP = "second_brain.sidecars"
CONFIG_DIR_IN_CONTAINER = PurePosixPath("/etc/second-brain")
# The services of the product stack: a sidecar with one of these names would replace it.
PRODUCT_SERVICES = frozenset(
    {
        "hermes",
        "terminal",
        "brain-tools",
        "gate",
        "watchdog",
        "vault-watcher",
        "call-router",
        "job-runner",
        "scheduler",
        "egress",
        "searxng",
    }
)
# Mounted by the product with its own meaning, so a sidecar file cannot land there.
RESERVED_PATHS = (
    PurePosixPath("/vault"),
    PurePosixPath("/data"),
    PurePosixPath("/run/secrets"),
    CONFIG_DIR_IN_CONTAINER,
)
_NAME = re.compile(r"[a-z][a-z0-9-]{0,30}")
_SECRET = re.compile(r"[a-z][a-z0-9_]{0,63}")
_HOST = re.compile(r"[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+(:[0-9]{1,5})?")


@dataclass(frozen=True, slots=True)
class SidecarContext:
    """What a sidecar factory needs to know about the stack it runs in: the containers' own paths."""

    timezone: str
    media_dir: Path
    data_dir: Path = Path("/data")
    vault_dir: Path = Path("/vault")
    secrets_dir: Path = Path("/run/secrets")
    webhook_url: str = "http://hermes:8644/webhooks"
    webhook_secret_file: Path = Path("/run/secrets/webhook_ingest")


class SidecarSpec(BaseModel):
    """One service of an instance. It runs the brain image unless it names another one."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(description="The compose service name, also the name of its settings file.")
    command: list[str] = Field(min_length=1)
    # Written to `config/<name>.json` and mounted read-only; its path is in `SIDECAR_CONFIG`.
    config: dict[str, Any] = Field(default_factory=dict)
    image: str | None = None
    # `name` or `name:port`. Hosts put the sidecar on the egress network, behind the proxy.
    hosts: list[str] = Field(default_factory=list)
    credentials: list[str] = Field(default_factory=list)
    # The HMAC secret of the ingest webhook, for a sidecar that wakes the brain up.
    webhook: bool = False
    # The data volume brain_tools also mounts, for a queue or a staging folder the two share.
    data: bool = False
    # The media folder, read-write, for a sidecar that leaves files the chat attaches.
    media: bool = False
    # Container path to host path, mounted read-only.
    files: dict[str, Path] = Field(default_factory=dict)
    cpus: float = Field(default=0.25, gt=0, le=8)
    mem_limit: str = Field(default="256m", pattern=r"^[1-9][0-9]*[mg]$")
    pids_limit: int = Field(default=128, ge=16, le=4096)

    @field_validator("name")
    @classmethod
    def _service_name(cls, value: str) -> str:
        if not _NAME.fullmatch(value):
            raise ValueError(f"not a service name: {value!r}")
        if value in PRODUCT_SERVICES:
            raise ValueError(f"{value!r} is a service of the product")
        return value

    @field_validator("command")
    @classmethod
    def _arguments(cls, value: list[str]) -> list[str]:
        for argument in value:
            if not argument or not argument.isascii() or not argument.isprintable():
                raise ValueError(f"not a command argument: {argument!r}")
        return value

    @field_validator("image")
    @classmethod
    def _image_reference(cls, value: str | None) -> str | None:
        if value is not None and (not value.strip() or any(c.isspace() for c in value)):
            raise ValueError(f"not an image reference: {value!r}")
        return value

    @field_validator("hosts")
    @classmethod
    def _host_names(cls, value: list[str]) -> list[str]:
        for host in value:
            if not _HOST.fullmatch(host):
                raise ValueError(f"not a host: {host!r}")
        return value

    @field_validator("credentials")
    @classmethod
    def _secret_names(cls, value: list[str]) -> list[str]:
        for name in value:
            if not _SECRET.fullmatch(name):
                raise ValueError(f"not a secret name: {name!r}")
        return value

    @field_validator("files")
    @classmethod
    def _mounts(cls, value: dict[str, Path]) -> dict[str, Path]:
        for target, source in value.items():
            path = PurePosixPath(target)
            if not path.is_absolute() or ".." in path.parts or ":" in target:
                raise ValueError(f"not an absolute container path: {target!r}")
            if any(path == reserved or reserved in path.parents for reserved in RESERVED_PATHS):
                raise ValueError(f"{target} is a path the product mounts")
            if not source.is_absolute() or ":" in str(source):
                raise ValueError(f"not an absolute host path: {source}")
        return value

    @property
    def config_path(self) -> PurePosixPath:
        return CONFIG_DIR_IN_CONTAINER / f"{self.name}.json"

    @property
    def health_marker(self) -> str:
        """What the process' cmdline carries: the module after `-m`, else the program."""
        if "-m" in self.command[:-1]:
            return self.command[self.command.index("-m") + 1]
        return PurePosixPath(self.command[0]).name


SidecarFactory = Callable[[ExtensionSettings, SidecarContext], Iterable[SidecarSpec]]


def load_sidecars(
    sections: Mapping[str, Mapping[str, Any]],
    context: SidecarContext,
    *,
    timezone: str,
    group: str = ENTRY_POINT_GROUP,
) -> list[SidecarSpec]:
    """Every sidecar the installed instance packages declare for these settings.

    A factory gets its own section of `tools.extensions` and returns nothing when that section
    does not ask for a sidecar. A failure is raised, never skipped: a brain must not start without
    a service its tools rely on.
    """
    specs: list[SidecarSpec] = []
    seen: dict[str, str] = {}
    for entry_point in entry_points(group=group):
        settings = ExtensionSettings(
            entry_point.name, sections.get(entry_point.name, {}), context.secrets_dir, timezone
        )
        try:
            factory = entry_point.load()
            built = list(factory(settings, context))
        except Exception:
            log.exception("loading sidecars from entry point %r failed", entry_point.name)
            raise
        for spec in built:
            if not isinstance(spec, SidecarSpec):
                raise ExtensionError(f"entry point {entry_point.name!r} returned {type(spec).__name__}")
            if spec.name in seen:
                raise ExtensionError(
                    f"sidecar {spec.name!r} declared by {seen[spec.name]} and {entry_point.name}"
                )
            seen[spec.name] = entry_point.name
            specs.append(spec)
    return specs
