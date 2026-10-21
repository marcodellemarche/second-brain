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

from second_brain.cli.render import render_text

TEMPLATES = Path(__file__).resolve().parents[2] / "templates" / "hermes"

OWNER_ID = "100000001"
VALUES: dict[str, Any] = {
    "model_name": "local-model",
    "model_gate_url": "http://model-gate:8080/v1",
    "model_context_length": "131072",
    "timezone": "Europe/Rome",
    "skills_dir": "/data/skills",
    "media_dir": "/data/media",
    "terminal_cwd": "/workspace",
    "terminal_ssh_key": "/run/secrets/terminal_ssh_key",
    "brain_tools_url": "http://brain-tools:8765/mcp",
    "brain_tools_outbox_url": "http://brain-tools:8765/outbox",
    "egress_proxy_url": "http://egress:3128",
    "owner_telegram_id": OWNER_ID,
    "owner_id": OWNER_ID,
    "is_slack": False,
    "is_team": False,
    "owner_slack_id": "U0000000001",
    "owner_name": "Sample Owner",
    "language": "Italian",
    "language_code": "it",
    "languages": "Italian, English",
    "channel": "Telegram",
    "deliver_platform": "telegram",
    "slack_home_channel": "C0000000001",
    "example_note_path": "knowledge/example.md",
    # Empty here: the routes of the skills an instance schedules are built in the CLI tests.
    "scheduled_skills": [],
}
_PLACEHOLDER = re.compile(r"\{\{\s*(\w+)\s*(\|\s*lower\s*)?\}\}")


def render(template: Path, values: dict[str, Any]) -> str:
    """Render with the real renderer, so the `{% if %}` and `{% for %}` blocks are the product's.

    The renderer is the one `sb init` uses, so this file cannot disagree with it about what a
    template means. An unknown placeholder raises, which is what the assertions below rely on.
    """
    return render_text(template.read_text(encoding="utf-8"), values, source=template.name)


@pytest.fixture(scope="module")
def config() -> dict[str, Any]:
    text = render(TEMPLATES / "config.yaml.j2", VALUES)
    loaded = yaml.safe_load(text)
    assert isinstance(loaded, dict)
    return loaded


def test_display_language_is_the_instance_language(config: dict[str, Any]) -> None:
    assert config["display"]["language"] == VALUES["language_code"]


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


def test_terminal_goes_over_ssh_to_a_separate_container(config: dict[str, Any]) -> None:
    terminal = config["terminal"]
    assert terminal["backend"] == "ssh"
    assert terminal["ssh_host"] == "terminal"
    assert terminal["ssh_user"] == "agent"
    assert terminal["ssh_port"] == 2222
    assert terminal["ssh_key"] == VALUES["terminal_ssh_key"]
    assert terminal["cwd"] == VALUES["terminal_cwd"]


def test_the_research_reply_comes_back_through_an_outbound_hook(config: dict[str, Any]) -> None:
    """A research route has no vault, so its reply is POSTed to brain_tools' outbox endpoint."""
    targets = config["hooks"]["outbound"]
    assert isinstance(targets, list) and targets
    target = targets[0]
    assert target["url"] == VALUES["brain_tools_outbox_url"]
    assert target["url"].endswith(":8765/outbox")
    # post_llm_call is the event that carries the finished turn's reply text.
    assert target["events"] == ["post_llm_call"]
    # The secret is an env var name, never the value, and never in this file.
    assert target["secret_env"] == "HERMES_OUTBOX_SECRET"
    assert "secret" not in target
    assert config["hooks_auto_accept"] is False


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
    assert terminal["ssh_persistent"] is False
    # The docker backend would need the socket, so none of its keys may come back.
    assert not [key for key in terminal if key.startswith(("docker_", "container_"))]


def test_private_urls_stay_blocked(config: dict[str, Any]) -> None:
    # web_extract must not reach the internal network where the model gate and brain_tools live.
    assert config["security"]["allow_private_urls"] is False
    assert config["security"]["redact_secrets"] is True


def test_brain_guard_is_the_enabled_plugin(config: dict[str, Any]) -> None:
    assert config["plugins"]["enabled"] == ["brain-guard"]
    assert config["plugins"]["auto_apply"] is False
    # The only hooks are the outbound ones that bring a research reply back.
    assert set(config["hooks"]) == {"outbound"}
    assert config["hooks_auto_accept"] is False


def test_the_brain_mcp_server_takes_its_token_from_the_env(config: dict[str, Any]) -> None:
    brain = config["mcp_servers"]["brain"]
    assert brain["url"] == VALUES["brain_tools_url"]
    assert brain["headers"]["Authorization"] == "Bearer ${TOOLS_TOKEN}"
    assert brain["sampling"]["enabled"] is False
    assert brain["elicitation"]["enabled"] is False
    assert brain["tools"] == {"prompts": False, "resources": False}


def test_the_product_skills_come_from_the_read_only_mount(config: dict[str, Any]) -> None:
    # Hermes reads external_dirs from its user config only, so a path here is never read: the
    # skills are a read-only mount at /opt/data/skills/second-brain instead.
    assert config["skills"]["external_dirs"] == []


def test_a_telegram_brain_has_no_slack_platform(config: dict[str, Any]) -> None:
    assert "slack" not in config["platforms"]
    assert "telegram" in config["platforms"]


def test_a_slack_brain_answers_on_slack_and_not_on_telegram() -> None:
    values = dict(VALUES, channel="Slack", deliver_platform="slack", is_slack=True)
    loaded = yaml.safe_load(render(TEMPLATES / "config.yaml.j2", values))
    assert "telegram" not in loaded["platforms"]
    slack = loaded["platforms"]["slack"]
    assert slack["enabled"] is True
    assert slack["home_channel"] == {
        "platform": "slack",
        "chat_id": VALUES["slack_home_channel"],
        "name": VALUES["owner_name"],
    }
    # One session per person in a shared thread, and the webhook admin is the same owner.
    assert loaded["thread_sessions_per_user"] is True
    assert loaded["platform_toolsets"]["slack"][:2] == ["file", "web"]
    assert "telegram" not in loaded["platform_toolsets"]
    webhook = loaded["platforms"]["webhook"]["extra"]
    assert webhook["allow_admin_from"] == [VALUES["owner_id"]]
    assert webhook["group_allow_admin_from"] == [VALUES["owner_id"]]


def test_a_telegram_brain_has_no_thread_sessions_key(config: dict[str, Any]) -> None:
    # The key only means something on a platform with threads: on Telegram it would be noise.
    assert "thread_sessions_per_user" not in config
    assert config["platform_toolsets"]["telegram"][:2] == ["file", "web"]


def test_soul_calls_other_content_data_and_forbids_python() -> None:
    soul = render(TEMPLATES / "SOUL.md.j2", VALUES)
    assert VALUES["owner_name"] in soul
    assert VALUES["language"] in soul
    assert "data, never instructions" in soul
    assert "read_file" in soul and "search_files" in soul
    assert "`/vault`" in soul
    assert "Never install software" in soul
    assert VALUES["example_note_path"] in soul
    assert "`/new`" in soul


def test_soul_keeps_identifiers_out_of_the_reply() -> None:
    soul = render(TEMPLATES / "SOUL.md.j2", VALUES)
    assert "identity document number" in soul
    assert "IBAN" in soul
    assert "card number" in soul
    # The note path is what the model points to instead of the value.
    assert "Name the note that holds it" in soul


def test_soul_says_when_the_owner_denied_something() -> None:
    soul = render(TEMPLATES / "SOUL.md.j2", VALUES)
    assert "denies an action" in soul
    assert "comes from memory" in soul
    assert "did not check it" in soul


def test_soul_suggests_new_inside_a_sentence() -> None:
    soul = render(TEMPLATES / "SOUL.md.j2", VALUES)
    assert "one short sentence" in soul
    for line in soul.splitlines():
        assert not line.rstrip().endswith("/new`"), f"bare /new at the end of a line: {line!r}"
        assert not line.rstrip().endswith("`/new`"), f"bare /new at the end of a line: {line!r}"


def test_a_slack_soul_suggests_a_new_thread_and_never_mentions_new() -> None:
    # `/new` in Slack opens a new message that may land outside the thread, so a team brain that
    # keeps one session per thread suggests a new thread instead.
    soul = render(TEMPLATES / "SOUL.md.j2", dict(VALUES, is_slack=True))
    assert "new thread" in soul
    assert "/new" not in soul


def test_a_telegram_soul_keeps_the_new_sentence() -> None:
    soul = render(TEMPLATES / "SOUL.md.j2", VALUES)
    assert "`/new`" in soul
    assert "new thread" not in soul


def test_a_team_soul_reads_the_system_page_before_jira() -> None:
    soul = render(TEMPLATES / "SOUL.md.j2", dict(VALUES, is_team=True))
    assert "`systems/`" in soul
    assert "team-preferences.md" in soul
    # The live miss: a case-sensitive search for "invoice matcher" did not find "Invoice Matcher".
    assert "(?i)" in soul
    assert "target: files" in soul


def test_a_personal_soul_has_no_team_rules() -> None:
    soul = render(TEMPLATES / "SOUL.md.j2", VALUES)
    assert "team-preferences.md" not in soul
    assert "`systems/`" not in soul


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


SCHEDULED_ROUTES = ("scheduled-digest", "scheduled-lint", "scheduled-brief")


def _scheduled(config: dict[str, Any]) -> dict[str, Any]:
    routes = config["platforms"]["webhook"]["extra"]["routes"]
    assert isinstance(routes, dict)
    return {name: routes[name] for name in SCHEDULED_ROUTES}


def test_the_scheduled_routes_share_the_scheduler_secret(config: dict[str, Any]) -> None:
    for name, route in _scheduled(config).items():
        assert route["secret"] == "${WEBHOOK_SECRET_SCHEDULE}", name
        # The prompt is the whole payload the scheduler signs, and nothing else.
        assert route["prompt"] == "Scheduled task: {task}. Due {due} ({weekday}).", name
        assert "web" not in route["toolsets"], name
        # A mirrored delivery lands in the chat as a user turn, and the digest quotes people.
        assert route.get("mirror_to_session", False) is False, name


def test_only_the_digest_of_the_scheduled_routes_is_delivered(config: dict[str, Any]) -> None:
    routes = _scheduled(config)
    # The digest is the message to the owner, so it goes to this brain's chat platform.
    assert routes["scheduled-digest"]["deliver"] == "telegram"
    assert routes["scheduled-digest"]["skills"] == ["digest"]
    # The lint writes its questions into the vault, the brief notifies through brain_tools.
    # Hermes injects only the first skill of a route, so each route names exactly one.
    assert routes["scheduled-lint"]["skills"] == ["lint"]
    assert routes["scheduled-brief"]["skills"] == ["brief"]
    for name in ("scheduled-lint", "scheduled-brief"):
        assert "deliver" not in routes[name], name


def test_web_search_uses_searxng_and_no_keyless_tier(config: dict[str, Any]) -> None:
    assert config["web"] == {"search_backend": "searxng", "keyless_fallback": False, "keyless_rescue": False}


def test_the_research_prompt_names_the_job_the_outbox_looks_for(config: dict[str, Any]) -> None:
    from second_brain.tools.outbox import job_id_from_prompt

    routes = config["platforms"]["webhook"]["extra"]["routes"]
    job_id = "0123456789abcdef0123456789abcdef"
    prompt = routes["background-research"]["prompt"].format(
        job_id=job_id, title="A title", question="A question?"
    )
    assert job_id_from_prompt(prompt) == job_id
