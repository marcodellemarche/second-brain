"""Copy the brain's own memory into a vault page, so it has a history and the owner can read it.

Hermes keeps what it remembers in two files under its home: `MEMORY.md`, the notes it wrote about
itself, and `USER.md`, the profile of the owner. Both are written only after the owner approves the
change, and both are invisible: no history, nothing in the vault, nothing another page can link.

This exporter reads that folder through a read-only mount and writes one page of the vault with
what it found. It is a copy in one direction only: the page is overwritten by the brain on the next
tick, so editing it changes nothing, and the only way to change the memory is to ask the brain.

The folder is the only thing it reads, and only the `*.md` files directly inside it: a subfolder, a
symlink and anything that is not a regular file are ignored, so a link to `/etc` or to the vault
itself is not a way in.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

from pydantic import BaseModel, ConfigDict, Field, field_validator

from second_brain.tools.vault import Provenance, Vault, VaultError

log = logging.getLogger(__name__)

SOURCE_DIR = "/hermes-memories"
# A memory file is a handful of paragraphs. The cap keeps one runaway file from filling the vault,
# and it is per file, so two big memories cannot add up to an unbounded page either.
MAX_CHARS = 20_000
MAX_POLL_SECONDS = 86_400

INTRO = (
    "This page is a copy of what the brain keeps in its own memory, the notes it writes about "
    "itself and the profile it keeps of the owner. Every line of it was approved by the owner, and "
    "the brain overwrites this page on every export, so editing it here changes nothing: to change "
    "the memory, ask the brain."
)


def _check_page(value: str) -> str:
    """A vault-relative `.md` path that stays inside the vault."""
    text = value.strip()
    if not text:
        raise ValueError("page must not be empty")
    path = PurePosixPath(text)
    if path.is_absolute():
        raise ValueError(f"page must be relative to the vault: {value!r}")
    if ".." in path.parts:
        raise ValueError(f"page must not leave the vault: {value!r}")
    if path.suffix.lower() != ".md":
        raise ValueError(f"page must be a markdown file: {value!r}")
    return str(path)


def _files(source: Path) -> list[Path]:
    """The memory files directly in the folder, sorted. A missing folder is nothing to export yet."""
    if not source.is_dir():
        return []
    found: list[Path] = []
    for path in sorted(source.iterdir(), key=lambda entry: entry.name):
        if path.suffix.lower() != ".md" or path.name.startswith("."):
            continue
        # `is_file` follows a symlink, and a link out of the folder is exactly what this refuses.
        if path.is_symlink() or not path.is_file():
            continue
        found.append(path)
    return found


def _read(path: Path) -> str:
    text = path.read_text(encoding="utf-8", errors="replace")
    return text[:MAX_CHARS]


def _fence(text: str) -> str:
    """A code fence longer than any run of backticks in the text, so a memory cannot close it."""
    longest = max((len(run) for run in re.findall(r"`+", text)), default=0)
    return "`" * max(3, longest + 1)


def _without_date(page: str) -> str:
    return re.sub(r"^updated: .*$", "", page, count=1, flags=re.MULTILINE)


def render(files: list[tuple[str, str]], today: str) -> str:
    """The page: a frontmatter, a paragraph saying what this is, then one section per memory file."""
    frontmatter = f"type: brain\nupdated: {today}\nindex: false"
    sections = []
    for name, text in files:
        fence = _fence(text)
        sections.append(f"## {name}\n\n{fence}\n{text.rstrip()}\n{fence}")
    body = "\n\n".join([INTRO, *sections])
    return f"---\n{frontmatter}\n---\n\n# Memory\n\n{body}\n"


class MemoryExporter:
    """Copy the memory files into one vault page, and only write when something changed."""

    def __init__(
        self,
        source_dir: Path | str,
        page: str,
        vault: Vault,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._source = Path(source_dir)
        self._page = page
        self._vault = vault
        self._clock = clock or (lambda: datetime.now(UTC))

    @property
    def page(self) -> str:
        return self._page

    @property
    def source_dir(self) -> Path:
        return self._source

    def export_once(self) -> str | None:
        """Write the page when it differs from what is there, and return its path. None if nothing to do."""
        files = _files(self._source)
        if not files:
            return None
        content = render([(path.name, _read(path)) for path in files], self._clock().strftime("%Y-%m-%d"))
        try:
            current, sha = self._vault.read(self._page)
        except VaultError as exc:
            if exc.code != "not_found":
                raise
            current, sha = None, None
        # The date alone is not a change: the page is rewritten only when the memory moved.
        if current is not None and _without_date(current) == _without_date(content):
            return None
        self._vault.write(self._page, content, self._provenance(), expected_sha256=sha)
        log.info("exported the memory to %s", self._page)
        return self._page

    @staticmethod
    def _provenance() -> Provenance:
        return Provenance(
            requested_by="memory-export",
            reason="copy of the brain's own memory",
            source_integrity="trusted",
            session="memory-export",
        )


class MemoryExportConfig(BaseModel):
    """Where the memory is mounted and which page holds the copy: no secret of its own."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    source_dir: Path = Field(default=Path(SOURCE_DIR))
    page: str = Field(description="Vault-relative page the copy is written to.")
    poll_seconds: float = Field(default=3600, gt=0, le=MAX_POLL_SECONDS)

    @field_validator("page")
    @classmethod
    def _page(cls, value: str) -> str:
        return _check_page(value)

    def exporter(self, vault: Vault) -> MemoryExporter:
        return MemoryExporter(self.source_dir, self.page, vault)


__all__ = [
    "INTRO",
    "MAX_CHARS",
    "MAX_POLL_SECONDS",
    "SOURCE_DIR",
    "MemoryExportConfig",
    "MemoryExporter",
    "render",
]
