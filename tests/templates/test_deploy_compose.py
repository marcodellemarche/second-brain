"""The deploy templates: render them with sample values and check what the deployment promises.

The renderer is the tiny one from `test_hermes_templates.py`, extended with the one `{% for %}` the
squid allowlist needs. The real rendering lives in the CLI; what these tests care about is the
content of the files, and the two structural promises: one writer of the vault, one docker socket,
no published port, one door to the internet.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

DEPLOY = Path(__file__).resolve().parents[2] / "templates" / "deploy"

VAULT = "/srv/brain/vault"
MEDIA = "/srv/brain/media"
DOCKER_SOCKET = "/var/run/docker.sock"
HOSTS = ["api.telegram.org", "model.example.com"]

VALUES: dict[str, Any] = {
    "instance_name": "sample-brain",
    "vault_dir": VAULT,
    "media_dir": MEDIA,
    "terminal_cwd": "/workspace",
    "hermes_image": "ghcr.io/example/second-brain/hermes:0.1.0",
    "terminal_image": "ghcr.io/example/second-brain/terminal:0.1.0",
    "brain_image": "ghcr.io/example/second-brain/brain:0.1.0",
    "egress_image": "ubuntu/squid:6.6-24.04_edge",
    "searxng_image": "docker.io/searxng/searxng:2025.1.1",
    "model_upstream": "https://model.example.com/v1",
    "egress_hosts": HOSTS,
    "searxng_upstream_hosts": "duckduckgo.com wikipedia.org",
    "searxng_secret_key": "sample-secret-key-not-a-real-one",
}

_PLACEHOLDER = re.compile(r"\{\{\s*(\w+)\s*\}\}")
_FOR = re.compile(r"\{%\s*for\s+(\w+)\s+in\s+(\w+)\s*%\}(.*?)\{%\s*endfor\s*%\}", re.S)


def _value(name: str, values: dict[str, Any]) -> str:
    assert name in values, f"unknown placeholder {name!r}"
    return str(values[name])


def render(template: Path, values: dict[str, Any]) -> str:
    text = template.read_text(encoding="utf-8")

    def loop(match: re.Match[str]) -> str:
        item, collection, body = match.group(1), match.group(2), match.group(3)
        assert collection in values, f"{template.name} loops over an unknown {collection!r}"
        return "".join(
            re.sub(r"\{\{\s*" + item + r"\s*\}\}", str(entry), body) for entry in values[collection]
        )

    text = _FOR.sub(loop, text)
    return _PLACEHOLDER.sub(lambda match: _value(match.group(1), values), text)


@pytest.fixture(scope="module")
def compose() -> dict[str, Any]:
    loaded = yaml.safe_load(render(DEPLOY / "compose.yaml.j2", VALUES))
    assert isinstance(loaded, dict)
    return loaded


@pytest.fixture(scope="module")
def services(compose: dict[str, Any]) -> dict[str, Any]:
    loaded = compose["services"]
    assert isinstance(loaded, dict)
    return loaded


@pytest.fixture(scope="module")
def squid() -> str:
    return render(DEPLOY / "squid.conf.j2", VALUES)


def _mounts(service: dict[str, Any]) -> list[str]:
    volumes = service.get("volumes", [])
    return [item if isinstance(item, str) else item.get("source", "") for item in volumes]


def test_every_service_from_the_plan_is_there(services: dict[str, Any]) -> None:
    assert set(services) == {
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


def test_every_service_has_a_healthcheck_and_a_restart_policy(services: dict[str, Any]) -> None:
    for name, service in services.items():
        assert "healthcheck" in service, name
        assert service.get("restart") == "unless-stopped", name


def test_no_service_publishes_a_port(services: dict[str, Any]) -> None:
    for name, service in services.items():
        assert "ports" not in service, name
        assert "network_mode" not in service, name


def test_the_vault_is_read_only_everywhere_but_brain_tools(services: dict[str, Any]) -> None:
    assert f"{VAULT}:/vault" in _mounts(services["brain-tools"])
    for name in ("hermes", "vault-watcher", "call-router", "job-runner"):
        assert f"{VAULT}:/vault:ro" in _mounts(services[name]), name
    for name, service in services.items():
        for mount in service.get("volumes", []):
            if not isinstance(mount, str) or not mount.startswith(VAULT):
                continue
            assert mount.endswith(":ro") or name == "brain-tools", f"{name}: {mount}"


def test_brain_tools_is_the_only_writer_and_reads_media_back(services: dict[str, Any]) -> None:
    # A reply may point at a file Hermes wrote, and that is all brain_tools needs from it.
    assert f"{MEDIA}:{MEDIA}:ro" in _mounts(services["brain-tools"])


def test_only_the_watchdog_gets_the_docker_socket(services: dict[str, Any]) -> None:
    with_socket = [
        name for name, service in services.items() if any(DOCKER_SOCKET in m for m in _mounts(service))
    ]
    assert with_socket == ["watchdog"]
    # The docker terminal backend would need the socket, so the switch to ssh is what keeps this.
    assert "terminal" not in with_socket


def test_hermes_reads_the_managed_config_read_only(services: dict[str, Any]) -> None:
    mounts = _mounts(services["hermes"])
    assert "./config/hermes/config.yaml:/etc/hermes/config.yaml:ro" in mounts
    # The managed env scope holds the tokens the config references by ${NAME}.
    assert "./secrets/hermes.env:/etc/hermes/.env:ro" in mounts
    assert "env_file" not in services["hermes"]


def test_only_the_three_allowed_services_see_the_internet(compose: dict[str, Any]) -> None:
    networks = compose["networks"]
    assert networks["internal"]["internal"] is True
    assert networks["terminal-net"]["internal"] is True
    on_egress = {name for name, service in compose["services"].items() if "egress-net" in service["networks"]}
    assert on_egress == {"egress", "gate", "searxng"}


def test_every_service_is_on_an_internal_network(services: dict[str, Any]) -> None:
    # The terminal is on its own internal network, with no route to anything but Hermes.
    for name, service in services.items():
        assert {"internal", "terminal-net"} & set(service["networks"]), name


def test_the_terminal_is_reachable_only_from_hermes(services: dict[str, Any]) -> None:
    assert services["terminal"]["networks"] == ["terminal-net"]
    assert "terminal-net" in services["hermes"]["networks"]


def test_hermes_reaches_the_internet_only_through_the_proxy(services: dict[str, Any]) -> None:
    env = services["hermes"]["environment"]
    assert env["HTTPS_PROXY"] == "http://egress:3128"
    assert env["HTTP_PROXY"] == "http://egress:3128"
    # The internal names must not be proxied, or the MCP and model calls never leave the box.
    for internal in ("brain-tools", "gate", "watchdog"):
        assert internal in env["NO_PROXY"]


def test_hermes_never_holds_the_model_key(services: dict[str, Any]) -> None:
    env = services["hermes"]["environment"]
    assert env["MODEL_GATE_API_KEY"] == "held-by-the-gate"
    assert "model_api_key" not in services["hermes"].get("secrets", [])
    assert services["gate"]["secrets"] == ["model_api_key"]


def test_each_secret_goes_to_the_services_that_need_it(services: dict[str, Any]) -> None:
    owners: dict[str, list[str]] = {}
    for name, service in services.items():
        for secret in service.get("secrets", []):
            owners.setdefault(secret, []).append(name)
    assert owners["tools_token"] == ["brain-tools"]
    assert owners["model_api_key"] == ["gate"]
    assert owners["terminal_ssh_key"] == ["hermes"]
    assert owners["calendar_token"] == ["brain-tools"]
    # Both watch the vault and both sign with the `ingest` route's secret.
    assert set(owners["webhook_ingest"]) == {"vault-watcher", "call-router"}
    assert set(owners["webhook_research"] + owners["webhook_idea"]) == {"job-runner"}
    # Hermes is the last place a secret should be, and the only one it gets is the terminal key.
    assert services["hermes"]["secrets"] == ["terminal_ssh_key"]


def test_the_sidecars_share_the_brain_image_with_their_own_commands(services: dict[str, Any]) -> None:
    names = (
        "brain-tools",
        "gate",
        "watchdog",
        "vault-watcher",
        "call-router",
        "job-runner",
        "scheduler",
    )
    commands = set()
    for name in names:
        service = services[name]
        assert service["image"] == VALUES["brain_image"], name
        assert service["command"][:2] == ["python", "-m"], name
        commands.add(tuple(service["command"]))
    assert len(commands) == len(names)


def test_no_container_runs_with_added_privileges(services: dict[str, Any]) -> None:
    for name, service in services.items():
        assert "no-new-privileges:true" in service["security_opt"], name
        assert service["cap_drop"] == ["ALL"], name


def test_squid_allows_only_the_rendered_allowlist(squid: str) -> None:
    for host in HOSTS:
        assert f"acl allowed_egress dstdomain {host}" in squid
    assert "acl allowed_egress dstdomain duckduckgo.com wikipedia.org" in squid
    assert "http_access allow CONNECT allowed_egress" in squid
    assert "http_access deny all" in squid


def test_squid_denies_the_package_registries(squid: str) -> None:
    for host in (
        "pypi.org",
        "files.pythonhosted.org",
        "registry.npmjs.org",
        "crates.io",
        "static.crates.io",
        "deb.debian.org",
        "security.debian.org",
        "dl-cdn.alpinelinux.org",
    ):
        assert f"acl package_registries dstdomain {host}" in squid
    # The deny has to come before the allow, or the allowlist would win.
    assert squid.index("http_access deny package_registries") < squid.index("http_access allow")


def test_squid_denies_private_ranges(squid: str) -> None:
    for network in ("10.0.0.0/8", "127.0.0.0/8", "169.254.0.0/16", "192.168.0.0/16", "fc00::/7"):
        assert f"acl private_dst dst {network}" in squid
    assert squid.index("http_access deny private_dst") < squid.index("http_access allow")


def test_squid_has_no_plain_http_and_no_cache(squid: str) -> None:
    assert "http_access deny !CONNECT" in squid
    assert "http_access deny CONNECT !SSL_ports" in squid
    assert "cache deny all" in squid


def test_searxng_is_private_and_speaks_json() -> None:
    settings = yaml.safe_load(render(DEPLOY / "searxng" / "settings.yml.j2", VALUES))
    assert settings["server"]["public_instance"] is False
    assert settings["server"]["limiter"] is False
    assert settings["server"]["secret_key"] == VALUES["searxng_secret_key"]
    assert "json" in settings["search"]["formats"]
    engines = {engine["name"] for engine in settings["engines"]}
    assert {"duckduckgo", "wikipedia"} <= engines


SIDECAR_CONFIG = "./config/sidecars.yaml:/etc/second-brain/sidecars.yaml:ro"


@pytest.fixture(scope="module")
def declared_secrets(compose: dict[str, Any]) -> dict[str, Any]:
    loaded = compose["secrets"]
    assert isinstance(loaded, dict)
    return loaded


def test_the_scheduler_fires_the_scheduled_tasks(services: dict[str, Any]) -> None:
    scheduler = services["scheduler"]
    assert scheduler["container_name"] == "sample-brain-scheduler"
    assert scheduler["command"] == ["python", "-m", "second_brain.sidecars.scheduler"]
    assert scheduler["networks"] == ["internal"]
    assert scheduler["environment"]["SIDECARS_CONFIG"] == "/etc/second-brain/sidecars.yaml"
    assert scheduler["environment"]["HOME"] == "/tmp"
    # The schedule and the route secrets come from the config and the mounted secret.
    assert SIDECAR_CONFIG in _mounts(scheduler)
    assert "data:/data" in _mounts(scheduler)
    assert scheduler["secrets"] == ["webhook_schedule"]
    assert scheduler["mem_limit"] == "128m"
    assert scheduler["pids_limit"] == 64
    # The task is a skill: the scheduler has no vault of its own to read.
    assert not [mount for mount in _mounts(scheduler) if "/vault" in mount]


def test_every_sidecar_reads_the_sidecars_config(services: dict[str, Any]) -> None:
    for name in ("vault-watcher", "call-router", "job-runner", "scheduler"):
        assert services[name]["environment"]["SIDECARS_CONFIG"] == "/etc/second-brain/sidecars.yaml", name
        assert SIDECAR_CONFIG in _mounts(services[name]), name


def test_brain_tools_reads_the_outbox_secret(
    services: dict[str, Any], declared_secrets: dict[str, Any]
) -> None:
    # The outbox endpoint checks the same secret Hermes signs its outbound hook with.
    assert "outbox_secret" in services["brain-tools"]["secrets"]
    assert "outbox_secret" in declared_secrets


def test_every_secret_is_a_file_next_to_the_instance(declared_secrets: dict[str, Any]) -> None:
    assert "webhook_schedule" in declared_secrets
    for name, secret in declared_secrets.items():
        assert secret["file"] == f"./secrets/{name}", name
