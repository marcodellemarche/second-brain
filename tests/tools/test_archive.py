"""Tests for the archive tools. Every fake value is built at runtime."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from second_brain.core.types import Autonomy, Effect
from second_brain.tools.archive import (
    ArchiveConfig,
    ArchiveError,
    CategoriesParams,
    DiscardParams,
    StoreParams,
    archive_tools,
)
from second_brain.tools.registry import Tool
from second_brain.tools.vault import Provenance

PAYLOAD = b"%PDF-1.4 fake document, no real data\n"


def _provenance(**overrides: object) -> Provenance:
    values: dict[str, object] = {
        "requested_by": "user-1",
        "reason": "test call",
        "source_integrity": "trusted",
        "session": "session-1",
    }
    values.update(overrides)
    return Provenance(**values)  # type: ignore[arg-type]


@pytest.fixture
def root(tmp_path: Path) -> Path:
    archive = tmp_path / "archive"
    for folder in ("10-19 Home/11 House", "10-19 Home/12 Car", "20-29 Money/21 Taxes"):
        (archive / folder).mkdir(parents=True)
    (archive / "10-19 Home/11 House/11.01 Old lease.pdf").write_bytes(b"old lease\n")
    return archive


@pytest.fixture
def media(tmp_path: Path) -> Path:
    folder = tmp_path / "media"
    folder.mkdir()
    (folder / "attachment.pdf").write_bytes(PAYLOAD)
    (folder / "photo.jpg").write_bytes(b"jpeg bytes\n")
    (folder / "notes.txt").write_bytes(b"plain text\n")
    return folder


@pytest.fixture
def config(root: Path, media: Path) -> ArchiveConfig:
    return ArchiveConfig(root=root, inbox_media_dir=media)


@pytest.fixture
def tools(config: ArchiveConfig) -> dict[str, Tool]:
    return {tool.spec.name: tool for tool in archive_tools(config)}


def _call(tools: dict[str, Tool], name: str, params: dict[str, Any], **prov: object) -> dict[str, Any]:
    tool = tools[name]
    parsed = tool.spec.params.model_validate(params)
    return tool.handler(parsed, _provenance(**prov))


# --- specs ---------------------------------------------------------------------------------


def test_tools_are_declared_with_their_effect_and_autonomy(tools: dict[str, Tool]) -> None:
    declared = {name: (tool.spec.effect, tool.spec.autonomy) for name, tool in tools.items()}
    assert declared == {
        "archive_categories": (Effect.READ, Autonomy.AUTO),
        "archive_store": (Effect.INTERNAL, Autonomy.NOTIFY),
        "archive_discard": (Effect.INTERNAL, Autonomy.ASK),
    }


def test_module_docstring_says_nothing_is_deleted_or_overwritten() -> None:
    from second_brain.tools import archive

    docstring = (archive.__doc__ or "").lower()
    assert "deletes" in docstring
    assert "overwrites" in docstring


def test_config_forbids_extra_keys(root: Path, media: Path) -> None:
    with pytest.raises(ValidationError):
        ArchiveConfig.model_validate({"root": root, "inbox_media_dir": media, "trash_dir": "elsewhere"})


def test_config_rejects_a_discard_dir_that_is_a_path(root: Path, media: Path) -> None:
    with pytest.raises(ValidationError):
        ArchiveConfig(root=root, inbox_media_dir=media, discard_dir="../outside")


def test_config_lowercases_the_allowed_extensions(root: Path, media: Path) -> None:
    config = ArchiveConfig(root=root, inbox_media_dir=media, allowed_extensions=("PDF", ".png"))
    assert config.allowed_extensions == ("pdf", "png")


# --- categories ----------------------------------------------------------------------------


def test_categories_lists_the_two_levels(tools: dict[str, Tool]) -> None:
    result = _call(tools, "archive_categories", {})
    assert result["ok"] is True
    codes = [(entry["code"], entry["name"]) for entry in result["categories"]]
    assert codes == [("11", "House"), ("12", "Car"), ("21", "Taxes")]
    assert result["categories"][0]["path"] == "10-19 Home/11 House"
    assert result["categories"][0]["area_code"] == "10"


def test_categories_skips_folders_without_a_code(tools: dict[str, Tool], root: Path) -> None:
    (root / "10-19 Home" / "misc").mkdir()
    (root / "loose folder").mkdir()
    result = _call(tools, "archive_categories", {})
    assert [entry["code"] for entry in result["categories"]] == ["11", "12", "21"]


# --- store ---------------------------------------------------------------------------------


def test_store_copies_into_the_next_item_number(tools: dict[str, Tool], root: Path, media: Path) -> None:
    result = _call(
        tools, "archive_store", {"source": "attachment.pdf", "category_code": "11", "name": "Lease"}
    )
    assert result["ok"] is True
    assert result["path"] == "10-19 Home/11 House/11.02 Lease.pdf"
    stored = root / result["path"]
    assert stored.read_bytes() == PAYLOAD
    assert result["sha256"] == hashlib.sha256(PAYLOAD).hexdigest()
    assert (media / "attachment.pdf").read_bytes() == PAYLOAD


def test_store_keeps_an_item_level_code(tools: dict[str, Tool], root: Path) -> None:
    result = _call(
        tools, "archive_store", {"source": "attachment.pdf", "category_code": "12.04", "name": "Insurance"}
    )
    assert result["ok"] is True
    assert result["path"] == "10-19 Home/12 Car/12.04 Insurance.pdf"
    assert (root / result["path"]).read_bytes() == PAYLOAD


def test_store_does_not_repeat_the_extension_in_the_name(tools: dict[str, Tool]) -> None:
    result = _call(
        tools, "archive_store", {"source": "photo.jpg", "category_code": "11", "name": "Photo.jpg"}
    )
    assert result["path"] == "10-19 Home/11 House/11.02 Photo.jpg"


def test_store_never_replaces_an_existing_seeded_file(tools: dict[str, Tool], root: Path) -> None:
    result = _call(
        tools,
        "archive_store",
        {"source": "attachment.pdf", "category_code": "11.01", "name": "Old lease"},
    )
    assert result["path"] == "10-19 Home/11 House/11.01 Old lease (2).pdf"
    assert (root / "10-19 Home/11 House/11.01 Old lease.pdf").read_bytes() == b"old lease\n"


def test_store_appends_a_suffix_on_a_collision(tools: dict[str, Tool], root: Path) -> None:
    params = {"source": "attachment.pdf", "category_code": "11.05", "name": "Duplicate"}
    first = _call(tools, "archive_store", params)
    second = _call(tools, "archive_store", params)
    third = _call(tools, "archive_store", params)
    assert first["path"] == "10-19 Home/11 House/11.05 Duplicate.pdf"
    assert second["path"] == "10-19 Home/11 House/11.05 Duplicate (2).pdf"
    assert third["path"] == "10-19 Home/11 House/11.05 Duplicate (3).pdf"
    assert (root / "10-19 Home/11 House/11.05 Duplicate.pdf").read_bytes() == PAYLOAD


def test_store_accepts_accents_in_the_name(tools: dict[str, Tool]) -> None:
    result = _call(
        tools,
        "archive_store",
        {"source": "attachment.pdf", "category_code": "11", "name": "Contratto città"},
    )
    assert result["ok"] is True
    assert result["path"] == "10-19 Home/11 House/11.02 Contratto città.pdf"


def test_store_refuses_an_unknown_category(tools: dict[str, Tool], root: Path) -> None:
    result = _call(
        tools, "archive_store", {"source": "attachment.pdf", "category_code": "99", "name": "Lease"}
    )
    assert result["ok"] is False
    assert result["code"] == "unknown_category"
    assert not list((root / "10-19 Home/11 House").glob("99*"))


def test_store_refuses_a_disallowed_extension(tools: dict[str, Tool], root: Path) -> None:
    result = _call(tools, "archive_store", {"source": "notes.txt", "category_code": "11", "name": "Notes"})
    assert result["ok"] is False
    assert result["code"] == "bad_extension"
    assert not list((root / "10-19 Home/11 House").glob("11.02*"))


@pytest.mark.parametrize(
    "name",
    ["..", "a/b", "a\\b", " leading", "trailing ", ".hidden", "with..dots", 'quo"te', "a" * 81],
)
def test_store_refuses_a_bad_name(tools: dict[str, Tool], name: str) -> None:
    with pytest.raises(ValidationError):
        StoreParams.model_validate({"source": "attachment.pdf", "category_code": "11", "name": name})


@pytest.mark.parametrize("code", ["1", "111", "11.1", "11.111", "aa", "11/12", ""])
def test_store_refuses_a_bad_category_code(tools: dict[str, Tool], code: str) -> None:
    with pytest.raises(ValidationError):
        StoreParams.model_validate({"source": "attachment.pdf", "category_code": code, "name": "Lease"})


@pytest.mark.parametrize("source", ["../attachment.pdf", "sub/attachment.pdf", "sub\\attachment.pdf", ".."])
def test_store_refuses_a_source_that_is_a_path(tools: dict[str, Tool], source: str) -> None:
    with pytest.raises(ValidationError):
        StoreParams.model_validate({"source": source, "category_code": "11", "name": "Lease"})


def test_store_refuses_a_source_outside_the_media_folder(
    tools: dict[str, Tool], media: Path, tmp_path: Path
) -> None:
    (media / "escape.pdf").symlink_to(tmp_path / "elsewhere.pdf")
    (tmp_path / "elsewhere.pdf").write_bytes(b"outside\n")
    result = _call(tools, "archive_store", {"source": "escape.pdf", "category_code": "11", "name": "Escape"})
    assert result["ok"] is False
    assert result["code"] == "outside_archive"


def _inbox_tools(root: Path, media: Path, inbox: Path) -> dict[str, Tool]:
    config = ArchiveConfig(root=root, inbox_media_dir=media, attachment_inbox=inbox)
    return {tool.spec.name: tool for tool in archive_tools(config)}


def test_store_takes_an_attachment_from_the_inbox(root: Path, media: Path, tmp_path: Path) -> None:
    inbox = tmp_path / "attachments"
    inbox.mkdir()
    (inbox / "doc_0a1b2c3d4e5f_Lease contract.pdf").write_bytes(PAYLOAD)
    tools = _inbox_tools(root, media, inbox)
    result = _call(
        tools,
        "archive_store",
        {"source": "doc_0a1b2c3d4e5f_Lease contract.pdf", "category_code": "11", "name": "Lease"},
    )
    assert result["ok"] is True
    assert (root / result["path"]).read_bytes() == PAYLOAD


def test_store_prefers_the_media_folder_to_the_inbox(root: Path, media: Path, tmp_path: Path) -> None:
    inbox = tmp_path / "attachments"
    inbox.mkdir()
    (inbox / "attachment.pdf").write_bytes(b"inbox copy\n")
    tools = _inbox_tools(root, media, inbox)
    params = {"source": "attachment.pdf", "category_code": "11", "name": "Lease"}
    result = _call(tools, "archive_store", params)
    assert (root / result["path"]).read_bytes() == PAYLOAD


def test_store_refuses_an_inbox_link_that_leaves_the_inbox(root: Path, media: Path, tmp_path: Path) -> None:
    inbox = tmp_path / "attachments"
    inbox.mkdir()
    (tmp_path / "secret.pdf").write_bytes(b"outside\n")
    (inbox / "escape.pdf").symlink_to(tmp_path / "secret.pdf")
    tools = _inbox_tools(root, media, inbox)
    result = _call(tools, "archive_store", {"source": "escape.pdf", "category_code": "11", "name": "Escape"})
    assert result["ok"] is False
    assert result["code"] == "outside_archive"


def test_store_refuses_a_missing_source(tools: dict[str, Tool]) -> None:
    result = _call(tools, "archive_store", {"source": "gone.pdf", "category_code": "11", "name": "Lease"})
    assert result["ok"] is False
    assert result["code"] == "not_found"


def test_store_leaves_the_source_untouched(tools: dict[str, Tool], media: Path) -> None:
    before = sorted(entry.name for entry in media.iterdir())
    _call(tools, "archive_store", {"source": "attachment.pdf", "category_code": "11", "name": "Lease"})
    assert sorted(entry.name for entry in media.iterdir()) == before
    assert (media / "attachment.pdf").read_bytes() == PAYLOAD


# --- discard -------------------------------------------------------------------------------


def test_discard_moves_the_file_into_the_discard_folder(tools: dict[str, Tool], root: Path) -> None:
    result = _call(
        tools,
        "archive_discard",
        {"path": "10-19 Home/11 House/11.01 Old lease.pdf", "reason": "duplicate"},
        approval="user-1",
    )
    assert result["ok"] is True
    assert result["path"].startswith("_scarti/10-19 Home/11 House/11.01 Old lease.pdf.")
    assert result["reason"] == "duplicate"
    moved = root / result["path"]
    assert moved.read_bytes() == b"old lease\n"
    assert not (root / "10-19 Home/11 House/11.01 Old lease.pdf").exists()
    stamp = result["path"].rsplit(".", 1)[1]
    assert len(stamp) >= 15 and stamp[:8].isdigit() and "T" in stamp


def test_discard_uses_a_configured_discard_dir(root: Path, media: Path, tools: dict[str, Tool]) -> None:
    custom = {
        tool.spec.name: tool
        for tool in archive_tools(ArchiveConfig(root=root, inbox_media_dir=media, discard_dir="_trash"))
    }
    result = _call(
        custom,
        "archive_discard",
        {"path": "10-19 Home/11 House/11.01 Old lease.pdf", "reason": "duplicate"},
        approval="user-1",
    )
    assert result["path"].startswith("_trash/")


@pytest.mark.parametrize(
    "path",
    ["../outside.pdf", "/etc/passwd", "10-19 Home/../outside.pdf", "..", ""],
)
def test_discard_refuses_a_path_outside_the_archive(tools: dict[str, Tool], path: str) -> None:
    if not path:
        with pytest.raises(ValidationError):
            DiscardParams.model_validate({"path": path, "reason": "test"})
        return
    result = _call(tools, "archive_discard", {"path": path, "reason": "test"}, approval="user-1")
    assert result["ok"] is False
    assert result["code"] == "outside_archive"


def test_discard_refuses_a_symlink_that_leaves_the_archive(
    tools: dict[str, Tool], root: Path, tmp_path: Path
) -> None:
    outside = tmp_path / "outside.pdf"
    outside.write_bytes(b"outside\n")
    (root / "10-19 Home/11 House/11.09 Link.pdf").symlink_to(outside)
    result = _call(
        tools,
        "archive_discard",
        {"path": "10-19 Home/11 House/11.09 Link.pdf", "reason": "test"},
        approval="user-1",
    )
    assert result["ok"] is False
    assert result["code"] == "outside_archive"
    assert outside.read_bytes() == b"outside\n"


def test_discard_refuses_a_file_already_discarded(tools: dict[str, Tool]) -> None:
    first = _call(
        tools,
        "archive_discard",
        {"path": "10-19 Home/11 House/11.01 Old lease.pdf", "reason": "duplicate"},
        approval="user-1",
    )
    second = _call(tools, "archive_discard", {"path": first["path"], "reason": "again"}, approval="user-1")
    assert second["ok"] is False
    assert second["code"] == "already_discarded"


def test_discard_refuses_a_missing_file(tools: dict[str, Tool]) -> None:
    result = _call(
        tools,
        "archive_discard",
        {"path": "10-19 Home/11 House/11.07 Nothing.pdf", "reason": "test"},
        approval="user-1",
    )
    assert result["ok"] is False
    assert result["code"] == "not_found"


def test_store_never_replaces_an_existing_file(tools: dict[str, Tool], root: Path) -> None:
    original = root / "10-19 Home/11 House/11.01 Old lease.pdf"
    before = original.read_bytes()
    _call(
        tools,
        "archive_store",
        {"source": "attachment.pdf", "category_code": "11.01", "name": "Old lease"},
    )
    assert original.read_bytes() == before
    assert (root / "10-19 Home/11 House/11.01 Old lease (2).pdf").read_bytes() == PAYLOAD


def test_categories_params_rejects_unknown_keys() -> None:
    with pytest.raises(ValidationError):
        CategoriesParams.model_validate({"depth": 3})


def test_archive_refuses_a_missing_root(tmp_path: Path, media: Path) -> None:
    with pytest.raises(ArchiveError) as refused:
        archive_tools(ArchiveConfig(root=tmp_path / "missing", inbox_media_dir=media))
    assert refused.value.code == "not_found"


def test_archive_store_uses_the_configured_file_mode(root: Path, media: Path) -> None:
    config = ArchiveConfig(root=root, inbox_media_dir=media, file_mode=0o666)
    tools = {tool.spec.name: tool for tool in archive_tools(config)}
    result = _call(tools, "archive_store", {"source": "photo.jpg", "category_code": "21", "name": "Receipt"})
    assert os.stat(root / result["path"]).st_mode & 0o777 == 0o666


def test_archive_store_writes_a_readable_file(tools: dict[str, Tool], root: Path) -> None:
    result = _call(tools, "archive_store", {"source": "photo.jpg", "category_code": "21", "name": "Receipt"})
    stored = root / result["path"]
    assert os.stat(stored).st_mode & 0o777 == 0o644
    assert stored.read_bytes() == b"jpeg bytes\n"
