"""The guard hands a chat attachment from Hermes' cache to brain_tools. Fake files only."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from second_brain.guard.attachments import INBOX_MAX_AGE_SECONDS, stage_attachment

PAYLOAD = b"%PDF-1.4 fake attachment\n"


@pytest.fixture
def cache(tmp_path: Path) -> tuple[Path, Path]:
    documents = tmp_path / "cache" / "documents"
    images = tmp_path / "cache" / "images"
    documents.mkdir(parents=True)
    images.mkdir(parents=True)
    return documents, images


@pytest.fixture
def inbox(tmp_path: Path) -> Path:
    folder = tmp_path / "attachments"
    folder.mkdir()
    return folder


def _sources(cache: tuple[Path, Path]) -> tuple[str, ...]:
    return tuple(str(folder) for folder in cache)


def test_copies_the_attachment_readable_by_others(cache: tuple[Path, Path], inbox: Path) -> None:
    origin = cache[0] / "doc_0a1b2c3d4e5f_Lease.pdf"
    origin.write_bytes(PAYLOAD)
    origin.chmod(0o600)
    staged = stage_attachment(origin.name, _sources(cache), str(inbox))
    assert staged == inbox / origin.name
    assert staged.read_bytes() == PAYLOAD
    assert stat.S_IMODE(staged.stat().st_mode) == 0o644


def test_finds_a_photo_in_the_images_cache(cache: tuple[Path, Path], inbox: Path) -> None:
    (cache[1] / "img_0a1b2c3d4e5f.jpg").write_bytes(b"jpeg\n")
    staged = stage_attachment("img_0a1b2c3d4e5f.jpg", _sources(cache), str(inbox))
    assert staged == inbox / "img_0a1b2c3d4e5f.jpg"


def test_leaves_a_copy_already_in_the_inbox(cache: tuple[Path, Path], inbox: Path) -> None:
    (cache[0] / "doc.pdf").write_bytes(PAYLOAD)
    (inbox / "doc.pdf").write_bytes(b"first copy\n")
    stage_attachment("doc.pdf", _sources(cache), str(inbox))
    assert (inbox / "doc.pdf").read_bytes() == b"first copy\n"


def test_a_missing_attachment_copies_nothing(cache: tuple[Path, Path], inbox: Path) -> None:
    assert stage_attachment("gone.pdf", _sources(cache), str(inbox)) is None
    assert list(inbox.iterdir()) == []


def test_without_an_inbox_nothing_happens(cache: tuple[Path, Path], tmp_path: Path) -> None:
    (cache[0] / "doc.pdf").write_bytes(PAYLOAD)
    assert stage_attachment("doc.pdf", _sources(cache), str(tmp_path / "missing")) is None


@pytest.mark.parametrize("name", ["", " doc.pdf", "..", ".", "../doc.pdf", "sub/doc.pdf", "sub\\doc.pdf"])
def test_only_a_bare_file_name_is_copied(cache: tuple[Path, Path], inbox: Path, name: str) -> None:
    (cache[0] / "doc.pdf").write_bytes(PAYLOAD)
    assert stage_attachment(name, _sources(cache), str(inbox)) is None
    assert list(inbox.iterdir()) == []


def test_a_link_out_of_the_cache_is_not_followed(
    cache: tuple[Path, Path], inbox: Path, tmp_path: Path
) -> None:
    secret = tmp_path / "state.db"
    secret.write_bytes(b"not an attachment\n")
    (cache[0] / "doc.pdf").symlink_to(secret)
    assert stage_attachment("doc.pdf", _sources(cache), str(inbox)) is None
    assert list(inbox.iterdir()) == []


def test_old_copies_are_dropped(cache: tuple[Path, Path], inbox: Path) -> None:
    old = inbox / "old.pdf"
    old.write_bytes(PAYLOAD)
    recent = inbox / "recent.pdf"
    recent.write_bytes(PAYLOAD)
    now = old.stat().st_mtime + INBOX_MAX_AGE_SECONDS + 60
    os.utime(recent, (now, now))
    stage_attachment("gone.pdf", _sources(cache), str(inbox), now=now)
    assert not old.exists()
    assert recent.exists()
