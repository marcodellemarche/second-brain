"""Tests for the one sidecars config file. Every file here is written to tmp_path, no real secret."""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest

from second_brain.sidecars import config as sidecar_config
from second_brain.sidecars import webhook
from second_brain.sidecars.config import SidecarsConfig, SidecarsConfigError
from second_brain.sidecars.webhook import SIGNATURE_HEADER, TIMESTAMP_HEADER, sign_body

BASE_URL = "http://hermes:8644/webhooks"
SECRET = "fake-secret-for-tests-only"

FULL_CONFIG = f"""
vault_dir: /vault
webhook_base_url: {BASE_URL}/
route_secrets:
  ingest: /run/secrets/ingest
  digest: /run/secrets/digest
  research: /run/secrets/research
vault_watcher:
  route: ingest
  quiet_seconds: 30
  ignore:
    - "*.tmp"
  brain_email: brain@example.invalid
  poll_seconds: 2
call_router:
  route: ingest
  watch_dir: calls
  rules:
    team_domains:
      - example.invalid
    team_min_participants: 3
  poll_seconds: 15
job_runner:
  jobs:
    store_path: /state/jobs.db
    kinds:
      research:
        route: research
        description: Look something up.
        web: true
  max_concurrent: 2
  dispatch_timeout: 1800
scheduler:
  schedule:
    timezone: Europe/Rome
    entries:
      - name: digest
        route: digest
        at: "07:00"
        days: [mon, tue, wed, thu, fri]
  state_file: /state/schedule.json
  poll_seconds: 60
"""


def write_config(tmp_path: Path, text: str, name: str = "sidecars.yaml") -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def write_secret(tmp_path: Path, name: str) -> Path:
    path = tmp_path / f"{name}.secret"
    path.write_text(f"fake-{name}-secret\n", encoding="utf-8")
    return path


def minimal_config(tmp_path: Path, extra: str = "") -> Path:
    ingest = write_secret(tmp_path, "ingest")
    return write_config(
        tmp_path,
        f"""
vault_dir: {tmp_path}
webhook_base_url: {BASE_URL}
route_secrets:
  ingest: {ingest}
{extra}
""",
    )


def test_full_config_loads(tmp_path: Path) -> None:
    config = SidecarsConfig.from_yaml(write_config(tmp_path, FULL_CONFIG))

    assert config.vault_dir == Path("/vault")
    assert config.webhook_base_url == BASE_URL
    assert config.route_secrets["ingest"] == Path("/run/secrets/ingest")
    assert config.vault_watcher is not None
    assert config.vault_watcher.quiet_seconds == 30
    assert config.vault_watcher.ignore == ["*.tmp"]
    assert config.call_router is not None
    assert config.call_router.watch_dir == "calls"
    assert config.call_router.rules.team_domains == ["example.invalid"]
    assert config.job_runner is not None
    assert config.job_runner.routes() == {"research"}
    assert config.scheduler is not None
    assert config.scheduler.schedule.entries[0].route == "digest"


def test_defaults_of_a_section(tmp_path: Path) -> None:
    path = minimal_config(tmp_path, "vault_watcher: {}\n")
    section = SidecarsConfig.from_yaml(path).require("vault_watcher")

    assert section.route == "ingest"
    assert section.quiet_seconds == 20
    assert section.ignore is None
    assert section.brain_email == "brain@localhost"
    assert section.poll_seconds == 5


@pytest.mark.parametrize("section", ["vault_watcher", "call_router", "job_runner", "scheduler"])
def test_each_section_is_optional(tmp_path: Path, section: str) -> None:
    config = SidecarsConfig.from_yaml(minimal_config(tmp_path))

    assert getattr(config, section) is None
    with pytest.raises(SidecarsConfigError, match=section):
        config.require(section)


def test_no_sections_at_all_is_valid(tmp_path: Path) -> None:
    config = SidecarsConfig.from_yaml(minimal_config(tmp_path))

    assert config.vault_watcher is None
    assert config.scheduler is None


def test_watcher_route_without_secret_is_refused(tmp_path: Path) -> None:
    path = minimal_config(tmp_path, "vault_watcher:\n  route: missing\n")
    with pytest.raises(SidecarsConfigError, match="missing"):
        SidecarsConfig.from_yaml(path)


def test_router_route_without_secret_is_refused(tmp_path: Path) -> None:
    (tmp_path / "calls").mkdir()
    path = minimal_config(tmp_path, "call_router:\n  route: missing\n  watch_dir: calls\n")
    with pytest.raises(SidecarsConfigError, match="missing"):
        SidecarsConfig.from_yaml(path)


def test_job_kind_route_without_secret_is_refused(tmp_path: Path) -> None:
    path = minimal_config(
        tmp_path,
        f"""
job_runner:
  jobs:
    store_path: {tmp_path / "jobs.db"}
    kinds:
      research:
        route: missing
        description: Look something up.
        web: true
""",
    )
    with pytest.raises(SidecarsConfigError, match="missing"):
        SidecarsConfig.from_yaml(path)


def test_schedule_entry_route_without_secret_is_refused(tmp_path: Path) -> None:
    path = minimal_config(
        tmp_path,
        f"""
scheduler:
  schedule:
    timezone: Europe/Rome
    entries:
      - name: digest
        route: missing
        at: "07:00"
  state_file: {tmp_path / "schedule.json"}
""",
    )
    with pytest.raises(SidecarsConfigError, match="missing"):
        SidecarsConfig.from_yaml(path)


def test_inline_secret_value_is_refused(tmp_path: Path) -> None:
    path = minimal_config(
        tmp_path,
        """
vault_watcher:
  route: ingest
  secret: fake-inline-secret
""",
    )
    with pytest.raises(SidecarsConfigError, match="secrets are never in this file"):
        SidecarsConfig.from_yaml(path)


def test_inline_secret_nested_in_a_section_is_refused(tmp_path: Path) -> None:
    path = minimal_config(
        tmp_path,
        f"""
scheduler:
  schedule:
    timezone: Europe/Rome
    entries:
      - name: digest
        route: ingest
        at: "07:00"
  state_file: {tmp_path / "schedule.json"}
  password: fake-inline-password
""",
    )
    with pytest.raises(SidecarsConfigError, match="secrets are never in this file"):
        SidecarsConfig.from_yaml(path)


@pytest.mark.parametrize("watch_dir", ["../outside", "calls/../../outside", "/absolute/calls"])
def test_watch_dir_must_stay_inside_the_vault(tmp_path: Path, watch_dir: str) -> None:
    path = minimal_config(tmp_path, f"call_router:\n  watch_dir: {watch_dir}\n")
    with pytest.raises(SidecarsConfigError, match="watch_dir"):
        SidecarsConfig.from_yaml(path)


def test_unknown_key_is_refused(tmp_path: Path) -> None:
    path = minimal_config(tmp_path, "vault_watcher:\n  quiet_second: 30\n")
    with pytest.raises(SidecarsConfigError, match="quiet_second"):
        SidecarsConfig.from_yaml(path)


def test_unknown_top_level_key_is_refused(tmp_path: Path) -> None:
    path = minimal_config(tmp_path, "vaultwatch:\n  route: ingest\n")
    with pytest.raises(SidecarsConfigError, match="vaultwatch"):
        SidecarsConfig.from_yaml(path)


def test_base_url_must_be_http(tmp_path: Path) -> None:
    ingest = write_secret(tmp_path, "ingest")
    path = write_config(
        tmp_path,
        f"""
vault_dir: {tmp_path}
webhook_base_url: hermes:8644
route_secrets:
  ingest: {ingest}
""",
    )
    with pytest.raises(SidecarsConfigError, match="webhook_base_url"):
        SidecarsConfig.from_yaml(path)


def test_missing_file_is_a_config_error(tmp_path: Path) -> None:
    with pytest.raises(SidecarsConfigError, match="cannot read sidecars config"):
        SidecarsConfig.from_yaml(tmp_path / "nope.yaml")


def test_load_uses_the_environment_variable(tmp_path: Path) -> None:
    path = minimal_config(tmp_path)

    assert SidecarsConfig.load({"SIDECARS_CONFIG": str(path)}).vault_dir == tmp_path
    with pytest.raises(SidecarsConfigError):
        SidecarsConfig.load({"SIDECARS_CONFIG": str(tmp_path / "nope.yaml")})


def test_load_defaults_to_the_etc_path(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    path = minimal_config(tmp_path)
    seen: list[Path] = []

    def fake_from_yaml(cls: type[SidecarsConfig], source: str | Path) -> SidecarsConfig:
        seen.append(Path(source))
        return SidecarsConfig.model_validate(
            {"vault_dir": tmp_path, "webhook_base_url": BASE_URL, "route_secrets": {"ingest": path}}
        )

    monkeypatch.setattr(SidecarsConfig, "from_yaml", classmethod(fake_from_yaml))

    SidecarsConfig.load({})

    assert seen == [Path(sidecar_config.DEFAULT_CONFIG_PATH)]


def test_sender_posts_to_the_route_with_a_valid_signature(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    secret_file = write_secret(tmp_path, "ingest")
    calls: list[tuple[str, bytes, dict[str, str], float]] = []

    def fake_post(url: str, body: bytes, headers: dict[str, str], timeout: float) -> tuple[int, bytes]:
        calls.append((url, body, dict(headers), timeout))
        return 202, b""

    monkeypatch.setattr(webhook, "http_post", fake_post)
    config = SidecarsConfig.from_yaml(minimal_config(tmp_path))

    status = config.sender("ingest")({"kind": "created", "path": "inbox/a.md"})

    assert status == 202
    url, body, headers, _ = calls[0]
    assert url == f"{BASE_URL}/ingest"
    assert headers[SIGNATURE_HEADER] == sign_body(
        secret_file.read_text(encoding="utf-8").strip().encode("utf-8"),
        body,
        headers[TIMESTAMP_HEADER],
    )


def test_sender_for_an_unknown_route_is_a_config_error(tmp_path: Path) -> None:
    config = SidecarsConfig.from_yaml(minimal_config(tmp_path))

    with pytest.raises(SidecarsConfigError, match="no secret file for route 'other'"):
        config.sender("other")


def test_require_names_the_missing_section(tmp_path: Path) -> None:
    config = SidecarsConfig.from_yaml(minimal_config(tmp_path))

    with pytest.raises(SidecarsConfigError, match="has no job_runner section"):
        config.require("job_runner")


@pytest.mark.parametrize("name", ["vault_watcher", "call_router", "job_runner", "scheduler"])
def test_every_sidecar_module_has_an_entry_point(name: str) -> None:
    module = importlib.import_module(f"second_brain.sidecars.{name}")

    assert callable(module.main)
