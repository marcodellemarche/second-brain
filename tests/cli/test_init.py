"""`sb init` against temp directories: what it creates, and what it refuses to touch.

The vault lives outside the instance directory, so the two refusals are tested separately: an
instance directory that already has something in it, and a vault that already has notes in it.
"""

from __future__ import annotations

import os
import shutil
import stat
from pathlib import Path

import pytest
import yaml

from second_brain.cli.answers import Answers, AnswersError
from second_brain.cli.init import InitError, init
from second_brain.cli.main import main
from second_brain.cli.secrets import create_file
from second_brain.guard.config import GuardConfig
from second_brain.sidecars.config import SidecarsConfig
from second_brain.tools.config import ToolsConfig

SAMPLE = Path(__file__).with_name("answers.sample.yaml")
GENERATED = (
    "tools_token",
    "webhook_ingest",
    "webhook_research",
    "webhook_idea",
    "webhook_schedule",
    "outbox_secret",
    "searxng_key",
)


@pytest.fixture
def answers_file(tmp_path: Path) -> Path:
    """The sample answers, with the three host paths moved under the temp directory."""
    raw = yaml.safe_load(SAMPLE.read_text(encoding="utf-8"))
    raw["vault_dir"] = str(tmp_path / "vault")
    raw["media_dir"] = str(tmp_path / "media")
    raw["archive_dir"] = str(tmp_path / "archive")
    path = tmp_path / "answers.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    return path


@pytest.fixture
def answers(answers_file: Path) -> Answers:
    return Answers.from_yaml(answers_file)


@pytest.fixture
def instance(tmp_path: Path, answers: Answers) -> Path:
    directory = tmp_path / "instance"
    init(answers, directory)
    return directory


def _files(root: Path) -> list[str]:
    return sorted(str(path.relative_to(root)) for path in root.rglob("*") if path.is_file())


def test_every_config_file_is_created(instance: Path) -> None:
    created = set(_files(instance))
    expected = {
        "README.md",
        "compose.yaml",
        "config/hermes/config.yaml",
        "config/hermes/SOUL.md",
        "config/hermes/USER.md",
        "config/brain-tools.yaml",
        "config/brain-guard.yaml",
        "config/sidecars.yaml",
        "config/squid.conf",
        "config/searxng/settings.yml",
        "secrets/README.md",
        "secrets/hermes.env",
        "secrets/terminal_ssh_key",
        "secrets/terminal_host_key",
    }
    assert expected <= created
    for name in GENERATED:
        assert f"secrets/{name}" in created, name


def test_no_template_placeholder_survives(instance: Path) -> None:
    for path in instance.rglob("*"):
        if not path.is_file() or path.name.startswith("terminal_"):
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        assert "{{" not in text, f"{path} still has a placeholder"
        assert "{%" not in text, f"{path} still has a tag"


def test_secrets_are_owner_only_and_the_generated_ones_are_filled(instance: Path) -> None:
    for path in (instance / "secrets").iterdir():
        if path.name.endswith(".pub"):
            continue
        mode = stat.S_IMODE(path.stat().st_mode)
        assert mode & 0o077 == 0, f"{path} is {oct(mode)}"
        if path.name in GENERATED:
            assert path.read_text(encoding="utf-8").strip(), path


def test_the_managed_env_carries_the_generated_values(instance: Path) -> None:
    env = (instance / "secrets" / "hermes.env").read_text(encoding="utf-8")
    token = (instance / "secrets" / "tools_token").read_text(encoding="utf-8").strip()
    assert f"TOOLS_TOKEN={token}" in env
    # The chat token comes from a service: the line is there, the value is not.
    assert "TELEGRAM_BOT_TOKEN=\n" in env


def test_two_runs_generate_different_internal_secrets(tmp_path: Path, answers_file: Path) -> None:
    first = Answers.from_yaml(answers_file)
    second_raw = yaml.safe_load(answers_file.read_text(encoding="utf-8"))
    second_raw["vault_dir"] = str(tmp_path / "vault-two")
    second_raw["instance_name"] = "other-brain"
    other = tmp_path / "other.yaml"
    other.write_text(yaml.safe_dump(second_raw), encoding="utf-8")
    second = Answers.from_yaml(other)
    init(first, tmp_path / "one")
    init(second, tmp_path / "two")
    for name in GENERATED:
        left = (tmp_path / "one" / "secrets" / name).read_text(encoding="utf-8")
        right = (tmp_path / "two" / "secrets" / name).read_text(encoding="utf-8")
        assert left != right, name


def test_guard_config_has_every_enabled_tool(instance: Path) -> None:
    guard = GuardConfig.from_yaml(instance / "config/brain-guard.yaml")
    tools = ToolsConfig.from_yaml(instance / "config/brain-tools.yaml")
    expected = {
        "vault_read",
        "vault_write",
        "vault_move",
        "vault_delete",
        "vault_undo",
        "calendar_list",
        "weather",
        "nextcloud_link",
        "archive_store",
        "job_enqueue",
        "notify",
    }
    assert expected <= set(guard.mcp_tools)
    assert guard.hermes_config_path == "/etc/hermes/config.yaml"
    assert guard.vault_root == "/vault"
    assert tools.vault.root == Path("/vault")
    # Every exported tool is a tool the server actually serves, and the reverse.
    assert set(guard.mcp_tools) == {spec.name for spec in _specs(tools)}


def _specs(tools: ToolsConfig) -> list:
    from second_brain.cli.config import export_registry

    return export_registry(tools).specs()


def test_a_disabled_group_is_not_in_the_guard_table(tmp_path: Path, answers_file: Path) -> None:
    raw = yaml.safe_load(answers_file.read_text(encoding="utf-8"))
    raw["tools"] = {"weather": False}
    raw["job_kinds"] = {}
    del raw["archive_dir"]
    path = tmp_path / "minimal.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    minimal = Answers.from_yaml(path)
    init(minimal, tmp_path / "minimal-instance")
    guard = GuardConfig.from_yaml(tmp_path / "minimal-instance" / "config/brain-guard.yaml")
    assert set(guard.mcp_tools) == {"vault_read", "vault_write", "vault_move", "vault_delete", "vault_undo"}


def test_squid_allows_the_tool_hosts_and_the_model(instance: Path) -> None:
    squid = (instance / "config" / "squid.conf").read_text(encoding="utf-8")
    for host in (
        "model.example.com",
        "api.telegram.org",
        "api.open-meteo.com",
        "geocoding-api.open-meteo.com",
        "www.googleapis.com",
    ):
        assert f"acl allowed_egress dstdomain {host}" in squid, host
    # A host nobody enabled is not on the list.
    assert "slack.com" not in squid


def test_the_compose_file_is_valid_and_has_no_plugin_mount(instance: Path) -> None:
    compose = yaml.safe_load((instance / "compose.yaml").read_text(encoding="utf-8"))
    assert compose["name"] == "sample-brain"
    for service in compose["services"].values():
        assert not any("plugins/brain-guard" in str(volume) for volume in service.get("volumes", []))


def test_the_vault_is_scaffolded_from_the_language_template(instance: Path, answers: Answers) -> None:
    assert (answers.vault_dir / "_meta" / "SCHEMA.md").is_file()
    assert (answers.vault_dir / "inbox").is_dir()
    assert not (answers.vault_dir / "inbox" / ".gitkeep").exists()


def test_an_existing_non_empty_vault_is_left_untouched(tmp_path: Path, answers: Answers) -> None:
    answers.vault_dir.mkdir(parents=True)
    note = answers.vault_dir / "inbox" / "mine.md"
    note.parent.mkdir()
    note.write_text("mine", encoding="utf-8")
    result = init(answers, tmp_path / "instance")
    assert result.vault_skipped is True
    assert note.read_text(encoding="utf-8") == "mine"
    assert not (answers.vault_dir / "_meta").exists()


def test_an_existing_instance_dir_is_refused_without_force(tmp_path: Path, answers: Answers) -> None:
    directory = tmp_path / "instance"
    directory.mkdir()
    (directory / "keep.txt").write_text("keep", encoding="utf-8")
    with pytest.raises(InitError):
        init(answers, directory)
    assert _files(directory) == ["keep.txt"]


def test_force_rerenders_but_keeps_every_secret(tmp_path: Path, answers: Answers) -> None:
    directory = tmp_path / "instance"
    init(answers, directory)
    before = {name: (directory / "secrets" / name).read_text(encoding="utf-8") for name in GENERATED}
    (directory / "config" / "brain-tools.yaml").write_text("stale", encoding="utf-8")

    init(answers, directory, force=True)

    after = {name: (directory / "secrets" / name).read_text(encoding="utf-8") for name in GENERATED}
    assert before == after
    assert ToolsConfig.from_yaml(directory / "config" / "brain-tools.yaml").vault.root == Path("/vault")


def test_a_second_run_renders_the_token_that_is_on_disk(tmp_path: Path, answers: Answers) -> None:
    directory = tmp_path / "instance"
    init(answers, directory)
    token = (directory / "secrets" / "tools_token").read_text(encoding="utf-8").strip()
    # The rendered config has to name the file, and the file has to hold the token the guard
    # and brain_tools both read: a re-render must not invent a third value.
    assert ToolsConfig.from_yaml(directory / "config" / "brain-tools.yaml").token_file == Path(
        "/run/secrets/tools_token"
    )
    init(answers, directory, force=True)
    assert (directory / "secrets" / "tools_token").read_text(encoding="utf-8").strip() == token


def test_a_slack_brain_allows_the_slack_hosts_and_no_telegram(tmp_path: Path, answers_file: Path) -> None:
    raw = yaml.safe_load(answers_file.read_text(encoding="utf-8"))
    del raw["owner_telegram_id"]
    raw["slack"] = {"owner_id": "U0000000001", "home_channel": "C0000000001"}
    raw["tools"] = {"weather": True}
    raw["job_kinds"] = {}
    del raw["archive_dir"]
    path = tmp_path / "slack.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    init(Answers.from_yaml(path), tmp_path / "slack-instance")
    squid = (tmp_path / "slack-instance" / "config" / "squid.conf").read_text(encoding="utf-8")
    assert "acl allowed_egress dstdomain slack.com" in squid
    assert "acl allowed_egress dstdomain api.telegram.org" not in squid
    assert (tmp_path / "slack-instance" / "secrets" / "slack_bot_token").is_file()
    assert not (tmp_path / "slack-instance" / "secrets" / "telegram_bot_token").exists()


def test_the_channel_is_required(tmp_path: Path, answers_file: Path) -> None:
    raw = yaml.safe_load(answers_file.read_text(encoding="utf-8"))
    del raw["owner_telegram_id"]
    path = tmp_path / "no-channel.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    with pytest.raises(AnswersError):
        Answers.from_yaml(path)


def test_an_unknown_answer_is_refused(tmp_path: Path, answers_file: Path) -> None:
    raw = yaml.safe_load(answers_file.read_text(encoding="utf-8"))
    raw["vault_dirs"] = "/typo"
    path = tmp_path / "typo.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    with pytest.raises(AnswersError):
        Answers.from_yaml(path)


def test_a_relative_path_is_refused(tmp_path: Path, answers_file: Path) -> None:
    raw = yaml.safe_load(answers_file.read_text(encoding="utf-8"))
    raw["vault_dir"] = "vault"
    path = tmp_path / "relative.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    with pytest.raises(AnswersError):
        Answers.from_yaml(path)


def test_the_command_reports_what_it_did(tmp_path: Path, answers_file: Path, capsys) -> None:
    instance = tmp_path / "instance"
    assert main(["init", str(instance), "--answers", str(answers_file)]) == 0
    out = capsys.readouterr().out
    assert "created config/hermes/config.yaml" in out
    assert "scaffolded" in out


def test_the_command_exits_non_zero_on_a_refusal(tmp_path: Path, answers_file: Path, capsys) -> None:
    instance = tmp_path / "instance"
    instance.mkdir()
    (instance / "keep.txt").write_text("keep", encoding="utf-8")
    assert main(["init", str(instance), "--answers", str(answers_file)]) == 1
    assert "--force" in capsys.readouterr().err


def _sidecars(instance: Path) -> SidecarsConfig:
    return SidecarsConfig.model_validate(yaml.safe_load((instance / "config" / "sidecars.yaml").read_text()))


def _minimal_answers(tmp_path: Path, answers_file: Path, **changes) -> Answers:
    """The sample answers with the tools turned down, written next to the instance."""
    raw = yaml.safe_load(answers_file.read_text(encoding="utf-8"))
    raw["tools"] = {"weather": False}
    raw["job_kinds"] = {}
    del raw["archive_dir"]
    raw.update(changes)
    path = tmp_path / "minimal.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    return Answers.from_yaml(path)


def test_the_sidecars_config_is_rendered_and_loads(instance: Path) -> None:
    sidecars = _sidecars(instance)
    assert sidecars.vault_dir == Path("/vault")
    assert sidecars.webhook_base_url == "http://hermes:8644/webhooks"
    assert sidecars.vault_watcher is not None
    assert sidecars.call_router is not None
    assert sidecars.scheduler is not None


def test_every_route_the_sidecars_use_has_a_secret_file(instance: Path) -> None:
    sidecars = _sidecars(instance)
    used = {entry.route for entry in sidecars.scheduler.schedule.entries}
    used |= {sidecars.vault_watcher.route, sidecars.call_router.route}
    used |= {kind.route for kind in sidecars.job_runner.jobs.kinds.values()}
    assert used <= set(sidecars.route_secrets)
    for route, path in sidecars.route_secrets.items():
        assert path.parent == Path("/run/secrets"), route


def test_the_scheduler_has_the_default_times(instance: Path) -> None:
    entries = _sidecars(instance).scheduler.schedule.entries
    by_route = {}
    for entry in entries:
        by_route.setdefault(entry.route, []).append(entry.at)
    assert len(by_route["scheduled-digest"]) == 2
    assert by_route["scheduled-lint"] == ["03:30"]
    assert len(by_route["scheduled-brief"]) == 2
    assert _sidecars(instance).scheduler.state_file == Path("/data/scheduler.json")


def test_the_vault_watcher_ignores_the_capture_folder(instance: Path) -> None:
    sidecars = _sidecars(instance)
    assert sidecars.call_router.watch_dir == "inbox/calls"
    assert "inbox/calls" in sidecars.vault_watcher.ignore


def test_the_team_domains_are_empty_by_default(instance: Path) -> None:
    assert _sidecars(instance).call_router.rules.team_domains == []


def test_the_tools_config_has_an_outbox_with_a_non_web_followup(instance: Path) -> None:
    tools = ToolsConfig.from_yaml(instance / "config" / "brain-tools.yaml")
    assert tools.outbox is not None
    assert tools.outbox.dir == Path("/data/outbox")
    assert tools.outbox.secret_file == Path("/run/secrets/outbox_secret")
    assert tools.outbox.research_followup_kind == "idea"
    assert tools.jobs.kinds["idea"].web is False


def test_a_brain_without_jobs_has_no_job_runner_and_no_outbox(tmp_path: Path, answers_file: Path) -> None:
    minimal = _minimal_answers(tmp_path, answers_file)
    instance = tmp_path / "minimal-instance"
    init(minimal, instance)
    assert _sidecars(instance).job_runner is None
    assert ToolsConfig.from_yaml(instance / "config" / "brain-tools.yaml").outbox is None


def test_the_schedule_secret_is_generated_and_exported(instance: Path) -> None:
    env = (instance / "secrets" / "hermes.env").read_text(encoding="utf-8")
    secret = (instance / "secrets" / "webhook_schedule").read_text(encoding="utf-8").strip()
    assert secret
    assert f"WEBHOOK_SECRET_SCHEDULE={secret}" in env


def test_jobs_with_only_web_kinds_are_refused(tmp_path: Path, answers_file: Path) -> None:
    raw = yaml.safe_load(answers_file.read_text(encoding="utf-8"))
    del raw["job_kinds"]["idea"]
    path = tmp_path / "web-only.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    with pytest.raises(AnswersError):
        Answers.from_yaml(path)


def test_a_bad_schedule_time_is_refused(tmp_path: Path, answers_file: Path) -> None:
    raw = yaml.safe_load(answers_file.read_text(encoding="utf-8"))
    raw["schedule"] = {"digest": ["8:00", "20:00"]}
    path = tmp_path / "bad-time.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    with pytest.raises(AnswersError):
        Answers.from_yaml(path)


def test_an_italian_brain_points_notify_at_its_own_initiative_page(
    tmp_path: Path, answers_file: Path
) -> None:
    raw = yaml.safe_load(answers_file.read_text(encoding="utf-8"))
    raw["language"] = "it"
    raw["tools"]["notify"] = {"default_daily": 4}
    path = tmp_path / "italian.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    italian = Answers.from_yaml(path)
    init(italian, tmp_path / "italian-instance")
    tools = ToolsConfig.from_yaml(tmp_path / "italian-instance" / "config" / "brain-tools.yaml")
    assert tools.notify is not None
    assert tools.notify.initiative_path == "_brain/iniziativa.md"


def test_an_explicit_initiative_path_wins_over_the_schema(tmp_path: Path, answers_file: Path) -> None:
    raw = yaml.safe_load(answers_file.read_text(encoding="utf-8"))
    raw["language"] = "it"
    raw["tools"]["notify"] = {"initiative_path": "_brain/mia-iniziativa.md", "default_daily": 4}
    path = tmp_path / "explicit.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    explicit = Answers.from_yaml(path)
    init(explicit, tmp_path / "explicit-instance")
    tools = ToolsConfig.from_yaml(tmp_path / "explicit-instance" / "config" / "brain-tools.yaml")
    assert tools.notify is not None
    assert tools.notify.initiative_path == "_brain/mia-iniziativa.md"


def test_the_docker_group_from_the_answers_wins(tmp_path: Path, answers_file: Path) -> None:
    from second_brain.cli.config import template_values

    raw = yaml.safe_load(answers_file.read_text(encoding="utf-8"))
    raw["docker_gid"] = 4242
    path = tmp_path / "gid.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    with_group = Answers.from_yaml(path)
    assert template_values(with_group, _registry(with_group), _generated(with_group))["docker_gid"] == 4242


def _registry(answers: Answers):
    from second_brain.cli.config import export_registry, tools_config

    return export_registry(tools_config(answers))


def _generated(answers: Answers) -> dict[str, str]:
    from second_brain.cli.secrets import SEARXNG_KEY

    return {SEARXNG_KEY: "sample-searxng-key-not-a-real-one"}


def test_the_gate_upstream_has_no_v1_suffix(tmp_path: Path, answers_file: Path) -> None:
    from second_brain.cli.config import model_upstream, template_values

    raw = yaml.safe_load(answers_file.read_text(encoding="utf-8"))
    raw["model_endpoint"] = "https://model.example.com/v1"
    path = tmp_path / "endpoint.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    with_v1 = Answers.from_yaml(path)
    assert model_upstream(with_v1) == "https://model.example.com"
    assert template_values(with_v1, _registry(with_v1), _generated(with_v1))["model_upstream"] == (
        "https://model.example.com"
    )
    # The other form keeps working: it is the endpoint as written.
    raw["model_endpoint"] = "https://model.example.com"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    without_v1 = Answers.from_yaml(path)
    assert model_upstream(without_v1) == "https://model.example.com"


def test_the_compose_gate_upstream_drops_the_v1(instance: Path) -> None:
    compose = yaml.safe_load((instance / "compose.yaml").read_text(encoding="utf-8"))
    upstream = compose["services"]["gate"]["environment"].get("GATE_UPSTREAM")
    if upstream is not None:
        assert upstream == "https://model.example.com"


def test_brain_tools_listens_on_every_interface(instance: Path) -> None:
    tools = ToolsConfig.from_yaml(instance / "config" / "brain-tools.yaml")
    assert tools.listen.host == "0.0.0.0"
    assert tools.listen.port == 8765


def test_a_brain_without_a_calendar_has_no_calendar_token(tmp_path: Path, answers_file: Path) -> None:
    raw = yaml.safe_load(answers_file.read_text(encoding="utf-8"))
    raw["tools"] = {"weather": False}
    raw["job_kinds"] = {}
    del raw["archive_dir"]
    path = tmp_path / "no-calendar.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    instance = tmp_path / "no-calendar-instance"
    init(Answers.from_yaml(path), instance)
    assert not (instance / "secrets" / "calendar_token").exists()
    assert "calendar_token" not in (instance / "secrets" / "README.md").read_text(encoding="utf-8")


def test_a_brain_with_a_calendar_has_a_calendar_token(instance: Path) -> None:
    assert (instance / "secrets" / "calendar_token").is_file()
    assert "calendar_token" in (instance / "secrets" / "README.md").read_text(encoding="utf-8")


def test_owners_sh_is_executable_and_fixes_the_ownership(instance: Path, answers: Answers) -> None:
    script = instance / "owners.sh"
    assert script.is_file()
    assert stat.S_IMODE(script.stat().st_mode) & 0o111 == 0o111
    text = script.read_text(encoding="utf-8")
    assert text.startswith("#!/bin/sh")
    assert 'cd "$(dirname "$0")"' in text
    assert 'chown 23001:23001 "secrets/hermes.env"' in text
    assert 'chown 23002:23002 "secrets/tools_token"' in text
    assert f"chown -R 23002:23002 '{answers.vault_dir}'" in text
    assert f"chown -R 23002:23002 '{answers.archive_dir}'" in text
    # The calendar token is only chowned when the brain has a calendar.
    assert "secrets/calendar_token" in text
    assert 'chmod 600 "secrets/calendar_token"' in text


def test_owners_sh_is_rerendered_on_every_run(tmp_path: Path, answers: Answers) -> None:
    directory = tmp_path / "instance"
    init(answers, directory)
    (directory / "owners.sh").write_text("stale", encoding="utf-8")
    init(answers, directory, force=True)
    assert "#!/bin/sh" in (directory / "owners.sh").read_text(encoding="utf-8")


def test_a_brain_without_a_calendar_has_no_calendar_line_in_owners_sh(
    tmp_path: Path, answers_file: Path
) -> None:
    minimal = _minimal_answers(tmp_path, answers_file)
    instance = tmp_path / "minimal-instance"
    init(minimal, instance)
    text = (instance / "owners.sh").read_text(encoding="utf-8")
    assert "secrets/calendar_token" not in text
    assert "archive" not in text


def test_the_capture_folder_is_created_in_a_fresh_vault(instance: Path, answers: Answers) -> None:
    capture = answers.vault_dir / "inbox" / "calls"
    assert capture.is_dir()
    assert (capture / ".gitkeep").is_file()


def test_an_existing_vault_does_not_get_a_capture_folder(tmp_path: Path, answers: Answers) -> None:
    answers.vault_dir.mkdir(parents=True)
    note = answers.vault_dir / "mine.md"
    note.write_text("mine", encoding="utf-8")
    init(answers, tmp_path / "instance")
    assert not (answers.vault_dir / "inbox" / "calls").exists()


def test_the_searxng_image_is_a_tag_that_exists(instance: Path) -> None:
    from second_brain.cli.config import SEARXNG_IMAGE

    assert SEARXNG_IMAGE == "searxng/searxng:2026.10.19-81371861f"
    compose = yaml.safe_load((instance / "compose.yaml").read_text(encoding="utf-8"))
    assert compose["services"]["searxng"]["image"] == SEARXNG_IMAGE


SKILLS = Path(__file__).resolve().parents[2] / "skills"


def test_every_product_skill_is_copied_into_the_instance(instance: Path) -> None:
    source = sorted(path.parent.name for path in SKILLS.glob("*/SKILL.md"))
    assert source
    for name in source:
        copied = instance / "skills" / name / "SKILL.md"
        assert copied.is_file(), name
        assert copied.read_bytes() == (SKILLS / name / "SKILL.md").read_bytes(), name


def test_a_rerun_refreshes_a_changed_skill(tmp_path: Path, answers: Answers) -> None:
    directory = tmp_path / "instance"
    init(answers, directory)
    skill = directory / "skills" / "lint" / "SKILL.md"
    skill.write_text("stale", encoding="utf-8")

    init(answers, directory, force=True)

    assert skill.read_bytes() == (SKILLS / "lint" / "SKILL.md").read_bytes()


def test_the_command_reports_the_skills_folder(tmp_path: Path, answers_file: Path, capsys) -> None:
    assert main(["init", str(tmp_path / "instance"), "--answers", str(answers_file)]) == 0
    assert "created skills/" in capsys.readouterr().out


def test_the_compose_mounts_the_skills_read_only(instance: Path) -> None:
    compose = yaml.safe_load((instance / "compose.yaml").read_text(encoding="utf-8"))
    assert "./skills:/opt/data/skills/second-brain:ro" in compose["services"]["hermes"]["volumes"]


def test_the_hermes_config_has_no_external_skill_dir(instance: Path) -> None:
    config = yaml.safe_load((instance / "config" / "hermes" / "config.yaml").read_text(encoding="utf-8"))
    assert config["skills"]["external_dirs"] == []


def _git(vault: Path, *args: str) -> str:
    import subprocess

    return subprocess.run(["git", "-C", str(vault), *args], capture_output=True, text=True, check=True).stdout


def test_the_scaffolded_vault_is_committed_once_by_sb_init(instance: Path, answers: Answers) -> None:
    commits = _git(answers.vault_dir, "log", "--format=%an %ae %s").strip().splitlines()
    assert commits == ["sb-init sb-init@localhost scaffold: vault created by sb init"]
    # Nothing left over: the human-commit timer must not see the scaffold as the owner's edits.
    assert _git(answers.vault_dir, "status", "--porcelain").strip() == ""


KEYPAIRS = ("terminal_ssh_key", "terminal_host_key")


def test_a_fresh_init_creates_both_keypairs(tmp_path: Path, answers: Answers) -> None:
    if shutil.which("ssh-keygen") is None:
        pytest.skip("ssh-keygen is not on PATH")
    directory = tmp_path / "instance"
    init(answers, directory)
    for name in KEYPAIRS:
        private = directory / "secrets" / name
        public = directory / "secrets" / f"{name}.pub"
        assert private.read_text(encoding="utf-8").startswith("-----BEGIN OPENSSH PRIVATE KEY-----")
        assert public.read_text(encoding="utf-8").startswith("ssh-ed25519 ")
        assert stat.S_IMODE(private.stat().st_mode) == 0o600


def test_a_rerun_keeps_the_host_key_unchanged(tmp_path: Path, answers: Answers) -> None:
    directory = tmp_path / "instance"
    init(answers, directory)
    host_key = (directory / "secrets" / "terminal_host_key").read_bytes()
    init(answers, directory, force=True)
    assert (directory / "secrets" / "terminal_host_key").read_bytes() == host_key


def test_the_secrets_readme_names_the_host_key(instance: Path) -> None:
    readme = (instance / "secrets" / "README.md").read_text(encoding="utf-8")
    assert "terminal_host_key" in readme
    assert "/etc/ssh/terminal_host_key" in readme


def test_owners_sh_gives_the_host_key_to_the_terminal_user(instance: Path) -> None:
    text = (instance / "owners.sh").read_text(encoding="utf-8")
    assert 'chown 1000:1000 "secrets/terminal_host_key"' in text
    assert 'chmod 600 "secrets/terminal_host_key"' in text


def test_the_terminal_mounts_the_host_key_read_only(instance: Path) -> None:
    compose = yaml.safe_load((instance / "compose.yaml").read_text(encoding="utf-8"))
    assert (
        "./secrets/terminal_host_key:/etc/ssh/terminal_host_key:ro"
        in compose["services"]["terminal"]["volumes"]
    )


def test_create_file_leaves_an_existing_file_alone(tmp_path: Path) -> None:
    path = tmp_path / "tools_token"
    path.write_text("first\n", encoding="utf-8")
    path.chmod(0o644)
    assert create_file(path, "second\n") == []
    assert path.read_text(encoding="utf-8") == "first\n"
    assert stat.S_IMODE(path.stat().st_mode) == 0o644


@pytest.mark.skipif(os.geteuid() == 0, reason="root can read a file whatever its mode is")
def test_an_unreadable_secret_on_a_rerun_gives_a_clear_error(tmp_path: Path, answers: Answers) -> None:
    directory = tmp_path / "instance"
    init(answers, directory)
    secret = directory / "secrets" / "tools_token"
    secret.chmod(0o000)
    with pytest.raises(InitError) as raised:
        init(answers, directory, force=True)
    message = str(raised.value)
    assert "tools_token" in message
    assert "owners.sh" in message
    assert "--force" in message
