"""The deploy templates: render them with sample values and check what the deployment promises.

The renderer is the tiny one from `test_hermes_templates.py`, extended with the one `{% for %}` the
squid allowlist needs. The real rendering lives in the CLI; what these tests care about is the
content of the files, and the two structural promises: one writer of the vault, one docker socket,
no published port, one door to the internet.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from second_brain.cli.config import egress_ports, sidecar_services
from second_brain.cli.render import render_template, render_text
from second_brain.sidecars.extension import SidecarSpec
from second_brain.tools.registry import Registry

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
    "egress_ips": [],
    "has_egress_ips": False,
    "egress_lan_rules": [],
    "egress_ports": [],
    "searxng_upstream_hosts": "duckduckgo.com wikipedia.org",
    "searxng_secret_key": "sample-secret-key-not-a-real-one",
    "docker_gid": 999,
    "brain_tools_extra_volumes": ["/srv/brain/archive:/srv/brain/archive"],
    "optional_secrets": ["calendar_token"],
    "instance_secrets": [],
    "brain_tools_secrets": ["tools_token", "outbox_secret", "calendar_token"],
    # Empty by default: a vault only needs a shared group when a second service writes it.
    "vault_group_gids": [],
    # The services an instance package adds: none unless one is installed and configured.
    "instance_sidecars": "",
    "has_memory_export": False,
}


def render(template: Path, values: dict[str, Any]) -> str:
    return render_template(template, values)


@pytest.fixture(scope="module")
def compose() -> dict[str, Any]:
    loaded = yaml.safe_load(render(DEPLOY / "compose.yaml.j2", VALUES))
    assert isinstance(loaded, dict)
    return loaded


def rendered_services(**overrides: Any) -> dict[str, Any]:
    values = {**VALUES, **overrides}
    loaded = yaml.safe_load(render(DEPLOY / "compose.yaml.j2", values))
    assert isinstance(loaded, dict)
    services = loaded["services"]
    assert isinstance(services, dict)
    return services


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
    for name in ("hermes", "terminal", "vault-watcher", "call-router", "job-runner"):
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


def test_only_the_allowed_services_see_the_internet(compose: dict[str, Any]) -> None:
    # brain-tools is here because its tools declare the hosts they need and squid is the only way
    # out: without the egress network no tool with an external host could ever reach it.
    networks = compose["networks"]
    assert networks["internal"]["internal"] is True
    assert networks["terminal-net"]["internal"] is True
    on_egress = {name for name, service in compose["services"].items() if "egress-net" in service["networks"]}
    assert on_egress == {"egress", "gate", "searxng", "brain-tools"}


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


S6_CAPS = ["CHOWN", "SETUID", "SETGID", "DAC_OVERRIDE", "FOWNER", "KILL"]


@pytest.mark.parametrize("name", ["hermes", "egress"])
def test_the_services_that_drop_privileges_keep_only_the_caps_they_need(
    services: dict[str, Any], name: str
) -> None:
    # s6 in the Hermes image and squid both start as root and switch user: these are the calls
    # that takes, and cap_drop: ALL stays on top of it.
    assert services[name]["cap_add"] == S6_CAPS
    assert services[name]["cap_drop"] == ["ALL"]
    assert "no-new-privileges:true" in services[name]["security_opt"]


def test_the_egress_healthcheck_does_not_ask_squid_about_its_pid_file(services: dict[str, Any]) -> None:
    test = services["egress"]["healthcheck"]["test"]
    assert "squid -k" not in " ".join(test)
    assert test == ["CMD", "bash", "-c", "exec 3<>/dev/tcp/127.0.0.1/3128"]


def test_the_watchdog_can_open_the_docker_socket(services: dict[str, Any]) -> None:
    # The socket is root:docker 660 and the container runs as 23002.
    assert services["watchdog"]["group_add"] == [str(VALUES["docker_gid"])]


def test_brain_tools_runs_as_the_image_user(services: dict[str, Any]) -> None:
    # The image sets USER 23002 itself, and overriding it here is one place too many to keep right.
    assert "user" not in services["brain-tools"]


def test_brain_tools_mounts_the_configured_extra_volumes() -> None:
    with_archive = rendered_services()
    assert "/srv/brain/archive:/srv/brain/archive" in _mounts(with_archive["brain-tools"])
    # The archive dir is optional: an empty list must still render valid YAML with no stray item.
    without = rendered_services(brain_tools_extra_volumes=[])
    mounts = _mounts(without["brain-tools"])
    assert not [mount for mount in mounts if "archive" in mount]
    assert f"{VAULT}:/vault" in mounts


def test_the_terminal_sees_the_vault_and_only_the_public_key(services: dict[str, Any]) -> None:
    # Hermes' file tools run over ssh in this container, so the /vault paths have to match.
    mounts = _mounts(services["terminal"])
    assert f"{VAULT}:/vault:ro" in mounts
    assert "./secrets/terminal_ssh_key.pub:/etc/ssh/authorized_keys/agent:ro" in mounts
    # The private key stays with Hermes.
    assert not [mount for mount in mounts if mount.endswith("terminal_ssh_key")]
    assert services["terminal"]["tmpfs"] == [
        "/tmp",
        "/home/agent:uid=1000,gid=1000,mode=0700",
        f"{VALUES['terminal_cwd']}:uid=1000,gid=1000,mode=0700",
    ]


def test_the_terminal_host_key_is_mounted_read_only(services: dict[str, Any]) -> None:
    # The host key outlives the image: one generated at build time changes at every rebuild.
    assert "./secrets/terminal_host_key:/etc/ssh/terminal_host_key:ro" in _mounts(services["terminal"])
    # It is a file mount, not a compose secret: sshd reads it as a plain path from its config.
    assert "terminal_host_key" not in services["terminal"].get("secrets", [])


def test_the_terminal_writable_paths_belong_to_the_agent_user(services: dict[str, Any]) -> None:
    # The container is read-only and a tmpfs comes up root-owned, so agent could write nowhere.
    for entry in services["terminal"]["tmpfs"]:
        if ":" not in entry:
            continue
        path, _, options = entry.partition(":")
        assert "uid=1000" in options, path
        assert "gid=1000" in options, path


def test_the_product_skills_are_mounted_read_only_under_the_hermes_home(services: dict[str, Any]) -> None:
    # Hermes scans /opt/data/skills/<category>/<skill>, and a read-only mount is what keeps a skill
    # write approved from the chat away from product code.
    assert "./skills:/opt/data/skills/second-brain:ro" in _mounts(services["hermes"])
    assert not [mount for mount in _mounts(services["hermes"]) if "/opt/second-brain" in mount]


def test_the_terminal_healthcheck_does_not_need_nc(services: dict[str, Any]) -> None:
    test = services["terminal"]["healthcheck"]["test"]
    assert "nc" not in test
    assert test[0] == "CMD" and test[1] == "python3"


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


def test_squid_shuts_down_inside_the_docker_timeout(squid: str) -> None:
    # Its own default is 30s, and Docker kills the container after 10s.
    assert "shutdown_lifetime 3 seconds" in squid


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
    # Its poll sleeps up to 30s, longer than the 10s Docker waits before a kill.
    assert scheduler["stop_grace_period"] == "40s"
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


def test_no_calendar_means_no_calendar_secret_anywhere() -> None:
    values = {**VALUES, "optional_secrets": [], "brain_tools_secrets": ["tools_token", "outbox_secret"]}
    loaded = yaml.safe_load(render(DEPLOY / "compose.yaml.j2", values))
    assert "calendar_token" not in loaded["secrets"]
    assert loaded["services"]["brain-tools"]["secrets"] == ["tools_token", "outbox_secret"]


def test_egress_runs_under_an_init_that_forwards_sigterm(services: dict[str, Any]) -> None:
    assert services["egress"]["init"] is True


def test_hermes_finds_searxng(services: dict[str, Any]) -> None:
    assert services["hermes"]["environment"]["SEARXNG_URL"] == "http://searxng:8080"


def test_an_instance_secret_is_declared_and_mounted_on_brain_tools() -> None:
    values = {
        **VALUES,
        "instance_secrets": ["parcel_api_key"],
        "brain_tools_secrets": ["tools_token", "outbox_secret", "calendar_token", "parcel_api_key"],
    }
    loaded = yaml.safe_load(render(DEPLOY / "compose.yaml.j2", values))
    assert loaded["secrets"]["parcel_api_key"] == {"file": "./secrets/parcel_api_key"}
    # Nothing else reads it: a secret goes to the one container that needs it.
    owners = [
        name for name, service in loaded["services"].items() if "parcel_api_key" in service.get("secrets", [])
    ]
    assert owners == ["brain-tools"]
    # The declared secrets and the mounted ones are the same set, or compose refuses to start.
    assert set(loaded["secrets"]) == {
        secret for service in loaded["services"].values() for secret in service.get("secrets", [])
    }


def test_the_brain_image_comes_from_the_values() -> None:
    services = rendered_services(brain_image="registry.example.com/team/brain:1.2.3")
    for name in (
        "brain-tools",
        "gate",
        "watchdog",
        "vault-watcher",
        "call-router",
        "job-runner",
        "scheduler",
    ):
        assert services[name]["image"] == "registry.example.com/team/brain:1.2.3", name


def test_brain_tools_can_reach_the_hosts_its_tools_declare(services: dict[str, Any]) -> None:
    # A tool declares hosts and squid lets only those through, but from a network with no gateway
    # nothing is reachable at all: brain-tools needs the egress network and the proxy as well.
    service = services["brain-tools"]
    assert "egress-net" in service["networks"]
    assert service["environment"]["HTTPS_PROXY"] == "http://egress:3128"
    # The internal names stay off the proxy, and `egress` itself is the proxy: excluding it would
    # mean the request to the proxy is never proxied.
    no_proxy = service["environment"]["NO_PROXY"].split(",")
    assert "localhost" in no_proxy
    assert "brain-tools" in no_proxy


def test_the_brain_services_join_the_vault_group_when_one_is_set() -> None:
    # A vault inside a Nextcloud data directory is owned by a group www-data is in too: the brain
    # has to join it, or brain_tools cannot write where Nextcloud writes.
    from second_brain.cli.answers import Answers
    from second_brain.cli.config import template_values
    from second_brain.tools.registry import Registry

    sample = Path(__file__).parent / ".." / "cli" / "answers.sample.yaml"
    data = yaml.safe_load(sample.read_text(encoding="utf-8"))
    data["vault_group_gid"] = 1003
    answers = Answers.model_validate(data)

    from second_brain.cli.secrets import SEARXNG_KEY

    values = template_values(answers, Registry(), {SEARXNG_KEY: "x"})
    assert values["vault_group_gids"] == ["1003"]

    template = (Path(__file__).parent / ".." / ".." / "templates" / "deploy" / "compose.yaml.j2").read_text()
    block = template[template.index("x-brain:") : template.index("x-sidecar-health:")]
    rendered = render_text(block, values)
    assert 'group_add: ["1003"]' in rendered

    without = template_values(
        answers.model_copy(update={"vault_group_gid": None}), Registry(), {SEARXNG_KEY: "x"}
    )
    assert render_text(block, without).count("group_add") == 0


# --- the instance sidecars --------------------------------------------------------------------


MAIL = SidecarSpec(
    name="mail",
    command=["python", "-m", "example_instance.mail"],
    config={"interval_seconds": 900},
    hosts=["imap.example.test:993"],
    credentials=["mail_user", "mail_app_password"],
    webhook=True,
    data=True,
    files={"/etc/example/rules.json": Path("/srv/brain/mail-rules.json")},
)
AUDIO = SidecarSpec(
    name="audio-worker",
    command=["python", "-m", "example_instance.audio"],
    image="ghcr.io/example/audio:1",
    data=True,
    media=True,
    cpus=1.0,
    mem_limit="5g",
)
WITH_SIDECARS = {
    "instance_sidecars": sidecar_services("sample-brain", Path(MEDIA), [MAIL, AUDIO]),
    "instance_secrets": ["mail_user", "mail_app_password"],
}


def test_no_sidecar_is_rendered_by_default(services: dict[str, Any]) -> None:
    assert not any("SIDECAR_CONFIG" in service.get("environment", {}) for service in services.values())


def test_a_sidecar_runs_the_brain_image_unless_it_names_one() -> None:
    services = rendered_services(**WITH_SIDECARS)
    assert services["mail"]["image"] == VALUES["brain_image"]
    assert services["audio-worker"]["image"] == "ghcr.io/example/audio:1"
    assert services["mail"]["command"] == ["python", "-m", "example_instance.mail"]
    assert services["mail"]["container_name"] == "sample-brain-mail"


def test_a_sidecar_keeps_the_hardening_of_every_brain_service() -> None:
    for name in ("mail", "audio-worker"):
        service = rendered_services(**WITH_SIDECARS)[name]
        assert service["read_only"] is True
        assert service["cap_drop"] == ["ALL"]
        assert "no-new-privileges:true" in service["security_opt"]
        assert "ports" not in service


def test_a_sidecar_without_hosts_has_no_way_out() -> None:
    audio = rendered_services(**WITH_SIDECARS)["audio-worker"]
    assert audio["networks"] == ["internal"]
    assert "HTTPS_PROXY" not in audio["environment"]
    assert "secrets" not in audio


def test_a_sidecar_with_hosts_goes_out_through_the_proxy_only() -> None:
    mail = rendered_services(**WITH_SIDECARS)["mail"]
    assert mail["networks"] == ["internal", "egress-net"]
    assert mail["environment"]["HTTPS_PROXY"] == "http://egress:3128"


def test_a_sidecar_gets_only_the_secrets_and_volumes_it_declared() -> None:
    services = rendered_services(**WITH_SIDECARS)
    mail, audio = services["mail"], services["audio-worker"]
    assert mail["secrets"] == ["mail_user", "mail_app_password", "webhook_ingest"]
    assert mail["volumes"] == [
        "./config/mail.json:/etc/second-brain/mail.json:ro",
        "data:/data",
        "/srv/brain/mail-rules.json:/etc/example/rules.json:ro",
    ]
    assert mail["environment"]["SIDECAR_CONFIG"] == "/etc/second-brain/mail.json"
    assert f"{MEDIA}:{MEDIA}" in audio["volumes"]
    assert not any(VAULT in volume for volume in mail["volumes"] + audio["volumes"])


def test_a_sidecar_has_a_healthcheck_on_its_own_process_and_its_limits() -> None:
    audio = rendered_services(**WITH_SIDECARS)["audio-worker"]
    assert "b'example_instance.audio'" in audio["healthcheck"]["test"][-1]
    assert audio["mem_limit"] == "5g"
    assert audio["cpus"] == 1.0


def test_the_sidecar_secrets_are_declared_at_the_top() -> None:
    rendered = yaml.safe_load(render(DEPLOY / "compose.yaml.j2", {**VALUES, **WITH_SIDECARS}))
    assert rendered["secrets"]["mail_user"] == {"file": "./secrets/mail_user"}
    assert "mail_user" not in rendered["services"]["brain-tools"]["secrets"]


def test_the_proxy_opens_a_sidecar_port_before_the_final_deny() -> None:
    assert "port 993" not in render(DEPLOY / "squid.conf.j2", VALUES)
    ports = egress_ports(Registry(), [MAIL])
    assert ports == [993]
    with_mail = render(DEPLOY / "squid.conf.j2", {**VALUES, "egress_ports": ports})
    allowed = with_mail.index("http_access allow CONNECT allowed_egress")
    assert with_mail.index("acl SSL_ports port 993") < allowed
    assert with_mail.strip().endswith("http_access deny all")


def test_the_proxy_opens_a_declared_port_and_matches_an_ip_by_address() -> None:
    plain = render(DEPLOY / "squid.conf.j2", VALUES)
    assert "port 6443" not in plain
    assert "allowed_egress_ip" not in plain
    squid = render(
        DEPLOY / "squid.conf.j2",
        {**VALUES, "egress_ports": [6443], "egress_ips": ["203.0.113.7"], "has_egress_ips": True},
    )
    assert "acl SSL_ports port 6443" in squid
    assert "acl allowed_egress_ip dst 203.0.113.7" in squid
    # Private ranges are still denied before either allow, and everything else after.
    allow_ip = squid.index("http_access allow CONNECT allowed_egress_ip")
    assert squid.index("http_access deny private_dst") < allow_ip
    assert squid.strip().endswith("http_access deny all")


def test_hermes_reads_the_catalog_overlay_read_only() -> None:
    compose = yaml.safe_load(render(DEPLOY / "compose.yaml.j2", VALUES))
    assert "./config/hermes/locales:/opt/data/locales:ro" in compose["services"]["hermes"]["volumes"]
