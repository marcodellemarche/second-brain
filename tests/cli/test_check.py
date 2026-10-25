"""`sb check` against an instance `sb init` built, and against one file broken at a time.

The instance is the same sample the `init` tests use, so the passing baseline is a real instance
rather than a fixture written to match the checks. Each failing test breaks exactly one thing: a
check that fails for two reasons would not say which rule it is actually enforcing.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest
import yaml

from second_brain.cli import check
from second_brain.cli.answers import Answers
from second_brain.cli.check import (
    FAIL,
    PASS,
    SKIP,
    CheckResult,
    live_checks,
    static_checks,
)
from second_brain.cli.init import init
from second_brain.cli.main import main

SAMPLE = Path(__file__).with_name("answers.sample.yaml")
FILLED = ("model_api_key", "calendar_token", "telegram_bot_token")


@pytest.fixture
def instance(tmp_path: Path) -> Path:
    raw = yaml.safe_load(SAMPLE.read_text(encoding="utf-8"))
    raw["vault_dir"] = str(tmp_path / "vault")
    raw["media_dir"] = str(tmp_path / "media")
    raw["archive_dir"] = str(tmp_path / "archive")
    answers_file = tmp_path / "answers.yaml"
    answers_file.write_text(yaml.safe_dump(raw), encoding="utf-8")
    directory = tmp_path / "instance"
    init(Answers.from_yaml(answers_file), directory)
    return directory


def _results(instance: Path) -> dict[str, CheckResult]:
    return {result.name: result for result in static_checks(instance)}


def _status(instance: Path, name: str) -> tuple[str, str]:
    result = _results(instance)[name]
    return result.status, result.reason


def _edit_yaml(path: Path, mutate: Callable[[dict], None]) -> None:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    mutate(data)
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


def _edit_text(path: Path, old: str, new: str) -> None:
    text = path.read_text(encoding="utf-8")
    assert old in text, old
    path.write_text(text.replace(old, new, 1), encoding="utf-8")


# --- a fresh instance -----------------------------------------------------------------------


def test_a_fresh_instance_passes_every_check_but_the_secrets(instance: Path) -> None:
    results = _results(instance)
    failed = {name for name, result in results.items() if result.status == FAIL}
    assert failed == {"secrets"}, {name: results[name].reason for name in failed}
    for name, result in results.items():
        assert result.status in (PASS, FAIL), (name, result.reason)


def test_the_secrets_check_names_the_files_a_human_must_fill(instance: Path) -> None:
    status, reason = _status(instance, "secrets")
    assert status == FAIL
    for name in FILLED:
        assert name in reason, name
    # The generated ones are never reported: nobody has to do anything about them.
    assert "tools_token" not in reason
    assert "webhook_ingest" not in reason


def test_filling_the_secrets_makes_every_static_check_pass(instance: Path) -> None:
    for name in FILLED:
        path = instance / "secrets" / name
        path.write_text("filled\n", encoding="utf-8")
        path.chmod(0o600)
    statuses = {name: result.status for name, result in _results(instance).items()}
    assert set(statuses.values()) == {PASS}, statuses


def test_a_missing_file_fails_the_files_check(instance: Path) -> None:
    (instance / "config" / "squid.conf").unlink()
    status, reason = _status(instance, "files")
    assert status == FAIL
    assert "config/squid.conf" in reason


def test_a_missing_chat_token_fails_the_files_check(instance: Path) -> None:
    (instance / "secrets" / "telegram_bot_token").unlink()
    status, reason = _status(instance, "files")
    assert status == FAIL
    assert "telegram_bot_token" in reason


def test_a_slack_brain_expects_the_slack_tokens(tmp_path: Path) -> None:
    raw = yaml.safe_load(SAMPLE.read_text(encoding="utf-8"))
    del raw["owner_telegram_id"]
    raw["slack"] = {"owner_id": "U0000001", "home_channel": "C0000001"}
    raw["vault_dir"] = str(tmp_path / "vault")
    raw["media_dir"] = str(tmp_path / "media")
    raw["archive_dir"] = str(tmp_path / "archive")
    answers_file = tmp_path / "answers.yaml"
    answers_file.write_text(yaml.safe_dump(raw), encoding="utf-8")
    directory = tmp_path / "slack-instance"
    init(Answers.from_yaml(answers_file), directory)

    statuses = {name: result.status for name, result in _results(directory).items()}
    assert set(statuses.values()) <= {PASS, FAIL}
    assert statuses["files"] == PASS
    assert statuses["hermes.owner"] == PASS
    assert statuses["squid.registries"] == PASS
    # The Slack tokens are empty, and so is the Telegram one: that file is not even there.
    assert statuses["secrets"] == FAIL
    assert "slack_bot_token" in _results(directory)["secrets"].reason


def test_a_leftover_placeholder_fails_the_placeholders_check(instance: Path) -> None:
    soul = instance / "config/hermes/SOUL.md"
    soul.write_text(soul.read_text(encoding="utf-8") + "\nSigned: {{ owner_name }}\n", encoding="utf-8")
    status, reason = _status(instance, "placeholders")
    assert status == FAIL
    assert "SOUL.md" in reason


def test_a_secret_readable_by_the_group_fails_the_secrets_check(instance: Path) -> None:
    (instance / "secrets" / "tools_token").chmod(0o640)
    status, reason = _status(instance, "secrets")
    assert status == FAIL
    assert "tools_token" in reason


def test_an_unreadable_secret_with_content_is_not_reported_as_empty(instance: Path) -> None:
    # A deployed host hands the secrets to the container uids, so the operator running
    # `sb check` cannot open them: the check must go by size, never by content.
    if os.geteuid() == 0:
        pytest.skip("root can read any file, whatever its mode")
    secret = instance / "secrets" / "telegram_bot_token"
    for name in FILLED:
        (instance / "secrets" / name).write_text("filled\n", encoding="utf-8")
    secret.chmod(0o000)
    status, reason = _status(instance, "secrets")
    # The mode is wrong now, which is reported, but never an emptiness that is not there.
    assert status == FAIL
    assert "is still empty" not in reason
    assert "crashed" not in reason


def test_an_empty_secret_is_reported(instance: Path) -> None:
    (instance / "secrets" / "telegram_bot_token").write_text("", encoding="utf-8")
    status, reason = _status(instance, "secrets")
    assert status == FAIL
    assert "telegram_bot_token is still empty" in reason


def test_a_secret_with_only_a_newline_is_reported_as_empty(instance: Path) -> None:
    (instance / "secrets" / "telegram_bot_token").write_text("\n", encoding="utf-8")
    status, reason = _status(instance, "secrets")
    assert status == FAIL
    assert "telegram_bot_token is still empty" in reason


def test_a_secret_owned_by_another_uid_fails_the_secrets_check(
    instance: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(os, "geteuid", lambda: 4242)
    # The files belong to whoever runs the tests, which may be one of the container uids.
    monkeypatch.setattr(check, "CONTAINER_UIDS", (4243,))
    status, reason = _status(instance, "secrets")
    assert status == FAIL
    assert "owned by uid" in reason


# --- brain_tools listening -------------------------------------------------------------------


def test_a_loopback_listen_address_fails(instance: Path) -> None:
    _edit_yaml(instance / "config/brain-tools.yaml", lambda data: data["listen"].update(host="127.0.0.1"))
    status, reason = _status(instance, "tools.listen")
    assert status == FAIL
    assert "127.0.0.1" in reason


def test_listening_on_every_interface_passes(instance: Path) -> None:
    _edit_yaml(instance / "config/brain-tools.yaml", lambda data: data["listen"].update(host="0.0.0.0"))
    status, _ = _status(instance, "tools.listen")
    assert status == PASS


def test_a_check_that_raises_becomes_a_failure(instance: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(ctx: object) -> CheckResult:
        raise RuntimeError("boom")

    monkeypatch.setattr("second_brain.cli.check._files_check", boom)
    results = _results(instance)
    assert results["files"].status == FAIL
    assert results["files"].reason == "crashed: boom"
    # Every other check still reports its own line.
    assert results["placeholders"].status == PASS


# --- compose --------------------------------------------------------------------------------


def test_a_read_write_vault_in_hermes_fails(instance: Path) -> None:
    _edit_text(instance / "compose.yaml", ":/vault:ro", ":/vault")
    status, reason = _status(instance, "compose.vault-read-only")
    assert status == FAIL
    assert "hermes" in reason


def test_the_vault_must_be_writable_by_brain_tools(instance: Path) -> None:
    _edit_text(instance / "compose.yaml", ":/vault\n", ":/vault:ro\n")
    status, reason = _status(instance, "compose.vault-read-only")
    assert status == FAIL
    assert "brain-tools" in reason


def test_a_docker_socket_in_hermes_fails(instance: Path) -> None:
    socket_mount = "      - /var/run/docker.sock:/var/run/docker.sock\n"
    _edit_text(
        instance / "compose.yaml",
        "      - guard:/guard\n      - hermes-home:/opt/data",
        f"      - guard:/guard\n{socket_mount}      - hermes-home:/opt/data",
    )
    status, reason = _status(instance, "compose.docker-socket")
    assert status == FAIL
    assert "hermes" in reason


def test_a_docker_socket_in_the_watchdog_fails(instance: Path) -> None:
    _edit_text(
        instance / "compose.yaml",
        "      - guard:/guard:ro\n",
        "      - guard:/guard:ro\n      - /var/run/docker.sock:/var/run/docker.sock\n",
    )
    status, reason = _status(instance, "compose.docker-socket")
    assert status == FAIL
    assert "watchdog" in reason


def test_no_docker_socket_passes(instance: Path) -> None:
    status, reason = _status(instance, "compose.docker-socket")
    assert status == PASS, reason


def test_a_published_port_fails(instance: Path) -> None:
    _edit_text(
        instance / "compose.yaml",
        "    networks: [terminal-net]\n    environment:\n      WORKSPACE:",
        '    networks: [terminal-net]\n    ports: ["2222:2222"]\n    environment:\n      WORKSPACE:',
    )
    status, reason = _status(instance, "compose.no-published-ports")
    assert status == FAIL
    assert "terminal" in reason


def test_an_internal_network_that_is_not_internal_fails(instance: Path) -> None:
    _edit_text(instance / "compose.yaml", "  internal:\n    internal: true", "  internal: {}")
    status, reason = _status(instance, "compose.internal-network")
    assert status == FAIL
    assert "internal" in reason


def test_hermes_without_a_proxy_fails(instance: Path) -> None:
    _edit_text(instance / "compose.yaml", "      HTTPS_PROXY: http://egress:3128\n", "")
    status, reason = _status(instance, "compose.hermes-proxy")
    assert status == FAIL
    assert "HTTPS_PROXY" in reason


# --- the Hermes config ----------------------------------------------------------------------

HERMES_CONFIG = "config/hermes/config.yaml"


def test_approvals_mode_smart_fails(instance: Path) -> None:
    _edit_text(instance / HERMES_CONFIG, "  mode: manual", "  mode: smart")
    status, reason = _status(instance, "hermes.approvals")
    assert status == FAIL
    assert "manual" in reason


def test_the_docker_terminal_backend_fails(instance: Path) -> None:
    _edit_text(instance / HERMES_CONFIG, "  backend: ssh", "  backend: docker")
    status, reason = _status(instance, "hermes.terminal")
    assert status == FAIL
    assert "docker" in reason


def _team_instance(tmp_path: Path, members: list[str], approvers: list[str] | None = None) -> Path:
    raw = yaml.safe_load(SAMPLE.read_text(encoding="utf-8"))
    del raw["owner_telegram_id"]
    raw["shape"] = "team"
    raw["slack"] = {
        "owner_id": "U0000001",
        "home_channel": "C0000001",
        "members": members,
        "approvers": approvers or [],
    }
    raw["vault_dir"] = str(tmp_path / "vault")
    raw["media_dir"] = str(tmp_path / "media")
    raw["archive_dir"] = str(tmp_path / "archive")
    answers_file = tmp_path / "answers.yaml"
    answers_file.write_text(yaml.safe_dump(raw), encoding="utf-8")
    directory = tmp_path / "team-instance"
    init(Answers.from_yaml(answers_file), directory)
    return directory


def test_a_declared_member_in_direct_messages_passes(tmp_path: Path) -> None:
    status, reason = _status(_team_instance(tmp_path, ["U0000002"]), "hermes.owner")
    assert status == PASS
    assert "1 members" in reason


def test_a_member_in_the_channel_list_fails(tmp_path: Path) -> None:
    directory = _team_instance(tmp_path, ["U0000002"])
    _edit_text(
        directory / HERMES_CONFIG,
        '    group_allow_from: ["U0000001"]',
        '    group_allow_from: ["U0000001", "U0000002"]',
    )
    status, reason = _status(directory, "hermes.owner")
    assert status == FAIL
    assert "group_allow_from" in reason


def test_an_approver_in_direct_messages_passes(tmp_path: Path) -> None:
    status, reason = _status(_team_instance(tmp_path, ["U0000002"], ["U0000003"]), "hermes.owner")
    assert status == PASS
    assert "1 approvers" in reason


def test_an_approver_made_admin_fails(tmp_path: Path) -> None:
    directory = _team_instance(tmp_path, [], ["U0000003"])
    _edit_text(
        directory / HERMES_CONFIG,
        '    allow_admin_from: ["U0000001"]',
        '    allow_admin_from: ["U0000001", "U0000003"]',
    )
    status, reason = _status(directory, "hermes.owner")
    assert status == FAIL
    assert "allow_admin_from" in reason


def test_a_member_the_guard_does_not_know_fails(tmp_path: Path) -> None:
    directory = _team_instance(tmp_path, ["U0000002"])
    _edit_yaml(directory / "config/brain-guard.yaml", lambda data: data.update(members=[]))
    status, reason = _status(directory, "hermes.owner")
    assert status == FAIL
    assert "U0000002" in reason


def test_an_extra_allowed_user_fails(instance: Path) -> None:
    _edit_text(
        instance / HERMES_CONFIG,
        '    allow_from: ["100000001"]',
        '    allow_from: ["100000001", "100000002"]',
    )
    status, reason = _status(instance, "hermes.owner")
    assert status == FAIL
    assert "100000002" in reason


def test_an_extra_admin_in_the_guard_config_fails(instance: Path) -> None:
    _edit_yaml(instance / "config/brain-guard.yaml", lambda data: data["admin_senders"].append("100000002"))
    status, reason = _status(instance, "hermes.owner")
    assert status == FAIL
    assert "admin senders" in reason


def test_a_disabled_plugin_fails(instance: Path) -> None:
    _edit_text(instance / HERMES_CONFIG, "  enabled: [brain-guard]", "  enabled: []")
    status, reason = _status(instance, "hermes.plugin")
    assert status == FAIL
    assert "brain-guard" in reason


def test_a_research_route_that_can_reach_the_vault_fails(instance: Path) -> None:
    _edit_text(
        instance / HERMES_CONFIG,
        "          toolsets: [web, no_mcp]",
        "          toolsets: [web, no_mcp, file, brain]",
    )
    status, reason = _status(instance, "hermes.research-routes")
    assert status == FAIL
    assert "background-research" in reason
    assert "file" in reason


def test_a_research_route_without_no_mcp_fails(instance: Path) -> None:
    _edit_text(
        instance / HERMES_CONFIG,
        "          toolsets: [web, no_mcp]",
        "          toolsets: [web]",
    )
    status, reason = _status(instance, "hermes.research-routes")
    assert status == FAIL
    assert "no_mcp" in reason


# --- the guard table and squid --------------------------------------------------------------


def test_a_tool_missing_from_the_guard_config_fails(instance: Path) -> None:
    _edit_yaml(instance / "config/brain-guard.yaml", lambda data: data["mcp_tools"].pop("vault_write"))
    status, reason = _status(instance, "guard.tools")
    assert status == FAIL
    assert "vault_write" in reason


def test_a_tool_the_guard_knows_but_the_brain_does_not_fails(instance: Path) -> None:
    def add(data: dict) -> None:
        data["mcp_tools"]["vault_teleport"] = {
            "effect": "read",
            "autonomy": "auto",
            "taints_untrusted": False,
            "taints_private": False,
        }

    _edit_yaml(instance / "config/brain-guard.yaml", add)
    status, reason = _status(instance, "guard.tools")
    assert status == FAIL
    assert "vault_teleport" in reason


def test_a_registry_allowed_in_squid_fails(instance: Path) -> None:
    _edit_text(instance / "config/squid.conf", "acl package_registries dstdomain pypi.org\n", "")
    status, reason = _status(instance, "squid.registries")
    assert status == FAIL
    assert "pypi.org" in reason


def test_a_registry_deny_after_the_allowlist_fails(instance: Path) -> None:
    squid = instance / "config/squid.conf"
    text = squid.read_text(encoding="utf-8")
    deny = "http_access deny package_registries\n"
    assert deny in text
    squid.write_text(text.replace(deny, "", 1) + "\n" + deny, encoding="utf-8")
    status, reason = _status(instance, "squid.registries")
    assert status == FAIL
    assert "after an allow line" in reason


# --- the command and the live checks --------------------------------------------------------


def test_the_command_exits_one_while_the_secrets_are_empty(
    instance: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["check", str(instance)]) == 1
    printed = capsys.readouterr().out
    assert "FAIL" in printed
    assert "secrets" in printed
    assert "passed" in printed


def test_the_command_exits_zero_when_everything_passes(
    instance: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    for name in FILLED:
        (instance / "secrets" / name).write_text("filled\n", encoding="utf-8")
    assert main(["check", str(instance)]) == 0
    printed = capsys.readouterr().out
    assert "FAIL" not in printed
    assert "0 failed" in printed


def test_a_missing_instance_directory_is_refused(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["check", str(tmp_path / "nope")]) == 2
    assert "not a directory" in capsys.readouterr().err


def test_a_broken_yaml_still_reports_one_line_per_check(
    instance: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (instance / "compose.yaml").write_text("services: [\n", encoding="utf-8")
    assert main(["check", str(instance)]) == 1
    lines = [line for line in capsys.readouterr().out.splitlines() if line.strip()]
    assert all(line.startswith((PASS.upper(), FAIL.upper(), SKIP.upper())) for line in lines[:-1])
    assert lines[-1].endswith("failed")


def test_live_checks_are_skipped_without_docker(instance: Path) -> None:
    results = live_checks(instance, docker="")
    assert [result.status for result in results] == [SKIP]
    assert "docker" in results[0].reason


def _fake_runner(calls: list[list[str]], ps: str, codes: dict[str, int] | None = None):
    def run(argv):
        calls.append(list(argv))
        joined = " ".join(argv)
        code = 0
        for marker, value in (codes or {}).items():
            if marker in joined:
                code = value
        stdout = ps if "ps" in argv else ""
        return subprocess.CompletedProcess(list(argv), code, stdout=stdout, stderr="")

    return run


def _ps_for_every_service(instance: Path) -> str:
    """A healthy `ps` row for every service compose.yaml declares."""
    declared = yaml.safe_load((instance / "compose.yaml").read_text(encoding="utf-8"))["services"]
    return "".join(
        f'{{"Service":"{service}","State":"running","Health":"healthy"}}\n' for service in declared
    )


def test_live_checks_pass_against_a_healthy_stack(instance: Path) -> None:
    calls: list[list[str]] = []
    ps = _ps_for_every_service(instance)
    codes = {"sb-check": 3, "docker.sock": 0, "heartbeat.json": 0}
    results = live_checks(instance, runner=_fake_runner(calls, ps, codes), docker="docker")
    statuses = {result.name: result.status for result in results}
    assert statuses["live.services"] == PASS
    assert statuses["live.vault-read-only"] == PASS
    assert statuses["live.no-docker-socket"] == PASS
    assert statuses["live.heartbeat"] == PASS
    # The gate's stale-heartbeat behaviour stops Hermes, so it is never exercised live.
    assert statuses["live.gate-stale-heartbeat"] == SKIP
    assert calls[0][:4] == ["docker", "compose", "-f", str(instance.resolve() / "compose.yaml")]


def test_live_services_report_a_declared_service_without_a_container(instance: Path) -> None:
    # An image that could not be pulled leaves no container behind, so the service is simply
    # absent from `ps`.
    calls: list[list[str]] = []
    ps = "".join(
        line for line in _ps_for_every_service(instance).splitlines(keepends=True) if '"searxng"' not in line
    )
    results = live_checks(instance, runner=_fake_runner(calls, ps, {}), docker="docker")
    services = next(result for result in results if result.name == "live.services")
    assert services.status == FAIL
    assert "searxng has no container" in services.reason


def test_live_checks_fail_when_the_vault_is_writable_and_the_socket_is_there(instance: Path) -> None:
    calls: list[list[str]] = []
    ps = '{"Service":"hermes","State":"running","Health":"healthy"}\n'
    codes = {"sb-check": 0, "docker.sock": 3, "heartbeat.json": 3}
    results = live_checks(instance, runner=_fake_runner(calls, ps, codes), docker="docker")
    statuses = {result.name: result.status for result in results}
    assert statuses["live.vault-read-only"] == FAIL
    assert statuses["live.no-docker-socket"] == FAIL
    assert statuses["live.heartbeat"] == FAIL


def test_live_services_fail_on_an_unhealthy_or_stopped_service(instance: Path) -> None:
    calls: list[list[str]] = []
    ps = (
        '{"Service":"hermes","State":"running","Health":"starting"}\n'
        '{"Service":"gate","State":"exited","Health":""}\n'
    )
    results = live_checks(instance, runner=_fake_runner(calls, ps, {}), docker="docker")
    services = next(result for result in results if result.name == "live.services")
    assert services.status == FAIL
    assert "hermes" in services.reason
    assert "gate" in services.reason


def test_live_checks_fail_when_compose_cannot_be_read(instance: Path) -> None:
    calls: list[list[str]] = []
    results = live_checks(instance, runner=_fake_runner(calls, "", {"ps": 1}), docker="docker")
    services = next(result for result in results if result.name == "live.services")
    assert services.status == FAIL
    assert "exited 1" in services.reason


# --- the sidecars and the scheduled routes --------------------------------------------------


def test_a_fresh_instance_passes_the_sidecar_checks(instance: Path) -> None:
    for name in ("sidecars_config", "sidecar_routes", "team_domains", "scheduled_routes"):
        status, reason = _status(instance, name)
        assert status == PASS, (name, reason)


def test_a_missing_sidecars_file_fails(instance: Path) -> None:
    (instance / "config/sidecars.yaml").unlink()
    status, reason = _status(instance, "sidecars_config")
    assert status == FAIL
    assert "sidecars.yaml" in reason
    assert _status(instance, "files")[0] == FAIL
    assert "config/sidecars.yaml" in _status(instance, "files")[1]


def test_a_route_removed_from_hermes_fails(instance: Path) -> None:
    _edit_yaml(
        instance / "config/hermes/config.yaml",
        lambda data: data["platforms"]["webhook"]["extra"]["routes"].pop("scheduled-lint"),
    )
    status, reason = _status(instance, "sidecar_routes")
    assert status == FAIL
    assert "scheduled-lint" in reason


def test_a_route_whose_secret_file_is_missing_fails(instance: Path) -> None:
    (instance / "secrets" / "webhook_idea").unlink()
    status, reason = _status(instance, "sidecar_routes")
    assert status == FAIL
    assert "background-idea" in reason
    assert "webhook_idea" in reason


def test_a_team_domain_fails(instance: Path) -> None:
    _edit_yaml(
        instance / "config/sidecars.yaml",
        lambda data: data["call_router"]["rules"].update({"team_domains": ["example.com"]}),
    )
    status, reason = _status(instance, "team_domains")
    assert status == FAIL
    assert "team calls would be lost" in reason


def test_a_team_brain_may_name_its_own_team_domains(tmp_path: Path) -> None:
    """A team brain is where its team calls go, so the domains are how it tells them from a 1:1."""
    data = yaml.safe_load(SAMPLE.read_text(encoding="utf-8"))
    data["vault_dir"] = str(tmp_path / "vault")
    data["media_dir"] = str(tmp_path / "media")
    data["archive_dir"] = str(tmp_path / "archive")
    data["shape"] = "team"
    data["instance_name"] = "brain-team"
    data["calls"] = {"team_domains": ["example.com"]}
    path = tmp_path / "team.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    directory = tmp_path / "team-instance"
    init(Answers.from_yaml(path), directory)
    status, reason = _status(directory, "team_domains")
    assert status == PASS, reason
    assert "example.com" in reason


def test_a_scheduled_route_that_reaches_the_web_fails(instance: Path) -> None:
    _edit_yaml(
        instance / "config/hermes/config.yaml",
        lambda data: data["platforms"]["webhook"]["extra"]["routes"]["scheduled-lint"]["toolsets"].append(
            "web"
        ),
    )
    status, reason = _status(instance, "scheduled_routes")
    assert status == FAIL
    assert "scheduled-lint reaches the web" in reason


def test_a_scheduled_route_that_mirrors_its_reply_fails(instance: Path) -> None:
    _edit_yaml(
        instance / "config/hermes/config.yaml",
        lambda data: data["platforms"]["webhook"]["extra"]["routes"]["scheduled-brief"].update(
            {"mirror_to_session": True}
        ),
    )
    status, reason = _status(instance, "scheduled_routes")
    assert status == FAIL
    assert "scheduled-brief mirrors its reply" in reason


def test_only_the_digest_may_deliver(instance: Path) -> None:
    _edit_yaml(
        instance / "config/hermes/config.yaml",
        lambda data: data["platforms"]["webhook"]["extra"]["routes"]["scheduled-brief"].update(
            {"deliver": "telegram"}
        ),
    )
    status, reason = _status(instance, "scheduled_routes")
    assert status == FAIL
    assert "only scheduled-digest may" in reason


def test_a_scheduled_route_outside_the_known_ones_fails(instance: Path) -> None:
    def mutate(data: dict) -> None:
        entries = data["scheduler"]["schedule"]["entries"]
        entries.append({"name": "research", "route": "background-research", "at": "09:00"})

    _edit_yaml(instance / "config/sidecars.yaml", mutate)
    status, reason = _status(instance, "scheduled_routes")
    assert status == FAIL
    assert "background-research is not one of" in reason


def test_the_placeholders_check_never_reads_the_secrets(instance: Path) -> None:
    # After owners.sh the secrets belong to container uids: the operator cannot read hermes.env,
    # and a template placeholder in there is not a thing that can happen anyway.
    env = instance / "secrets/hermes.env"
    env.write_text(env.read_text(encoding="utf-8") + "SIGNED={{ owner_name }}\n", encoding="utf-8")
    env.chmod(0o000)
    status, reason = _status(instance, "placeholders")
    assert status == PASS, reason


def test_a_git_object_in_the_vault_is_not_a_template_placeholder(instance: Path) -> None:
    # The vault is a git repository from its first commit, and a binary git index can hold `{%`.
    git_dir = instance / "vault" / ".git"
    git_dir.mkdir(parents=True, exist_ok=True)
    (git_dir / "index").write_bytes(b"DIRC\x00\x00{%\x00binary")
    status, reason = _status(instance, "placeholders")
    assert status == PASS, reason


def test_a_binary_file_in_the_vault_is_not_a_template_placeholder(instance: Path) -> None:
    # A PDF in the archive can hold the marker bytes by chance; only text is rendered from a template.
    pdf = instance / "vault" / "archivio" / "contract.pdf"
    pdf.parent.mkdir(parents=True, exist_ok=True)
    pdf.write_bytes(b"%PDF-1.4\nstream\n{{\\binary\nendstream\n")
    status, reason = _status(instance, "placeholders")
    assert status == PASS, reason


# --- the skills an instance schedules by name ------------------------------------------------


def _scheduled_instance(tmp_path: Path, skills: dict[str, list[str]]) -> Path:
    data = yaml.safe_load(SAMPLE.read_text(encoding="utf-8"))
    data["vault_dir"] = str(tmp_path / "vault")
    data["media_dir"] = str(tmp_path / "media")
    data["archive_dir"] = str(tmp_path / "archive")
    data["schedule"] = {
        "digest": ["08:00"],
        "lint": "03:30",
        "brief": ["07:00"],
        "triage": ["21:00"],
        "skills": skills,
    }
    path = tmp_path / "scheduled.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    directory = tmp_path / "scheduled-instance"
    init(Answers.from_yaml(path), directory)
    return directory


def test_a_skill_the_instance_schedules_passes_the_scheduled_routes(tmp_path: Path) -> None:
    directory = _scheduled_instance(tmp_path, {"dailies": ["03:00"]})
    status, reason = _status(directory, "scheduled_routes")
    assert status == PASS, reason
    assert "scheduled-dailies" in reason


def test_a_scheduled_route_without_a_hermes_block_fails(tmp_path: Path) -> None:
    directory = _scheduled_instance(tmp_path, {"dailies": ["03:00"]})

    def mutate(data: dict) -> None:
        entries = data["scheduler"]["schedule"]["entries"]
        entries.append({"name": "foo", "route": "scheduled-foo", "at": "09:00"})
        # The sidecars config refuses a route with no secret, so the secret is there: what is
        # missing is the Hermes block, which is what this check is about.
        data["route_secrets"]["scheduled-foo"] = "/run/secrets/webhook_schedule"

    _edit_yaml(directory / "config/sidecars.yaml", mutate)
    status, reason = _status(directory, "scheduled_routes")
    assert status == FAIL
    assert "scheduled-foo is not one of" in reason


# --- the stack ceiling ----------------------------------------------------------------------

SLICE = "brain-team.slice"


def _sliced_instance(tmp_path: Path, name: str = "sliced-instance") -> Path:
    raw = yaml.safe_load(SAMPLE.read_text(encoding="utf-8"))
    raw["vault_dir"] = str(tmp_path / "vault")
    raw["media_dir"] = str(tmp_path / "media")
    raw["archive_dir"] = str(tmp_path / "archive")
    raw["stack_limits"] = {"slice": SLICE, "memory_mb": 4096, "cpus": 2}
    answers_file = tmp_path / "answers.yaml"
    answers_file.write_text(yaml.safe_dump(raw), encoding="utf-8")
    directory = tmp_path / name
    init(Answers.from_yaml(answers_file), directory)
    return directory


def test_a_brain_without_a_ceiling_passes_the_slice_check(instance: Path) -> None:
    status, reason = _status(instance, "compose.slice")
    assert status == PASS
    assert "no stack ceiling" in reason


def test_a_brain_under_a_slice_passes_the_slice_check(tmp_path: Path) -> None:
    status, reason = _status(_sliced_instance(tmp_path), "compose.slice")
    assert status == PASS
    assert SLICE in reason


def test_a_service_outside_the_slice_fails(tmp_path: Path) -> None:
    directory = _sliced_instance(tmp_path)
    _edit_yaml(directory / "compose.yaml", lambda data: data["services"]["hermes"].pop("cgroup_parent"))
    status, reason = _status(directory, "compose.slice")
    assert status == FAIL
    assert "hermes" in reason


def test_a_personal_brain_with_a_handoff_may_name_the_team_domains(tmp_path: Path) -> None:
    data = yaml.safe_load(SAMPLE.read_text(encoding="utf-8"))
    data["vault_dir"] = str(tmp_path / "vault")
    data["media_dir"] = str(tmp_path / "media")
    data["archive_dir"] = str(tmp_path / "archive")
    data["calls"] = {"team_domains": ["example.com"], "team_markers": ["ds9"]}
    data["handoff"] = {"host_dir": str(tmp_path / "handoff")}
    path = tmp_path / "personal.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    directory = tmp_path / "personal-instance"
    init(Answers.from_yaml(path), directory)
    status, reason = _status(directory, "team_domains")
    assert status == PASS, reason
    assert "team_handoff" in reason
