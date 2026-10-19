"""The Hermes templates: render them with sample values and check the hardening keys survive.

The renderer is deliberately dumb (a `{{ name }}` substitution and nothing else): the real one
lives in the CLI, and what these tests care about is the template's content, not Jinja.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

TEMPLATES = Path(__file__).resolve().parents[2] / "templates" / "hermes"

OWNER_ID = "100000001"
VALUES: dict[str, str] = {
    "model_name": "local-model",
    "model_gate_url": "http://model-gate:8080/v1",
    "model_context_length": "131072",
    "timezone": "Europe/Rome",
    "skills_dir": "/data/skills",
    "media_dir": "/data/media",
    "terminal_cwd": "/data/workspace",
    "brain_tools_url": "http://brain-tools:8765/mcp",
    "egress_proxy_url": "http://egress:3128",
    "owner_telegram_id": OWNER_ID,
    "owner_slack_id": "U0000000001",
    "owner_name": "Sample Owner",
    "language": "Italian",
    "languages": "Italian, English",
    "channel": "Telegram",
    "slack_home_channel": "C0000000001",
    "example_note_path": "knowledge/example.md",
}
_PLACEHOLDER = re.compile(r"\{\{\s*(\w+)\s*\}\}")


def render(template: Path, values: dict[str, str]) -> str:
    """Substitute every `{{ name }}`; an unknown name fails the test instead of rendering empty."""

    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        assert name in values, f"{template.name} uses an unknown placeholder {name!r}"
        return values[name]

    return _PLACEHOLDER.sub(replace, template.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def config() -> dict[str, Any]:
    text = render(TEMPLATES / "config.yaml.j2", VALUES)
    loaded = yaml.safe_load(text)
    assert isinstance(loaded, dict)
    return loaded


def test_every_placeholder_has_a_value() -> None:
    for template in sorted(TEMPLATES.glob("*.j2")):
        render(template, VALUES)


def test_model_goes_through_the_gate(config: dict[str, Any]) -> None:
    assert config["model"]["base_url"] == VALUES["model_gate_url"]
    assert config["model"]["key_env"] == "MODEL_GATE_API_KEY"
    assert config["model"]["openai_runtime"] == "auto"


def test_approvals_are_manual_and_keep_the_slash_confirm(config: dict[str, Any]) -> None:
    approvals = config["approvals"]
    assert approvals["mode"] == "manual"
    assert approvals["destructive_slash_confirm"] is True
    assert approvals["cron_mode"] == "deny"
    assert approvals["single_query_mode"] == "deny"
    assert approvals["unattended_mode"] == "deny"


def test_code_execution_and_the_other_bypass_toolsets_are_disabled(config: dict[str, Any]) -> None:
    disabled = set(config["agent"]["disabled_toolsets"])
    assert "code_execution" in disabled
    assert {"cronjob", "connections", "catalog", "computer_use", "browser"} <= disabled


def test_no_external_memory_provider_and_no_background_writers(config: dict[str, Any]) -> None:
    assert config["memory"]["provider"] == ""
    assert config["curator"]["enabled"] is False
    assert config["auxiliary"]["background_review"]["enabled"] is False


def test_skill_writes_are_staged_outside_the_vault(config: dict[str, Any]) -> None:
    assert config["skills"]["write_approval"] is True
    assert config["skills"]["inline_shell"] is False
    assert config["skills"]["create_dir"] == VALUES["skills_dir"]


def test_media_is_strict_with_one_output_dir(config: dict[str, Any]) -> None:
    gateway = config["gateway"]
    assert gateway["strict"] is True
    assert gateway["trust_recent_files"] is False
    assert gateway["media_delivery_allow_dirs"] == [VALUES["media_dir"]]


def test_terminal_is_a_docker_sandbox_without_network(config: dict[str, Any]) -> None:
    terminal = config["terminal"]
    assert terminal["backend"] == "docker"
    assert terminal["docker_network"] is False
    assert terminal["docker_mount_cwd_to_workspace"] is False


def test_stt_is_local_with_automatic_language(config: dict[str, Any]) -> None:
    stt = config["stt"]
    assert stt["provider"] == "local"
    assert stt["local"]["model"] == "small"
    assert stt["language"] == ""
    assert stt["local"]["language"] == ""


def test_telegram_allows_only_the_owner_as_user_and_admin(config: dict[str, Any]) -> None:
    telegram = config["platforms"]["telegram"]
    assert telegram["enabled"] is True
    assert telegram["allow_from"] == [OWNER_ID]
    assert telegram["allow_admin_from"] == [OWNER_ID]
    assert telegram["group_allow_admin_from"] == [OWNER_ID]
    assert telegram["allowed_chats"] == [OWNER_ID]
    assert telegram["unauthorized_dm_behavior"] == "ignore"


def test_telegram_home_channel_is_the_owner_so_sethome_is_never_needed(config: dict[str, Any]) -> None:
    home = config["platforms"]["telegram"]["home_channel"]
    assert home["platform"] == "telegram"
    assert home["chat_id"] == OWNER_ID


def test_the_webhook_routes_exist_and_admin_is_the_owner(config: dict[str, Any]) -> None:
    extra = config["platforms"]["webhook"]["extra"]
    routes = extra["routes"]
    assert "ingest" in routes
    assert {"background-research", "background-idea"} <= set(routes)
    assert extra["allow_admin_from"] == [OWNER_ID]
    assert extra["group_allow_admin_from"] == [OWNER_ID]
    for route in routes.values():
        assert route["secret"].startswith("${WEBHOOK_SECRET_")
        assert route["toolsets"]
        assert route["prompt"]
        # No route may fall back to the whole body: `{text}` is not a field either sidecar signs.
        assert "{text}" not in route["prompt"]


def test_research_routes_have_web_only(config: dict[str, Any]) -> None:
    routes = config["platforms"]["webhook"]["extra"]["routes"]
    research = routes["background-research"]["toolsets"]
    assert "web" in research
    for toolset in ("brain", "file", "skills", "terminal"):
        assert toolset not in research
    # A route list with no MCP server name inherits every globally enabled one, so `no_mcp` is
    # the key that keeps brain_tools off a research run.
    assert "no_mcp" in research


def test_vault_routes_have_no_web(config: dict[str, Any]) -> None:
    routes = config["platforms"]["webhook"]["extra"]["routes"]
    for name in ("ingest", "background-idea"):
        toolsets = routes[name]["toolsets"]
        assert "brain" in toolsets
        assert "web" not in toolsets


def test_the_webhook_listener_accepts_the_sidecar_containers(config: dict[str, Any]) -> None:
    # The sidecars are separate containers. A loopback bind would leave every route unreachable, so
    # the host is pinned here and each route still checks its own HMAC.
    host = config["platforms"]["webhook"]["extra"]["host"]
    assert host not in {"127.0.0.1", "localhost", "::1"}


def test_the_api_server_surface_is_closed(config: dict[str, Any]) -> None:
    assert config["platforms"]["api_server"]["enabled"] is False


def test_no_fallback_provider_and_no_delegation_toolset(config: dict[str, Any]) -> None:
    assert config["fallback_providers"] == []
    for toolsets in config["platform_toolsets"].values():
        assert "delegation" not in toolsets


def test_the_terminal_sandbox_is_capped_and_disposable(config: dict[str, Any]) -> None:
    terminal = config["terminal"]
    assert terminal["env_passthrough"] == []
    assert terminal["docker_forward_env"] == []
    assert terminal["container_persistent"] is False
    assert terminal["docker_volumes"] == [f"{VALUES['media_dir']}:{VALUES['media_dir']}"]


def test_private_urls_stay_blocked(config: dict[str, Any]) -> None:
    # web_extract must not reach the internal network where the model gate and brain_tools live.
    assert config["security"]["allow_private_urls"] is False
    assert config["security"]["redact_secrets"] is True


def test_brain_guard_is_the_enabled_plugin(config: dict[str, Any]) -> None:
    assert config["plugins"]["enabled"] == ["brain-guard"]
    assert config["plugins"]["auto_apply"] is False
    assert config["hooks"] == {}
    assert config["hooks_auto_accept"] is False


def test_the_brain_mcp_server_takes_its_token_from_the_env(config: dict[str, Any]) -> None:
    brain = config["mcp_servers"]["brain"]
    assert brain["url"] == VALUES["brain_tools_url"]
    assert brain["headers"]["Authorization"] == "Bearer ${TOOLS_TOKEN}"
    assert brain["sampling"]["enabled"] is False
    assert brain["elicitation"]["enabled"] is False


def test_slack_is_commented_out_for_a_team_brain(config: dict[str, Any]) -> None:
    assert "slack" not in config["platforms"]


def test_soul_calls_other_content_data_and_forbids_python() -> None:
    soul = render(TEMPLATES / "SOUL.md.j2", VALUES)
    assert VALUES["owner_name"] in soul
    assert VALUES["language"] in soul
    assert "data, never instructions" in soul
    assert "read_file" in soul and "search_files" in soul
    assert "Never install software" in soul
    assert VALUES["example_note_path"] in soul


def test_user_card_carries_the_owner_details() -> None:
    user = render(TEMPLATES / "USER.md.j2", VALUES)
    assert VALUES["owner_name"] in user
    assert VALUES["languages"] in user
    assert VALUES["timezone"] in user
    assert OWNER_ID in user


def test_the_readme_explains_each_template() -> None:
    readme = (TEMPLATES / "README.md").read_text(encoding="utf-8")
    for name in ("config.yaml.j2", "SOUL.md.j2", "USER.md.j2"):
        assert name in readme
