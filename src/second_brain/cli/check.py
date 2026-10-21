"""`sb check`: the instance `sb init` should have produced, and the deployment it should be running.

Static checks read the rendered files and need no Docker. They are what `sb check` runs before a
deployment exists, and what catches a hand-edited config afterwards. Live checks ask Docker Compose
what is actually running, and are skipped when Docker is not there.

Every check is one line: PASS or FAIL with the reason, plus SKIP for what cannot be decided here.
A check that cannot read the file it needs is a FAIL, never a pass: the point is to hear about a
config that is not what it should be, and a missing file is exactly that.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Any

import yaml

from second_brain.cli.config import export_registry
from second_brain.cli.init import BRAIN_UID, HERMES_UID, TERMINAL_UID
from second_brain.cli.secrets import GENERATED, SECRET_MODE, SECRETS_DIR, TERMINAL_SSH_KEY
from second_brain.gate.watchdog import DEFAULT_MAX_AGE
from second_brain.guard.config import GuardConfig, GuardConfigError
from second_brain.sidecars.config import SidecarsConfig, SidecarsConfigError
from second_brain.tools.config import ToolsConfig, ToolsConfigError
from second_brain.tools.export import export_guard_classes

PASS = "pass"
FAIL = "fail"
SKIP = "skip"

HERMES = "hermes"
WATCHDOG = "watchdog"
BRAIN_TOOLS = "brain-tools"
VAULT_TARGET = "/vault"
DOCKER_SOCKET = "docker.sock"

# What `sb init` renders, whatever the answers say. The secrets are added per instance, because
# which chat token exists depends on the channel.
RENDERED_FILES: tuple[str, ...] = (
    "README.md",
    "compose.yaml",
    "config/brain-tools.yaml",
    "config/brain-guard.yaml",
    "config/squid.conf",
    "config/searxng/settings.yml",
    "config/sidecars.yaml",
    "config/hermes/config.yaml",
    "config/hermes/SOUL.md",
    "config/hermes/USER.md",
)

# The registries a command must never reach: this is what makes `pip install` fail even when a
# human approved it. The template denies them by name, and the check refuses a squid.conf that
# stopped denying any of them, or that denies them after an allow line.
PACKAGE_REGISTRIES: tuple[str, ...] = (
    "pypi.org",
    "files.pythonhosted.org",
    "registry.npmjs.org",
    "crates.io",
    "static.crates.io",
    "deb.debian.org",
    "security.debian.org",
    "dl-cdn.alpinelinux.org",
)

PLACEHOLDER_MARKERS: tuple[bytes, ...] = (b"{{", b"{%", b"{#")
# The vault is a git repository from its first commit, and a git object or index can contain the
# marker bytes by chance. Git metadata is not rendered from a template, so it is not scanned.
PLACEHOLDER_SKIP_DIRS = frozenset({".git", ".obsidian", ".trash", "__pycache__"})
# A template is text. A PDF, an image or an audio file can hold the marker bytes by chance, and
# the archive is full of them.
PLACEHOLDER_TEXT_SUFFIXES = frozenset(
    {
        "",
        ".cfg",
        ".conf",
        ".env",
        ".ini",
        ".json",
        ".md",
        ".py",
        ".sh",
        ".sql",
        ".toml",
        ".txt",
        ".yaml",
        ".yml",
    }
)

# The routes the scheduler is allowed to post to, and the only one that may deliver its reply into
# the chat: a run that reaches the web or mirrors its reply quotes untrusted content into it.
SCHEDULED_ROUTES: tuple[str, ...] = (
    "scheduled-digest",
    "scheduled-lint",
    "scheduled-brief",
    "scheduled-triage",
)
DELIVERING_ROUTE = "scheduled-digest"
NO_DELIVERY: tuple[Any, ...] = (None, False, "", "log", "none")

HEARTBEAT_MAX_AGE = DEFAULT_MAX_AGE

# A listen address only the container itself can reach. Hermes talks to brain_tools over the
# internal network, so a loopback host makes every tool call fail.
LOOPBACK_HOSTS: tuple[str, ...] = ("localhost", "::1", "0:0:0:0:0:0:0:1")

# The uids that read the secrets inside the containers, so an operator cannot read them from the
# host. The brain image services run as BRAIN_UID, Hermes as HERMES_UID.
CONTAINER_UIDS: tuple[int, ...] = (HERMES_UID, BRAIN_UID, TERMINAL_UID)

# A write into the read-only vault must fail; the probe removes its file if it somehow succeeds.
VAULT_WRITE_PROBE = (
    "import os, sys\n"
    "path = '/vault/.sb-check'\n"
    "try:\n"
    "    open(path, 'w').close()\n"
    "except OSError:\n"
    "    sys.exit(3)\n"
    "os.remove(path)\n"
    "sys.exit(0)\n"
)
SOCKET_PROBE = "import os, sys\nsys.exit(3 if os.path.exists('/var/run/docker.sock') else 0)\n"
# The freshness rule is the one the watchdog enforces, imported from the package rather than
# restated here, so `sb check` and the watchdog cannot disagree about what fresh means.
HEARTBEAT_PROBE = """
import sys, time
from second_brain.guard.heartbeat import is_fresh, read_heartbeat

path = "/guard/heartbeat.json"
max_age = float(sys.argv[1])
beat = read_heartbeat(path)
if beat is None:
    print(f"no readable heartbeat at {path}")
    sys.exit(3)
if not is_fresh(path, max_age, time.time()):
    print(f"the heartbeat is stale, ts={beat.get('ts')}")
    sys.exit(3)
print(f"the heartbeat is fresh, ts={beat.get('ts')}")
"""
# The watchdog reads the socket itself, through the group compose adds to its service: this is
# the only probe that says whether `group_add` actually works on the host.
WATCHDOG_DOCKER_PROBE = """
import os, sys
from second_brain.gate.watchdog import DockerError, docker_started_at

socket_path = os.environ.get("WATCHDOG_DOCKER_SOCKET", "/var/run/docker.sock")
container = os.environ["WATCHDOG_CONTAINER"]
try:
    started = docker_started_at(socket_path, container)
except (PermissionError, OSError, DockerError) as exc:
    print(f"the watchdog cannot reach Docker: check group_add / docker_gid: {exc}")
    sys.exit(3)
if started is None:
    print("hermes is not running")
    sys.exit(3)
print(f"hermes started at {started}")
"""

Runner = Callable[[Sequence[str]], "subprocess.CompletedProcess[str]"]


@dataclass(frozen=True, slots=True)
class CheckResult:
    """One line of the report: what was checked, whether it holds, and why."""

    name: str
    status: str
    reason: str


def _pass(name: str, reason: str) -> CheckResult:
    return CheckResult(name, PASS, reason)


def _fail(name: str, reason: str) -> CheckResult:
    return CheckResult(name, FAIL, reason)


def _skip(name: str, reason: str) -> CheckResult:
    return CheckResult(name, SKIP, reason)


def _run_checks(checks: Sequence[tuple[str, Callable[[], CheckResult]]]) -> list[CheckResult]:
    """Run every check, turning an unexpected exception into a FAIL for that check alone."""
    results: list[CheckResult] = []
    for name, check in checks:
        try:
            results.append(check())
        except Exception as exc:  # a check that crashes must not take the whole report with it
            results.append(_fail(name, f"crashed: {exc}"))
    return results


@dataclass(slots=True)
class _Instance:
    """The instance as the checks read it: the root, and the three configs already parsed."""

    root: Path
    hermes: dict[str, Any] | None
    hermes_error: str | None
    guard: GuardConfig | None
    guard_error: str | None
    tools: ToolsConfig | None
    tools_error: str | None
    sidecars: SidecarsConfig | None
    sidecars_error: str | None
    compose: dict[str, Any] | None
    compose_error: str | None


def static_checks(instance: Path) -> list[CheckResult]:
    """Everything that can be decided from the instance directory alone, in report order."""
    ctx = _load(instance.resolve())
    return _run_checks(
        [
            ("files", partial(_files_check, ctx)),
            ("placeholders", partial(_placeholders_check, ctx)),
            ("secrets", partial(_secrets_check, ctx)),
            ("compose.vault-read-only", partial(_compose_vault_check, ctx)),
            ("compose.docker-socket", partial(_compose_socket_check, ctx)),
            ("compose.no-published-ports", partial(_compose_ports_check, ctx)),
            ("compose.internal-network", partial(_compose_network_check, ctx)),
            ("compose.hermes-proxy", partial(_compose_proxy_check, ctx)),
            ("compose.slice", partial(_compose_slice_check, ctx)),
            ("hermes.approvals", partial(_approvals_check, ctx)),
            ("hermes.terminal", partial(_terminal_check, ctx)),
            ("hermes.owner", partial(_owner_check, ctx)),
            ("hermes.plugin", partial(_plugin_check, ctx)),
            ("hermes.research-routes", partial(_research_routes_check, ctx)),
            ("sidecars_config", partial(_sidecars_config_check, ctx)),
            ("sidecar_routes", partial(_sidecar_routes_check, ctx)),
            ("team_domains", partial(_team_domains_check, ctx)),
            ("scheduled_routes", partial(_scheduled_routes_check, ctx)),
            ("tools.listen", partial(_tools_listen_check, ctx)),
            ("guard.tools", partial(_guard_tools_check, ctx)),
            ("squid.registries", partial(_squid_check, ctx)),
        ]
    )


def live_checks(
    instance: Path, *, runner: Runner | None = None, docker: str | None = None
) -> list[CheckResult]:
    """What only a running deployment can answer. Skipped, not failed, without Docker."""
    binary = docker if docker is not None else shutil.which("docker")
    if not binary:
        return [_skip("live", "docker is not available, nothing live was checked")]
    run = runner or _run
    root = instance.resolve()
    compose = [binary, "compose", "-f", str(root / "compose.yaml")]
    declared = _declared_services(root)
    return _run_checks(
        [
            ("live.services", partial(_live_services, compose, run, declared)),
            ("live.vault-read-only", partial(_live_vault_read_only, compose, run)),
            ("live.no-docker-socket", partial(_live_no_socket, compose, run)),
            ("live.heartbeat", partial(_live_heartbeat, compose, run)),
            ("live.watchdog-docker", partial(_live_watchdog_docker, compose, run)),
            (
                "live.gate-stale-heartbeat",
                lambda: _skip(
                    "live.gate-stale-heartbeat",
                    "not exercised here, stopping Hermes to test it is too invasive: covered by unit tests",
                ),
            ),
        ]
    )


# --- reading the instance -------------------------------------------------------------------


def _load(root: Path) -> _Instance:
    hermes, hermes_error = _read_yaml(root / "config/hermes/config.yaml")
    guard, guard_error = _read_guard(root / "config/brain-guard.yaml")
    tools, tools_error = _read_tools(root / "config/brain-tools.yaml")
    sidecars, sidecars_error = _read_sidecars(root / "config/sidecars.yaml")
    compose, compose_error = _read_yaml(root / "compose.yaml")
    return _Instance(
        root,
        hermes,
        hermes_error,
        guard,
        guard_error,
        tools,
        tools_error,
        sidecars,
        sidecars_error,
        compose,
        compose_error,
    )


def _read_yaml(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        return None, f"cannot read {path.name}: {exc}"
    if not isinstance(data, dict):
        return None, f"{path.name} is not a mapping"
    return data, None


def _read_guard(path: Path) -> tuple[GuardConfig | None, str | None]:
    try:
        return GuardConfig.from_yaml(path), None
    except GuardConfigError as exc:
        return None, str(exc)


def _read_tools(path: Path) -> tuple[ToolsConfig | None, str | None]:
    try:
        return ToolsConfig.from_yaml(path), None
    except ToolsConfigError as exc:
        return None, str(exc)


def _read_sidecars(path: Path) -> tuple[SidecarsConfig | None, str | None]:
    try:
        return SidecarsConfig.from_yaml(path), None
    except SidecarsConfigError as exc:
        return None, str(exc)


# --- the files and their contents -----------------------------------------------------------


def _files_check(ctx: _Instance) -> CheckResult:
    name = "files"
    # The secrets README lists what this instance has to carry, chat token included: it is written
    # from the plan, so it knows whether this is a Telegram brain or a Slack one.
    to_fill, readme_error = _must_fill(ctx.root / SECRETS_DIR / "README.md")
    expected = list(RENDERED_FILES)
    expected += [f"{SECRETS_DIR}/{secret}" for secret in GENERATED]
    expected += [f"{SECRETS_DIR}/{secret}" for secret in to_fill]
    expected += [f"{SECRETS_DIR}/README.md", f"{SECRETS_DIR}/hermes.env"]
    # The settings of the two services an instance adds, when compose declares them: a brain
    # without the audio worker or the mail sidecar has neither file.
    services = _declared_services(ctx.root)
    if "tts-worker" in services:
        expected.append("config/tts-worker.json")
    if "mail" in services:
        expected.append("config/mail.json")
    missing = [relative for relative in expected if not (ctx.root / relative).is_file()]
    if readme_error is not None:
        return _fail(name, readme_error)
    if missing:
        return _fail(name, f"missing: {', '.join(missing)}")
    return _pass(name, f"{len(expected)} files from `sb init` are there")


def _placeholders_check(ctx: _Instance) -> CheckResult:
    name = "placeholders"
    secrets_dir = ctx.root / SECRETS_DIR
    for path in sorted(ctx.root.rglob("*")):
        # The secrets are generated values, never templates, and after owners.sh they belong to
        # container uids: reading them here would only fail on a deployment.
        if secrets_dir in path.parents:
            continue
        if not path.is_file() or path.name.endswith(".pub") or path.name == TERMINAL_SSH_KEY:
            continue
        if PLACEHOLDER_SKIP_DIRS.intersection(path.relative_to(ctx.root).parts[:-1]):
            continue
        if path.suffix.lower() not in PLACEHOLDER_TEXT_SUFFIXES:
            continue
        try:
            content = path.read_bytes()
        except OSError as exc:
            return _fail(name, f"cannot read {path.relative_to(ctx.root)}: {exc}")
        for marker in PLACEHOLDER_MARKERS:
            if marker in content:
                found = marker.decode()
                return _fail(name, f"{path.relative_to(ctx.root)} still has {found}")
    return _pass(name, "no template placeholder survived rendering")


def _secrets_check(ctx: _Instance) -> CheckResult:
    name = "secrets"
    directory = ctx.root / SECRETS_DIR
    if not directory.is_dir():
        return _fail(name, f"{SECRETS_DIR}/ is missing")
    problems: list[str] = []
    mode = stat.S_IMODE(directory.stat().st_mode)
    if mode != 0o700:
        problems.append(f"{SECRETS_DIR}/ is {oct(mode)}, not 0o700")
    for path in sorted(directory.iterdir()):
        if not path.is_file() or path.name.endswith(".pub"):
            continue
        try:
            info = path.stat()
        except OSError as exc:
            problems.append(f"{path.name}: cannot stat it: {exc}")
            continue
        mode = stat.S_IMODE(info.st_mode)
        if mode != SECRET_MODE:
            problems.append(f"{path.name} is {oct(mode)}, not {oct(SECRET_MODE)}")
        owner = info.st_uid
        if owner != os.geteuid() and owner not in CONTAINER_UIDS:
            problems.append(f"{path.name} is owned by uid {owner}, not by this operator nor a container")
    to_fill, readme_error = _must_fill(directory / "README.md")
    if readme_error is not None:
        problems.append(readme_error)
    for secret in to_fill:
        # Never read the content: on a deployed host the file belongs to a container uid and the
        # operator cannot open it, and only its size decides whether a human filled it in.
        try:
            info = (directory / secret).stat()
        except FileNotFoundError:
            problems.append(f"{secret} is missing")
            continue
        except OSError as exc:
            problems.append(f"{secret}: cannot stat it: {exc}")
            continue
        if not stat.S_ISREG(info.st_mode):
            problems.append(f"{secret} is not a regular file")
        elif info.st_size <= 1:
            problems.append(f"{secret} is still empty")
    if problems:
        return _fail(name, "; ".join(problems))
    return _pass(name, "owner-only, and every secret a human must provide is filled")


def _must_fill(readme: Path) -> tuple[list[str], str | None]:
    """The files the secrets README tells a human to fill in, in its own order."""
    try:
        text = readme.read_text(encoding="utf-8")
    except OSError as exc:
        return [], f"cannot read {SECRETS_DIR}/README.md: {exc}"
    names: list[str] = []
    inside = False
    for line in text.splitlines():
        if line.startswith("#"):
            inside = line.strip().lower() == "## fill these in"
            continue
        if inside and line.startswith("- `"):
            names.append(line[3:].split("`", 1)[0])
    return names, None


# --- compose --------------------------------------------------------------------------------


def _services(ctx: _Instance) -> tuple[dict[str, Any], str | None]:
    if ctx.compose is None:
        return {}, ctx.compose_error or "compose.yaml is not readable"
    services = ctx.compose.get("services")
    if not isinstance(services, dict) or not services:
        return {}, "compose.yaml declares no service"
    return services, None


def _volume_target(volume: Any) -> str | None:
    if isinstance(volume, str):
        parts = volume.split(":")
        return parts[1] if len(parts) >= 2 else None
    if isinstance(volume, dict):
        target = volume.get("target")
        return str(target) if target is not None else None
    return None


def _volume_source(volume: Any) -> str | None:
    if isinstance(volume, str):
        parts = volume.split(":")
        return parts[0] if len(parts) >= 2 else None
    if isinstance(volume, dict):
        source = volume.get("source")
        return str(source) if source is not None else None
    return None


def _volume_read_only(volume: Any) -> bool:
    if isinstance(volume, str):
        parts = volume.split(":")
        return len(parts) >= 3 and "ro" in parts[2].split(",")
    if isinstance(volume, dict):
        return bool(volume.get("read_only"))
    return False


def _volumes(service: Any) -> list[Any]:
    volumes = service.get("volumes") if isinstance(service, dict) else None
    return list(volumes) if isinstance(volumes, list) else []


def _compose_vault_check(ctx: _Instance) -> CheckResult:
    name = "compose.vault-read-only"
    services, error = _services(ctx)
    if error is not None:
        return _fail(name, error)
    problems: list[str] = []
    writer = False
    for service_name, service in services.items():
        for volume in _volumes(service):
            if _volume_target(volume) != VAULT_TARGET:
                continue
            read_only = _volume_read_only(volume)
            if service_name == BRAIN_TOOLS:
                writer = not read_only
                if read_only:
                    problems.append(f"{BRAIN_TOOLS} mounts {VAULT_TARGET} read-only")
            elif not read_only:
                problems.append(f"{service_name} mounts {VAULT_TARGET} read-write")
    if not writer and not problems:
        problems.append(f"{BRAIN_TOOLS} does not mount {VAULT_TARGET}")
    if problems:
        return _fail(name, "; ".join(problems))
    return _pass(name, f"{BRAIN_TOOLS} is the only writer, everyone else mounts it read-only")


def _compose_socket_check(ctx: _Instance) -> CheckResult:
    name = "compose.docker-socket"
    services, error = _services(ctx)
    if error is not None:
        return _fail(name, error)
    problems: list[str] = []
    mounted = False
    for service_name, service in services.items():
        for volume in _volumes(service):
            source = _volume_source(volume) or ""
            target = _volume_target(volume) or ""
            if not source.endswith(DOCKER_SOCKET) and not target.endswith(DOCKER_SOCKET):
                continue
            if service_name != WATCHDOG:
                problems.append(f"{service_name} mounts the docker socket")
            else:
                mounted = True
    if not mounted and not problems:
        problems.append(f"{WATCHDOG} does not mount the docker socket")
    if problems:
        return _fail(name, "; ".join(problems))
    return _pass(name, f"only {WATCHDOG} holds the docker socket")


def _compose_ports_check(ctx: _Instance) -> CheckResult:
    name = "compose.no-published-ports"
    services, error = _services(ctx)
    if error is not None:
        return _fail(name, error)
    published = [
        f"{service_name}: {service.get('ports')}"
        for service_name, service in services.items()
        if service.get("ports")
    ]
    if published:
        return _fail(name, "a port is published: " + "; ".join(published))
    return _pass(name, "nothing is reachable from outside the docker networks")


def _compose_network_check(ctx: _Instance) -> CheckResult:
    name = "compose.internal-network"
    if ctx.compose is None:
        return _fail(name, ctx.compose_error or "compose.yaml is not readable")
    networks = ctx.compose.get("networks")
    internal = networks.get("internal") if isinstance(networks, dict) else None
    if not isinstance(internal, dict) or internal.get("internal") is not True:
        return _fail(name, "the `internal` network is not marked `internal: true`")
    return _pass(name, "the `internal` network has no gateway")


def _compose_proxy_check(ctx: _Instance) -> CheckResult:
    name = "compose.hermes-proxy"
    services, error = _services(ctx)
    if error is not None:
        return _fail(name, error)
    hermes = services.get(HERMES)
    if not isinstance(hermes, dict):
        return _fail(name, f"compose.yaml has no {HERMES} service")
    environment = hermes.get("environment")
    value = None
    if isinstance(environment, dict):
        value = environment.get("HTTPS_PROXY")
    elif isinstance(environment, list):
        prefix = "HTTPS_PROXY="
        value = next((str(item)[len(prefix) :] for item in environment if str(item).startswith(prefix)), None)
    if not value:
        return _fail(name, f"{HERMES} has no HTTPS_PROXY: it would not be forced through egress")
    return _pass(name, f"{HERMES} reaches the internet only through {value}")


def _compose_slice_check(ctx: _Instance) -> CheckResult:
    name = "compose.slice"
    services, error = _services(ctx)
    if error is not None:
        return _fail(name, error)
    parents = {
        service_name: (service.get("cgroup_parent") if isinstance(service, dict) else None)
        for service_name, service in services.items()
    }
    declared = sorted({parent for parent in parents.values() if parent})
    if not declared:
        return _pass(name, "no stack ceiling")
    if len(declared) > 1:
        listed = "; ".join(f"{service_name}: {parent}" for service_name, parent in parents.items() if parent)
        return _fail(name, "services disagree about the slice: " + listed)
    slice_name = declared[0]
    missing = [service_name for service_name, parent in parents.items() if parent != slice_name]
    if missing:
        return _fail(name, f"not under {slice_name}: " + ", ".join(missing))
    if not (ctx.root / "systemd" / slice_name).is_file():
        return _fail(name, f"systemd/{slice_name} is missing, so nothing enforces the ceiling")
    return _pass(name, f"every container runs under {slice_name}, and systemd caps the slice")


# --- the Hermes config ----------------------------------------------------------------------


def _hermes(ctx: _Instance, name: str) -> tuple[dict[str, Any] | None, CheckResult | None]:
    if ctx.hermes is None:
        return None, _fail(name, ctx.hermes_error or "the Hermes config is not readable")
    return ctx.hermes, None


def _approvals_check(ctx: _Instance) -> CheckResult:
    name = "hermes.approvals"
    # The guard's own rule decides, so the check cannot drift from what the plugin enforces at load.
    probe = GuardConfig(
        vault_root=VAULT_TARGET,
        guard_dir="/guard",
        hermes_config_path=str(ctx.root / "config/hermes/config.yaml"),
    )
    try:
        probe.check_approvals()
    except GuardConfigError as exc:
        return _fail(name, str(exc))
    return _pass(name, "approvals.mode is manual, so a human really answered")


def _terminal_check(ctx: _Instance) -> CheckResult:
    name = "hermes.terminal"
    hermes, error = _hermes(ctx, name)
    if error is not None:
        return error
    terminal = hermes.get("terminal")
    backend = terminal.get("backend") if isinstance(terminal, dict) else None
    if backend is None:
        return _fail(name, "terminal.backend is not set, so Hermes falls back to a local shell")
    if backend == "docker":
        return _fail(name, "terminal.backend is docker, which needs the docker socket")
    return _pass(name, f"terminal.backend is {backend}, in its own container")


def _owner_check(ctx: _Instance) -> CheckResult:
    name = "hermes.owner"
    if ctx.guard is None:
        return _fail(name, ctx.guard_error or "the guard config is not readable")
    admins = ctx.guard.admin_senders
    if len(admins) != 1:
        return _fail(name, f"the guard config names {len(admins)} admin senders, not only the owner")
    owner = admins[0]
    hermes, error = _hermes(ctx, name)
    if error is not None:
        return error
    problems = [
        f"{label} is {list(values)}" for label, values in _owner_lists(hermes) if set(values) != {owner}
    ]
    gateway = hermes.get("gateway")
    if isinstance(gateway, dict) and gateway.get("allow_all_users"):
        problems.append("gateway.allow_all_users is on")
    if problems:
        return _fail(name, f"the owner is {owner}, but " + "; ".join(problems))
    return _pass(name, f"{owner} is the only allowed user and the only admin")


def _owner_lists(hermes: dict[str, Any]) -> Iterator[tuple[str, list[str]]]:
    platforms = hermes.get("platforms")
    if not isinstance(platforms, dict):
        return
    for platform, block in platforms.items():
        if not isinstance(block, dict) or block.get("enabled") is not True:
            continue
        extra = block.get("extra") if platform == "webhook" else None
        source = extra if isinstance(extra, dict) else block
        for key in ("allow_from", "group_allow_from", "allow_admin_from", "group_allow_admin_from"):
            values = source.get(key)
            if isinstance(values, list):
                yield f"platforms.{platform}.{key}", [str(value) for value in values]


def _plugin_check(ctx: _Instance) -> CheckResult:
    name = "hermes.plugin"
    hermes, error = _hermes(ctx, name)
    if error is not None:
        return error
    plugins = hermes.get("plugins")
    enabled = plugins.get("enabled") if isinstance(plugins, dict) else None
    if not isinstance(enabled, list) or "brain-guard" not in enabled:
        return _fail(name, f"brain-guard is not in plugins.enabled (found {enabled!r})")
    return _pass(name, "the brain-guard plugin is enabled")


def _research_routes_check(ctx: _Instance) -> CheckResult:
    name = "hermes.research-routes"
    if ctx.tools is None:
        return _fail(name, ctx.tools_error or "the tools config is not readable")
    routes = (
        sorted({kind.route for kind in ctx.tools.jobs.kinds.values() if kind.web})
        if ctx.tools.jobs is not None
        else []
    )
    if not routes:
        return _pass(name, "no job kind reaches the web, so no research route to check")
    hermes, error = _hermes(ctx, name)
    if error is not None:
        return error
    configured = _webhook_routes(hermes)
    problems: list[str] = []
    for route in routes:
        block = configured.get(route)
        if not isinstance(block, dict):
            problems.append(f"{route} is not a webhook route")
            continue
        toolsets = [str(toolset) for toolset in block.get("toolsets") or []]
        reach = sorted(set(toolsets) & {"file", "brain"})
        if reach:
            problems.append(f"{route} has {', '.join(reach)}")
        if "no_mcp" not in toolsets:
            problems.append(f"{route} does not say no_mcp, so it inherits every MCP server")
    if problems:
        return _fail(name, "; ".join(problems))
    return _pass(name, f"{', '.join(routes)}: the web, and no vault or brain_tools")


def _skill_route(route: str, block: Any) -> bool:
    """Whether this route is a skill the instance scheduled, judged by the skill its block runs."""
    if not route.startswith("scheduled-") or not isinstance(block, dict):
        return False
    return block.get("skills") == [route.removeprefix("scheduled-")]


def _webhook_routes(hermes: dict[str, Any]) -> dict[str, Any]:
    platforms = hermes.get("platforms")
    if not isinstance(platforms, dict):
        return {}
    webhook = platforms.get("webhook")
    extra = webhook.get("extra") if isinstance(webhook, dict) else None
    routes = extra.get("routes") if isinstance(extra, dict) else None
    return routes if isinstance(routes, dict) else {}


# --- sidecars -------------------------------------------------------------------------------


def _sidecars_config_check(ctx: _Instance) -> CheckResult:
    name = "sidecars_config"
    if ctx.sidecars is None:
        return _fail(name, ctx.sidecars_error or "the sidecars config is not readable")
    sections = [
        section
        for section in ("vault_watcher", "call_router", "job_runner", "scheduler")
        if getattr(ctx.sidecars, section) is not None
    ]
    return _pass(name, f"config/sidecars.yaml loads: {', '.join(sections)}")


def _sidecar_routes_check(ctx: _Instance) -> CheckResult:
    name = "sidecar_routes"
    if ctx.sidecars is None:
        return _fail(name, ctx.sidecars_error or "the sidecars config is not readable")
    hermes, error = _hermes(ctx, name)
    if error is not None:
        return error
    configured = _webhook_routes(hermes)
    problems: list[str] = []
    for route, secret in sorted(ctx.sidecars.route_secrets.items()):
        if not isinstance(configured.get(route), dict):
            problems.append(f"{route} is not a webhook route in the Hermes config")
        if not (ctx.root / SECRETS_DIR / secret.name).is_file():
            problems.append(f"{route} points at {secret}, and {SECRETS_DIR}/{secret.name} is missing")
    if problems:
        return _fail(name, "; ".join(problems))
    count = len(ctx.sidecars.route_secrets)
    return _pass(name, f"all {count} routes are a webhook route with its secret file in {SECRETS_DIR}/")


def _team_domains_check(ctx: _Instance) -> CheckResult:
    name = "team_domains"
    if ctx.sidecars is None:
        return _fail(name, ctx.sidecars_error or "the sidecars config is not readable")
    router = ctx.sidecars.call_router
    domains = list(router.rules.team_domains) if router is not None else []
    if not domains:
        return _pass(name, "no team domain is set, so every call stays personal")
    if ctx.guard is None:
        return _fail(name, ctx.guard_error or "the guard config is not readable")
    if ctx.guard.shape == "team":
        # This brain is the team brain: a call it recognises as a team call is written here, so the
        # domains are how it tells its own meetings from a 1:1.
        return _pass(name, f"{', '.join(domains)} is the team, and this is the team brain")
    if ctx.tools is not None and ctx.tools.handoff is not None:
        return _pass(
            name, f"{', '.join(domains)} is the team, and its calls are handed over with team_handoff"
        )
    return _fail(
        name,
        "team_domains is set but this brain has no team brain to route to: team calls would be lost"
        f" (found {', '.join(domains)})",
    )


def _scheduled_routes_check(ctx: _Instance) -> CheckResult:
    name = "scheduled_routes"
    if ctx.sidecars is None:
        return _fail(name, ctx.sidecars_error or "the sidecars config is not readable")
    scheduler = ctx.sidecars.scheduler
    routes = sorted({entry.route for entry in scheduler.schedule.entries}) if scheduler else []
    if not routes:
        return _pass(name, "the scheduler fires nothing, so no scheduled route to check")
    hermes, error = _hermes(ctx, name)
    if error is not None:
        return error
    configured = _webhook_routes(hermes)
    problems: list[str] = [
        f"{route} is not one of {', '.join(SCHEDULED_ROUTES)}"
        for route in sorted(set(routes) - set(SCHEDULED_ROUTES))
        if not _skill_route(route, configured.get(route))
    ]
    for route in routes:
        block = configured.get(route)
        if not isinstance(block, dict):
            problems.append(f"{route} is not a webhook route")
            continue
        toolsets = [str(toolset) for toolset in block.get("toolsets") or []]
        if "web" in toolsets:
            problems.append(f"{route} reaches the web")
        if block.get("mirror_to_session") not in (None, False):
            problems.append(f"{route} mirrors its reply into the chat")
        if route != DELIVERING_ROUTE and block.get("deliver") not in NO_DELIVERY:
            problems.append(f"{route} delivers, and only {DELIVERING_ROUTE} may")
    if problems:
        return _fail(name, "; ".join(problems))
    return _pass(name, f"{', '.join(routes)}: no web, no mirror, and only the digest is delivered")


def _tools_listen_check(ctx: _Instance) -> CheckResult:
    name = "tools.listen"
    if ctx.tools is None:
        return _fail(name, ctx.tools_error or "the tools config is not readable")
    host = ctx.tools.listen.host.strip().lower().strip("[]")
    if host.startswith("127.") or host in LOOPBACK_HOSTS:
        return _fail(
            name,
            f"brain-tools listens on {ctx.tools.listen}, so nothing outside its container can "
            "reach it, Hermes included",
        )
    return _pass(name, f"brain-tools listens on {ctx.tools.listen}, reachable from Hermes")


def _guard_tools_check(ctx: _Instance) -> CheckResult:
    name = "guard.tools"
    if ctx.guard is None:
        return _fail(name, ctx.guard_error or "the guard config is not readable")
    if ctx.tools is None:
        return _fail(name, ctx.tools_error or "the tools config is not readable")
    expected = set(export_guard_classes(export_registry(ctx.tools)))
    declared = set(ctx.guard.mcp_tools)
    missing = sorted(expected - declared)
    extra = sorted(declared - expected)
    problems: list[str] = []
    if missing:
        problems.append(f"the guard config has no entry for {', '.join(missing)}")
    if extra:
        problems.append(f"the guard config decides on tools this brain does not enable: {', '.join(extra)}")
    if problems:
        return _fail(name, "; ".join(problems))
    return _pass(name, f"all {len(expected)} enabled tools are exported into the guard config")


# --- squid ----------------------------------------------------------------------------------


def _squid_check(ctx: _Instance) -> CheckResult:
    name = "squid.registries"
    path = ctx.root / "config/squid.conf"
    try:
        lines = [line.strip() for line in path.read_text(encoding="utf-8").splitlines()]
    except OSError as exc:
        return _fail(name, f"cannot read config/squid.conf: {exc}")
    denied: set[str] = set()
    for line in lines:
        parts = line.split()
        if parts[:3] == ["acl", "package_registries", "dstdomain"] and len(parts) >= 4:
            denied.add(parts[3])
    missing = [host for host in PACKAGE_REGISTRIES if host not in denied]
    if missing:
        return _fail(name, f"not denied by name: {', '.join(missing)}")
    deny_line = ["http_access", "deny", "package_registries"]
    deny_at = next((index for index, line in enumerate(lines) if line.split()[:3] == deny_line), None)
    if deny_at is None:
        return _fail(name, "no `http_access deny package_registries` line")
    allow_at = next((index for index, line in enumerate(lines) if line.startswith("http_access allow")), None)
    if allow_at is not None and deny_at > allow_at:
        return _fail(name, "the registry deny comes after an allow line, so it never matches")
    return _pass(name, f"{len(PACKAGE_REGISTRIES)} package registries are denied before the allowlist")


# --- live -----------------------------------------------------------------------------------


def _run(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(list(argv), capture_output=True, text=True, check=False)


def _compose_output(
    name: str, compose: list[str], args: list[str], run: Runner
) -> tuple[subprocess.CompletedProcess[str] | None, CheckResult | None]:
    try:
        result = run([*compose, *args])
    except OSError as exc:
        return None, _fail(name, f"cannot run docker: {exc}")
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip().splitlines()
        return None, _fail(name, detail[-1] if detail else f"docker exited {result.returncode}")
    return result, None


def _compose_probe(
    name: str,
    compose: list[str],
    service: str,
    script: str,
    run: Runner,
    *script_args: str,
) -> tuple[int | None, str, CheckResult | None]:
    """Run a python probe in a container: exit 0 is a pass, 3 is the failure the probe reports.

    Anything else is docker itself refusing to run it, which is not an answer about the instance.
    """
    argv = [*compose, "exec", "-T", service, "python", "-c", script, *script_args]
    try:
        result = run(argv)
    except OSError as exc:
        return None, "", _fail(name, f"cannot run docker: {exc}")
    output = (result.stderr or result.stdout or "").strip().splitlines()
    detail = output[-1] if output else ""
    if result.returncode == 0:
        return 0, detail, None
    if result.returncode == 3:
        return 3, detail, None
    return None, detail, _fail(name, detail or f"the probe in {service} exited {result.returncode}")


def _declared_services(root: Path) -> list[str]:
    """The services compose.yaml declares: one with no container never appears in `ps`."""
    data, _ = _read_yaml(root / "compose.yaml")
    services = data.get("services") if isinstance(data, dict) else None
    if not isinstance(services, dict):
        return []
    return sorted(str(service) for service in services)


def _live_services(compose: list[str], run: Runner, declared: Sequence[str]) -> CheckResult:
    name = "live.services"
    result, error = _compose_output(name, compose, ["ps", "--format", "json"], run)
    if error is not None:
        return error
    assert result is not None
    rows = _ps_rows(result.stdout)
    if not rows and not declared:
        return _fail(name, "compose reports no service: is the stack up?")
    problems: list[str] = []
    running = {str(row.get("Service") or row.get("Name") or "?") for row in rows}
    for service in declared:
        if service not in running:
            problems.append(f"{service} has no container: was its image pulled?")
    for row in rows:
        service = str(row.get("Service") or row.get("Name") or "?")
        state = str(row.get("State") or "")
        health = str(row.get("Health") or "")
        if state != "running":
            problems.append(f"{service} is {state or 'unknown'}")
        elif health and health != "healthy":
            problems.append(f"{service} is {health}")
        elif not health:
            problems.append(f"{service} has no healthcheck")
    if problems:
        return _fail(name, "; ".join(problems))
    return _pass(name, f"{len(rows)} services running and healthy")


def _ps_rows(output: str) -> list[dict[str, Any]]:
    """`compose ps --format json`: a JSON array in some versions, one object per line in others."""
    text = output.strip()
    if not text:
        return []
    try:
        parsed = json.loads(text)
    except ValueError:
        parsed = None
    if isinstance(parsed, list):
        return [row for row in parsed if isinstance(row, dict)]
    if isinstance(parsed, dict):
        return [parsed]
    rows: list[dict[str, Any]] = []
    for line in text.splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def _live_vault_read_only(compose: list[str], run: Runner) -> CheckResult:
    name = "live.vault-read-only"
    code, detail, error = _compose_probe(name, compose, HERMES, VAULT_WRITE_PROBE, run)
    if error is not None:
        return error
    if code == 0:
        return _fail(name, f"{VAULT_TARGET} is writable from the {HERMES} container")
    return _pass(name, f"{VAULT_TARGET} is read-only inside {HERMES}")


def _live_no_socket(compose: list[str], run: Runner) -> CheckResult:
    name = "live.no-docker-socket"
    code, detail, error = _compose_probe(name, compose, HERMES, SOCKET_PROBE, run)
    if error is not None:
        return error
    if code == 3:
        return _fail(name, f"{HERMES} can see /var/run/docker.sock")
    return _pass(name, f"{HERMES} has no /var/run/docker.sock")


def _live_heartbeat(compose: list[str], run: Runner) -> CheckResult:
    name = "live.heartbeat"
    code, detail, error = _compose_probe(
        name, compose, WATCHDOG, HEARTBEAT_PROBE, run, str(HEARTBEAT_MAX_AGE)
    )
    if error is not None:
        return error
    reason = detail or "the heartbeat was not read"
    if code == 3:
        return _fail(name, reason)
    return _pass(name, f"read through {WATCHDOG}: {reason}")


def _live_watchdog_docker(compose: list[str], run: Runner) -> CheckResult:
    name = "live.watchdog-docker"
    code, detail, error = _compose_probe(name, compose, WATCHDOG, WATCHDOG_DOCKER_PROBE, run)
    if error is not None:
        return error
    reason = detail or "the docker socket was not read"
    if code == 3:
        return _fail(name, reason)
    return _pass(name, f"read through {WATCHDOG}: {reason}")
