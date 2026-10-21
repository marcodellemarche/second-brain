"""The memory export: one page of the vault with a copy of what the brain remembers.

The folder it reads belongs to Hermes, and it is the only thing it reads. These tests are about the
page it writes and about what it refuses to look at: a symlink out of the folder, a subfolder, a file
too big to belong in the vault.
"""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

from second_brain.tools import memory_export
from second_brain.tools.vault import Vault, VaultConfig

FIXED_NOW = datetime(2026, 10, 21, 9, 30, tzinfo=UTC)
PAGE = "_brain/hermes-memory.md"


def _git(root: Path) -> None:
    subprocess.run(["git", "init", "-q", str(root)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(root), "config", "user.email", "brain@localhost"], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.name", "brain"], check=True)
    subprocess.run(
        ["git", "-C", str(root), "commit", "-qm", "init", "--allow-empty"], check=True, capture_output=True
    )


@pytest.fixture()
def vault(tmp_path: Path) -> Vault:
    root = tmp_path / "vault"
    root.mkdir()
    _git(root)
    return Vault(VaultConfig(root=root))


@pytest.fixture()
def memories(tmp_path: Path) -> Path:
    folder = tmp_path / "memories"
    folder.mkdir()
    return folder


def _exporter(memories: Path, vault: Vault) -> memory_export.MemoryExporter:
    return memory_export.MemoryExporter(memories, PAGE, vault, clock=lambda: FIXED_NOW)


def test_the_page_copies_every_memory_file(memories: Path, vault: Vault) -> None:
    (memories / "MEMORY.md").write_text("The owner likes short answers.\n", encoding="utf-8")
    (memories / "USER.md").write_text("Sample Owner, works in the morning.\n", encoding="utf-8")

    assert _exporter(memories, vault).export_once() == PAGE

    text, _ = vault.read(PAGE)
    assert text.startswith("---\ntype: brain\nupdated: 2026-10-21\nindex: false\n---\n")
    assert memory_export.INTRO in text
    assert "## MEMORY.md" in text
    assert "The owner likes short answers." in text
    assert "## USER.md" in text
    assert "Sample Owner, works in the morning." in text
    # Sorted by name, so the page does not move around when the folder is listed again.
    assert text.index("## MEMORY.md") < text.index("## USER.md")


def test_the_page_is_a_copy_of_the_brain_own_memory(memories: Path, vault: Vault) -> None:
    (memories / "MEMORY.md").write_text("Something remembered.\n", encoding="utf-8")

    _exporter(memories, vault).export_once()

    log = subprocess.run(
        ["git", "-C", str(vault.root), "log", "-1", "--format=%B"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert "write: " + PAGE in log
    author = subprocess.run(
        ["git", "-C", str(vault.root), "log", "-1", "--format=%ae"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert author == "brain@localhost"


def test_nothing_changed_writes_nothing(memories: Path, vault: Vault) -> None:
    (memories / "MEMORY.md").write_text("Something remembered.\n", encoding="utf-8")
    exporter = _exporter(memories, vault)
    assert exporter.export_once() == PAGE
    before = _commits(vault)

    assert exporter.export_once() is None
    assert _commits(vault) == before


def test_a_changed_file_writes_the_page_again(memories: Path, vault: Vault) -> None:
    (memories / "MEMORY.md").write_text("First version.\n", encoding="utf-8")
    exporter = _exporter(memories, vault)
    exporter.export_once()

    (memories / "MEMORY.md").write_text("Second version.\n", encoding="utf-8")
    assert exporter.export_once() == PAGE
    text, _ = vault.read(PAGE)
    assert "Second version." in text
    assert "First version." not in text


def test_a_missing_folder_exports_nothing(tmp_path: Path, vault: Vault) -> None:
    assert _exporter(tmp_path / "nowhere", vault).export_once() is None
    with pytest.raises(memory_export.VaultError):
        vault.read(PAGE)


def test_a_symlink_and_a_subfolder_are_ignored(memories: Path, tmp_path: Path, vault: Vault) -> None:
    (memories / "MEMORY.md").write_text("Something remembered.\n", encoding="utf-8")
    outside = tmp_path / "outside.md"
    outside.write_text("Not the brain's memory.\n", encoding="utf-8")
    (memories / "USER.md").symlink_to(outside)
    (memories / "notes").mkdir()
    (memories / "notes" / "deep.md").write_text("Nested and not read.\n", encoding="utf-8")

    assert _exporter(memories, vault).export_once() == PAGE

    text, _ = vault.read(PAGE)
    assert "## MEMORY.md" in text
    assert "Not the brain's memory." not in text
    assert "Nested and not read." not in text
    assert "## USER.md" not in text


def test_a_huge_file_is_capped(memories: Path, vault: Vault) -> None:
    (memories / "MEMORY.md").write_text("x" * (memory_export.MAX_CHARS + 5_000), encoding="utf-8")

    _exporter(memories, vault).export_once()

    text, _ = vault.read(PAGE)
    assert f"```\n{'x' * memory_export.MAX_CHARS}\n```" in text
    assert "x" * (memory_export.MAX_CHARS + 1) not in text


def test_a_page_outside_the_vault_is_refused() -> None:
    with pytest.raises(ValueError, match="leave the vault"):
        memory_export.MemoryExportConfig(page="_brain/../policy.md")


def test_a_page_that_is_not_markdown_is_refused() -> None:
    with pytest.raises(ValueError, match="markdown"):
        memory_export.MemoryExportConfig(page="_brain/memory.txt")


def test_a_page_is_required() -> None:
    with pytest.raises(ValueError):
        memory_export.MemoryExportConfig()


def _commits(vault: Vault) -> str:
    return subprocess.run(
        ["git", "-C", str(vault.root), "rev-list", "--count", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def test_a_new_day_alone_does_not_rewrite_the_page(memories: Path, vault: Vault) -> None:
    (memories / "MEMORY.md").write_text("Something remembered.\n", encoding="utf-8")
    _exporter(memories, vault).export_once()
    before = _commits(vault)

    next_day = memory_export.MemoryExporter(
        memories, PAGE, vault, clock=lambda: datetime(2026, 10, 22, tzinfo=UTC)
    )
    assert next_day.export_once() is None
    assert _commits(vault) == before


def test_backticks_in_a_memory_cannot_close_its_block(memories: Path, vault: Vault) -> None:
    (memories / "MEMORY.md").write_text("Use ```code``` and ````more````.\n", encoding="utf-8")
    _exporter(memories, vault).export_once()
    text, _ = vault.read(PAGE)
    assert "\n`````\nUse ```code``` and ````more````.\n`````" in text
