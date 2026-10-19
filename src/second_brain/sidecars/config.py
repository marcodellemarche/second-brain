"""Sidecar configuration: one YAML file for the vault watcher, the call router, the job runner
and the scheduler.

The file says which sidecars this brain runs and which Hermes webhook route each one posts to.
It never holds a secret: `route_secrets` maps a route to the file holding its HMAC secret, and a
field that carries a secret value is refused before anything is read. A route a section uses but
`route_secrets` does not name stops the sidecar at startup, because Hermes would refuse every
event it sends.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path, PurePosixPath
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from second_brain.sidecars.call_router import RouterConfig
from second_brain.sidecars.scheduler import ScheduleConfig
from second_brain.sidecars.webhook import SendEvent, sender_for
from second_brain.tools.config import JobsConfig

CONFIG_ENV = "SIDECARS_CONFIG"
DEFAULT_CONFIG_PATH = "/etc/second-brain/sidecars.yaml"

# A field whose name is one of these holds a secret value, so it may only be a path to a file.
_SECRET_NAMES = frozenset(
    {"token", "access_token", "refresh_token", "client_secret", "secret", "password", "api_key"}
)


class SidecarsConfigError(RuntimeError):
    """A sidecar cannot start without a usable configuration."""


def _check_route(value: str) -> str:
    route = value.strip()
    if not route or "/" in route:
        raise ValueError(f"not a route name: {value!r}")
    return route


class VaultWatcherSection(BaseModel):
    """Watch the vault and report every change it is allowed to report."""

    model_config = ConfigDict(extra="forbid")

    route: str = "ingest"
    quiet_seconds: float = Field(default=20, ge=0)
    ignore: list[str] | None = None
    brain_email: str = Field(default="brain@localhost", min_length=1)
    poll_seconds: float = Field(default=5, gt=0)

    @field_validator("route")
    @classmethod
    def _check_route(cls, value: str) -> str:
        return _check_route(value)


class CallRouterSection(BaseModel):
    """Watch the capture folder and route every new call note."""

    model_config = ConfigDict(extra="forbid")

    route: str = "ingest"
    watch_dir: str = Field(min_length=1)
    rules: RouterConfig = Field(default_factory=RouterConfig)
    poll_seconds: float = Field(default=10, gt=0)

    @field_validator("route")
    @classmethod
    def _check_route(cls, value: str) -> str:
        return _check_route(value)

    @field_validator("watch_dir")
    @classmethod
    def _check_watch_dir(cls, value: str) -> str:
        path = PurePosixPath(value.strip())
        if not value.strip():
            raise ValueError("watch_dir must not be empty")
        if path.is_absolute():
            raise ValueError(f"watch_dir must be relative to the vault: {value!r}")
        if ".." in path.parts:
            raise ValueError(f"watch_dir must not leave the vault: {value!r}")
        return str(path)

    def resolved_watch_dir(self, vault_dir: Path) -> Path:
        return vault_dir / self.watch_dir


class JobRunnerSection(BaseModel):
    """Run the queued background jobs, one at a time by default."""

    model_config = ConfigDict(extra="forbid")

    jobs: JobsConfig
    max_concurrent: int = Field(default=1, ge=1)
    dispatch_timeout: float = Field(default=3600, gt=0)

    def routes(self) -> set[str]:
        return {kind.route for kind in self.jobs.kinds.values()}


class SchedulerSection(BaseModel):
    """Fire the scheduled tasks at their local times."""

    model_config = ConfigDict(extra="forbid")

    schedule: ScheduleConfig
    state_file: Path
    poll_seconds: float = Field(default=30, gt=0)


class SidecarsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    vault_dir: Path
    webhook_base_url: str = Field(min_length=1)
    route_secrets: dict[str, Path] = Field(min_length=1)
    vault_watcher: VaultWatcherSection | None = None
    call_router: CallRouterSection | None = None
    job_runner: JobRunnerSection | None = None
    scheduler: SchedulerSection | None = None

    @model_validator(mode="before")
    @classmethod
    def _no_inline_secrets(cls, raw: Any) -> Any:
        """Refuse a secret value anywhere in the file: only a path to a file is allowed."""
        if isinstance(raw, Mapping):
            _reject_secret_fields(raw, "config")
        return raw

    @field_validator("webhook_base_url")
    @classmethod
    def _check_base_url(cls, value: str) -> str:
        base = value.strip().rstrip("/")
        if not base.startswith(("http://", "https://")):
            raise ValueError(f"webhook_base_url must be an http(s) URL: {value!r}")
        return base

    @field_validator("route_secrets")
    @classmethod
    def _check_route_secrets(cls, value: dict[str, Path]) -> dict[str, Path]:
        return {_check_route(route): Path(path) for route, path in value.items()}

    @model_validator(mode="after")
    def _check_routes_have_secrets(self) -> SidecarsConfig:
        for route, used_by in self._used_routes():
            if route not in self.route_secrets:
                raise ValueError(f"route {route!r} used by {used_by} has no entry in route_secrets")
        return self

    def _used_routes(self) -> list[tuple[str, str]]:
        used: list[tuple[str, str]] = []
        if self.vault_watcher is not None:
            used.append((self.vault_watcher.route, "vault_watcher"))
        if self.call_router is not None:
            used.append((self.call_router.route, "call_router"))
        if self.job_runner is not None:
            used.extend((route, "job_runner") for route in sorted(self.job_runner.routes()))
        if self.scheduler is not None:
            used.extend(
                (entry.route, f"scheduler entry {entry.name!r}") for entry in self.scheduler.schedule.entries
            )
        return used

    @classmethod
    def load(cls, environ: Mapping[str, str] | None = None) -> SidecarsConfig:
        env = os.environ if environ is None else environ
        path = env.get(CONFIG_ENV, "").strip() or DEFAULT_CONFIG_PATH
        return cls.from_yaml(path)

    @classmethod
    def from_yaml(cls, path: str | Path) -> SidecarsConfig:
        source = Path(path)
        try:
            raw = yaml.safe_load(source.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as exc:
            raise SidecarsConfigError(f"cannot read sidecars config {source}: {exc}") from exc
        if raw is None:
            raw = {}
        if not isinstance(raw, dict):
            raise SidecarsConfigError(f"sidecars config must be a mapping: {source}")
        try:
            return cls.model_validate(raw)
        except ValueError as exc:
            raise SidecarsConfigError(f"invalid sidecars config {source}: {exc}") from exc

    def sender(self, route: str) -> SendEvent:
        """The signed sender for one route, or a clear error when the route has no secret file."""
        try:
            secret_file = self.route_secrets[route]
        except KeyError:
            raise SidecarsConfigError(f"no secret file for route {route!r}") from None
        return sender_for(self.webhook_base_url, route, secret_file)

    def require(self, section_name: str) -> Any:
        """One section, or an error saying the sidecar is not configured at all."""
        section = getattr(self, section_name, None)
        if section is None:
            raise SidecarsConfigError(f"the sidecars config has no {section_name} section")
        return section


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


__all__ = [
    "CONFIG_ENV",
    "DEFAULT_CONFIG_PATH",
    "CallRouterSection",
    "JobRunnerSection",
    "SchedulerSection",
    "SidecarsConfig",
    "SidecarsConfigError",
    "VaultWatcherSection",
]
