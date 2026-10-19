"""`sb init` against temp directories: what it creates, and what it refuses to touch.

The vault lives outside the instance directory, so the two refusals are tested separately: an
instance directory that already has something in it, and a vault that already has notes in it.
"""

from __future__ import annotations

import stat
from pathlib import Path

import pytest
import yaml

from second_brain.cli.answers import Answers, AnswersError
from second_brain.cli.init import InitError, init
from second_brain.cli.main import main
from second_brain.guard.config import GuardConfig
from second_brain.tools.config import ToolsConfig

SAMPLE = Path(__file__).with_name("answers.sample.yaml")
GENERATED = (
    "tools_token",
    "webhook_ingest",
    "webhook_research",
    "webhook_idea",
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
        "config/squid.conf",
        "config/searxng/settings.yml",
        "secrets/README.md",
        "secrets/hermes.env",
        "secrets/terminal_ssh_key",
    }
    assert expected <= created
    for name in GENERATED:
        assert f"secrets/{name}" in created, name


def test_no_template_placeholder_survives(instance: Path) -> None:
    for path in instance.rglob("*"):
        if not path.is_file() or path.name == "terminal_ssh_key":
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
