"""Tests for the Nextcloud link tool. The tool only builds URLs, so no fake is needed."""

from __future__ import annotations

import urllib.parse
from typing import Any

import pytest
from pydantic import ValidationError

from second_brain.core.types import Autonomy, Effect
from second_brain.tools.links import LinkConfig, LinkParams, link_tools
from second_brain.tools.vault import Provenance

BASE = "https://cloud.example.test"
VAULT = "Brain/Vault"
ARCHIVE = "Brain/Archive"


def _provenance(**overrides: object) -> Provenance:
    values: dict[str, object] = {
        "requested_by": "user-1",
        "reason": "test call",
        "source_integrity": "trusted",
        "session": "session-1",
    }
    values.update(overrides)
    return Provenance(**values)  # type: ignore[arg-type]


def _config(**overrides: Any) -> LinkConfig:
    values: dict[str, Any] = {
        "base_url": BASE,
        "vault_folder": VAULT,
        "archive_folder": ARCHIVE,
    }
    values.update(overrides)
    return LinkConfig(**values)


def _tool(config: LinkConfig | None = None):
    tools = link_tools(config or _config())
    assert len(tools) == 1
    return tools[0]


def _call(config: LinkConfig | None = None, **params: Any) -> dict[str, Any]:
    return _tool(config).handler(LinkParams(**params), _provenance())


def _parts(url: str) -> tuple[str, dict[str, str]]:
    split = urllib.parse.urlsplit(url)
    return f"{split.scheme}://{split.netloc}{split.path}", dict(
        urllib.parse.parse_qsl(split.query, keep_blank_values=True)
    )


def test_vault_link_selects_the_file_in_its_folder() -> None:
    result = _call(target="vault", path="topics/notes/foo.md")

    assert result["ok"] is True
    path, query = _parts(result["url"])
    assert path == f"{BASE}/apps/files/"
    assert query == {"dir": "Brain/Vault/topics/notes", "scrollto": "foo.md"}
    assert result["folder"] == "Brain/Vault/topics/notes"
    assert result["name"] == "foo.md"


def test_archive_link_uses_the_archive_folder() -> None:
    result = _call(target="archive", path="2026/old-note.md")

    assert result["ok"] is True
    path, query = _parts(result["url"])
    assert path == f"{BASE}/apps/files/"
    assert query["dir"] == "Brain/Archive/2026"
    assert query["scrollto"] == "old-note.md"


def test_target_defaults_to_the_vault() -> None:
    result = _call(path="foo.md")

    assert result["target"] == "vault"
    assert _parts(result["url"])[1]["dir"] == "Brain/Vault"


def test_a_file_at_the_root_of_the_area() -> None:
    result = _call(path="foo.md")

    assert result["folder"] == "Brain/Vault"
    assert _parts(result["url"])[1] == {"dir": "Brain/Vault", "scrollto": "foo.md"}


def test_spaces_and_accents_are_encoded() -> None:
    result = _call(target="vault", path="progetti/città e país/relazione finale.md")

    url = result["url"]
    assert " " not in url
    assert "à" not in url
    _, query = _parts(url)
    assert query["dir"] == "Brain/Vault/progetti/città e país"
    assert query["scrollto"] == "relazione finale.md"
    assert "relazione%20finale.md" in url
    assert "citt%C3%A0%20e%20pa%C3%ADs" in url


def test_a_file_name_with_a_query_like_character_is_encoded() -> None:
    result = _call(path="weird/a&b=c#d.md")

    assert result["ok"] is True
    _, query = _parts(result["url"])
    assert query["scrollto"] == "a&b=c#d.md"
    assert "#" not in result["url"].split("?")[1]


def test_the_url_keeps_the_subpath_of_the_base_url() -> None:
    config = _config(base_url="https://cloud.example.test/nextcloud/")
    result = _call(config, path="foo.md")

    assert result["url"].startswith("https://cloud.example.test/nextcloud/apps/files/?")


def test_a_trailing_slash_in_the_base_url_does_not_double_up() -> None:
    config = _config(base_url="https://cloud.example.test/")
    assert config.base_url == BASE
    assert "//apps" not in _call(config, path="foo.md")["url"]


@pytest.mark.parametrize(
    "path",
    [
        "/etc/passwd",
        "/foo.md",
        "\\foo.md",
        "topics/../../secret.md",
        "../secret.md",
        "topics/../secret.md",
        "topics/..",
        "..",
        "topics\\..\\secret.md",
        "foo\x00.md",
        "",
        "   ",
        "/",
    ],
)
def test_absolute_paths_and_dot_dot_are_refused(path: str) -> None:
    result = _call(path=path)

    assert result["ok"] is False
    assert result["code"] == "invalid_path"


def test_dot_segments_are_dropped_without_climbing_out() -> None:
    result = _call(path="./topics/./foo.md")

    assert result["ok"] is True
    assert result["folder"] == "Brain/Vault/topics"


def test_unknown_target_is_refused() -> None:
    with pytest.raises(ValidationError):
        LinkParams(target="public", path="foo.md")


def test_extra_params_are_refused() -> None:
    with pytest.raises(ValidationError):
        LinkParams.model_validate({"path": "foo.md", "expire": 7})


@pytest.mark.parametrize(
    "base_url",
    [
        "http://cloud.example.test",
        "ftp://cloud.example.test",
        "cloud.example.test",
        "//cloud.example.test",
        "https://",
        "https://cloud.example.test/?a=1",
        "https://cloud.example.test/#frag",
        "",
    ],
)
def test_base_url_must_be_https_with_a_host(base_url: str) -> None:
    with pytest.raises(ValidationError):
        _config(base_url=base_url)


def test_folders_are_cleaned_and_cannot_climb_out() -> None:
    assert _config(vault_folder="Brain/Vault/").vault_folder == VAULT
    assert _config(archive_folder="/Brain//Archive/").archive_folder == ARCHIVE
    with pytest.raises(ValidationError):
        _config(vault_folder="../Vault")
    with pytest.raises(ValidationError):
        _config(archive_folder="")


def test_the_two_folders_must_differ() -> None:
    with pytest.raises(ValidationError):
        _config(archive_folder=VAULT)


def test_config_forbids_extra_fields() -> None:
    with pytest.raises(ValidationError):
        LinkConfig.model_validate(
            {
                "base_url": BASE,
                "vault_folder": VAULT,
                "archive_folder": ARCHIVE,
                "password": "hunter2",
            }
        )


def test_spec_is_a_read_with_no_hosts() -> None:
    spec = _tool().spec

    assert spec.name == "nextcloud_link"
    assert spec.effect is Effect.READ
    assert spec.autonomy is Autonomy.AUTO
    assert spec.hosts == ()
    assert spec.credentials == ()


def test_the_tool_never_builds_a_public_share_link() -> None:
    result = _call(path="foo.md")

    assert "/s/" not in result["url"]
    assert "/public" not in result["url"]
