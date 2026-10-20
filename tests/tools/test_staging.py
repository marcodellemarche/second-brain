"""The staging importer: text only, no secrets, no overwrite, one untrusted inbox note each.

The staging folder is the only path a candidate from another brain takes into this vault, so the
tests here are about what it refuses as much as about what it writes.
"""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

from second_brain.core.schema import load_schema
from second_brain.tools import staging
from second_brain.tools.vault import Vault, VaultConfig

SCHEMA = Path(__file__).resolve().parents[2] / "templates" / "vault" / "en"
FIXED_NOW = datetime(2026, 10, 21, 9, 30, tzinfo=UTC)


def _git(root: Path) -> None:
    subprocess.run(["git", "init", "-q", str(root)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(root), "config", "user.email", "brain@localhost"], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.name", "brain"], check=True)


@pytest.fixture()
def vault(tmp_path: Path) -> Vault:
    root = tmp_path / "vault"
    root.mkdir()
    for path in sorted(SCHEMA.rglob("*")):
        if path.is_file():
            target = root / path.relative_to(SCHEMA)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(path.read_bytes())
    _git(root)
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(root), "commit", "-qm", "scaffold"], check=True, capture_output=True)
    return Vault(VaultConfig(root=root))


@pytest.fixture()
def importer(vault: Vault, tmp_path: Path) -> staging.StagingImporter:
    folder = tmp_path / "staging"
    folder.mkdir()
    return staging.StagingImporter(folder, vault, load_schema(vault.root), clock=lambda: FIXED_NOW)


def test_a_candidate_becomes_an_untrusted_inbox_capture(importer: staging.StagingImporter) -> None:
    (importer.staging_dir / "gateway shard notes.md").write_text(
        "---\ntype: topic\ntags: [gateway, invented]\nsources: [https://example.test/a]\n"
        "domain: acme\n---\n\nSome text.\n",
        encoding="utf-8",
    )
    results = importer.sweep()
    assert len(results) == 1
    result = results[0]
    assert result.ok, result.error
    assert result.note == "inbox/2026-10-21-gateway-shard-notes.md"
    text = (importer._vault.root / str(result.note)).read_text(encoding="utf-8")
    assert "type: capture" in text
    assert "trust: untrusted" in text
    assert "original: gateway shard notes.md" in text
    assert "https://example.test/a" in text
    assert "domain: acme" in text
    # A tag the schema does not list is dropped rather than written into a closed taxonomy.
    assert "invented" not in text
    assert "gateway" in text
    # The importer writes its own frontmatter: the candidate's type does not survive.
    assert "type: topic" not in text
    assert text.count("---") == 2
    assert "Some text." in text
    assert (importer.staging_dir / "done" / "gateway shard notes.md").is_file()


def test_the_import_is_one_commit_the_brain_can_undo(importer: staging.StagingImporter) -> None:
    (importer.staging_dir / "note.md").write_text("body", encoding="utf-8")
    before = len(importer._vault.log(limit=50))
    result = importer.sweep()[0]
    assert result.ok
    after = importer._vault.log(limit=50)
    assert len(after) == before + 1
    assert after[0]["author"] == "brain"
    assert result.note is not None
    assert result.note in after[0]["paths"]


def test_a_file_that_looks_like_a_secret_is_refused(importer: staging.StagingImporter) -> None:
    (importer.staging_dir / "creds.md").write_text("aws: AKIAIOSFODNN7EXAMPLE\n", encoding="utf-8")
    results = importer.sweep()
    assert len(results) == 1
    assert results[0].ok is False
    assert "secret" in str(results[0].error)
    # The matched value is never written into the reason file.
    reason = (importer.staging_dir / "failed" / f"creds.md{staging.REASON_SUFFIX}").read_text(
        encoding="utf-8"
    )
    assert "AKIAIOSFODNN7EXAMPLE" not in reason
    assert "aws_access_key_id" in reason
    assert (importer.staging_dir / "failed" / "creds.md").is_file()
    assert not list((importer._vault.root / "inbox").glob("*.md"))


@pytest.mark.parametrize("name", ["a.pdf", "a.zip", "a.png", "a"])
def test_only_text_notes_are_imported(importer: staging.StagingImporter, name: str) -> None:
    (importer.staging_dir / name).write_bytes(b"%PDF-1.4 not a note")
    results = importer.sweep()
    assert results[0].ok is False
    assert "not a text note" in str(results[0].error)


def test_an_empty_file_is_refused(importer: staging.StagingImporter) -> None:
    (importer.staging_dir / "empty.md").write_text("   \n", encoding="utf-8")
    assert importer.sweep()[0].ok is False


def test_a_binary_disguised_as_text_is_refused(importer: staging.StagingImporter) -> None:
    (importer.staging_dir / "weird.md").write_bytes(b"\xff\xfe\x00\x00bad")
    results = importer.sweep()
    assert results[0].ok is False
    assert "UTF-8" in str(results[0].error)


def test_the_same_candidate_twice_does_not_overwrite_the_first(importer: staging.StagingImporter) -> None:
    first = importer.staging_dir / "note.md"
    first.write_text("one", encoding="utf-8")
    first_result = importer.sweep()[0]
    second = importer.staging_dir / "note.md"
    second.write_text("two", encoding="utf-8")
    second_result = importer.sweep()[0]
    assert first_result.ok and second_result.ok
    assert first_result.note != second_result.note
    assert second_result.note is not None
    assert second_result.note.startswith("inbox/2026-10-21-note-")
    kept = (importer._vault.root / str(first_result.note)).read_text(encoding="utf-8")
    assert "one" in kept


def test_a_missing_staging_folder_imports_nothing(tmp_path: Path, vault: Vault) -> None:
    importer = staging.StagingImporter(tmp_path / "gone", vault, load_schema(vault.root))
    assert importer.sweep() == []


def test_hidden_files_and_folders_are_skipped(importer: staging.StagingImporter) -> None:
    (importer.staging_dir / ".hidden.md").write_text("x", encoding="utf-8")
    (importer.staging_dir / "done").mkdir()
    assert importer.sweep() == []


def test_the_slug_is_ascii_and_short() -> None:
    assert staging._slug("Caffè e città") == "caffe-e-citta"
    assert staging._slug("🚲 e 💃🏻") == "e"
    assert len(staging._slug("x" * 200)) == staging.MAX_STEM


def test_a_file_over_the_size_limit_is_refused(importer: staging.StagingImporter) -> None:
    (importer.staging_dir / "big.md").write_text("x" * (staging.MAX_BYTES + 1), encoding="utf-8")
    results = importer.sweep()
    assert results[0].ok is False
    assert "limit" in str(results[0].error)


def test_the_body_loses_the_candidate_frontmatter() -> None:
    text = "---\ntype: topic\n---\n\nbody here\n"
    assert staging.body_of(text).strip() == "body here"
    assert staging.body_of("no frontmatter") == "no frontmatter"
