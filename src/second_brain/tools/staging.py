"""Bring a candidate the owner moved into the staging folder into the vault, as an untrusted capture.

The two brains have no federation: the personal brain writes a candidate somewhere, the owner moves
it here with `rsync`, and this importer is the only thing that reads that folder. It is deliberately
dumb and suspicious, because everything it sees was written by another brain:

- only text is imported. A PDF, an archive or an image is refused: the vault holds notes, and a
  binary in it would only be a file nobody can read;
- a file that looks like a secret is refused, and it stays in the staging folder with a
  `.reason.txt` next to it, so the owner sees what happened instead of losing it. The matched value
  is never written into the reason;
- a note that is already there (same name) gets a suffix instead of being overwritten, so moving the
  same folder twice cannot destroy the first import;
- the note lands in the inbox zone with `type: capture`, `trust: untrusted` and its original file
  name in `original`, and the file is then moved to `done/` (or to `failed/` with the reason).

The vault is written through `Vault`, so the write is one undoable commit and the product scanner
runs a second time inside it.
"""

from __future__ import annotations

import logging
import os
import shutil
import unicodedata
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field

from second_brain.core.schema import VaultSchema
from second_brain.core.types import Zone
from second_brain.tools.secrets import scan
from second_brain.tools.vault import Provenance, Vault, VaultError

log = logging.getLogger(__name__)

STAGING_PATH = "/staging"
DONE_DIR = "done"
FAILED_DIR = "failed"
MAX_BYTES = 512 * 1024
MAX_STEM = 60
TEXT_SUFFIXES = frozenset({".md", ".markdown", ".txt"})
REASON_SUFFIX = ".reason.txt"
# The frontmatter a candidate may carry from the brain that wrote it. Everything else is dropped:
# this importer writes the provenance of the note, not the other brain.
KEPT_KEYS = ("sources", "tags", "domain")


class StagingError(RuntimeError):
    """The staging folder cannot be read at all."""


@dataclass(frozen=True, slots=True)
class ImportResult:
    """One staged file, imported or refused. `note` is the vault path it was written to."""

    source: str
    note: str | None
    error: str | None

    @property
    def ok(self) -> bool:
        return self.error is None


def _slug(stem: str) -> str:
    """A file name stem: lowercase ASCII words, so a name with emoji or accents is still a path."""
    ascii_only = unicodedata.normalize("NFKD", stem).encode("ascii", "ignore").decode("ascii")
    words = "".join(ch if ch.isalnum() else " " for ch in ascii_only.lower()).split()
    return "-".join(words)[:MAX_STEM].strip("-") or "candidate"


def _frontmatter_of(text: str) -> dict[str, Any] | None:
    """The YAML block a candidate may already carry, or None. Anything else is body text."""
    if not text.startswith("---\n"):
        return None
    end = text.find("\n---", 4)
    if end < 0:
        return None
    try:
        loaded = yaml.safe_load(text[4:end])
    except yaml.YAMLError:
        return None
    return loaded if isinstance(loaded, dict) else None


def body_of(text: str) -> str:
    """The note without a leading frontmatter block: this importer writes its own."""
    if not text.startswith("---\n"):
        return text
    end = text.find("\n---", 4)
    if end < 0:
        return text
    rest = text[end + 4 :]
    return rest[1:] if rest.startswith("\n") else rest


def describe_findings(findings: list[Any]) -> str:
    """What was found, by rule and line: never the matched value."""
    where = ", ".join(f"{finding.rule} at line {finding.line}" for finding in findings[:5])
    return f"the file looks like it holds a secret ({where})"


class StagingImporter:
    """Turn the files the owner moved into the staging folder into untrusted inbox captures."""

    def __init__(
        self,
        staging_dir: Path | str,
        vault: Vault,
        schema: VaultSchema,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._staging = Path(staging_dir)
        self._vault = vault
        self._schema = schema
        self._clock = clock or (lambda: datetime.now(UTC))

    @property
    def staging_dir(self) -> Path:
        return self._staging

    def sweep(self) -> list[ImportResult]:
        """Import every staged file. One bad file never stops the sweep."""
        if not self._staging.is_dir():
            return []
        results: list[ImportResult] = []
        for path in sorted(self._staging.iterdir()):
            if path.is_dir() or path.name.startswith("."):
                continue
            try:
                results.append(self.import_file(path))
            except Exception:  # a broken file must not take the server down
                log.exception("could not import %s", path)
                results.append(ImportResult(path.name, None, "unexpected import failure"))
        return results

    def import_file(self, path: Path) -> ImportResult:
        """Import one staged file, moving it to `done/` or to `failed/` with a reason."""
        if path.suffix.lower() not in TEXT_SUFFIXES:
            return self._fail(path, f"not a text note: {path.suffix or 'no extension'}")
        try:
            size = path.stat().st_size
        except OSError as exc:
            return self._fail(path, f"cannot stat the file: {exc}")
        if size > MAX_BYTES:
            return self._fail(path, f"the file is {size} bytes, over the {MAX_BYTES} limit")
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            return self._fail(path, f"cannot read the file as UTF-8 text: {exc}")
        if not text.strip():
            return self._fail(path, "the file is empty")

        findings = scan(text)
        if findings:
            return self._fail(path, describe_findings(findings))

        created = self._clock().strftime("%Y-%m-%d")
        note_path = self._note_path(created, path)
        try:
            self._vault.write(note_path, self._render(text, created, path), self._provenance(path))
        except VaultError as exc:
            return self._fail(path, f"{exc.code}: {exc.message}")
        self._move(path, DONE_DIR)
        log.info("imported %s as %s", path.name, note_path)
        return ImportResult(path.name, note_path, None)

    def _note_path(self, created: str, path: Path) -> str:
        folder = self._schema.folder(Zone.INBOX)
        stem = f"{created}-{_slug(path.stem)}"
        candidate = f"{folder}/{stem}.md"
        if not (self._vault.root / candidate).exists():
            return candidate
        # The same candidate moved twice in one day must not overwrite the first import.
        return f"{folder}/{stem}-{uuid.uuid4().hex[:8]}.md"

    def _render(self, text: str, created: str, path: Path) -> str:
        staged = _frontmatter_of(text) or {}
        frontmatter: dict[str, Any] = {
            "type": "capture",
            "created": created,
            "updated": created,
            "trust": "untrusted",
            "original": path.name,
        }
        sources = staged.get("sources")
        if isinstance(sources, list) and sources:
            frontmatter["sources"] = [str(item) for item in sources][:20]
        tags = [str(tag) for tag in staged.get("tags") or [] if str(tag) in self._schema.tags]
        if tags:
            frontmatter["tags"] = tags
        domain = staged.get("domain")
        if isinstance(domain, str) and domain.strip():
            frontmatter["domain"] = domain.strip()[:60]
        head = yaml.safe_dump(frontmatter, sort_keys=False, allow_unicode=True).rstrip()
        return f"---\n{head}\n---\n\n{body_of(text).strip()}\n"

    @staticmethod
    def _provenance(path: Path) -> Provenance:
        return Provenance(
            requested_by="staging-import",
            reason=f"a candidate moved in from the personal brain ({path.name})",
            source_integrity="untrusted",
            session="staging",
        )

    def _move(self, path: Path, subdir: str) -> Path:
        target_dir = self._staging / subdir
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / path.name
        if target.exists():
            target = target_dir / f"{path.stem}-{uuid.uuid4().hex[:8]}{path.suffix}"
        shutil.move(str(path), str(target))
        return target

    def _fail(self, path: Path, reason: str) -> ImportResult:
        log.warning("not importing %s: %s", path.name, reason)
        moved = self._move(path, FAILED_DIR)
        reason_file = moved.with_name(f"{moved.name}{REASON_SUFFIX}")
        reason_file.write_text(f"{reason}\n", encoding="utf-8")
        os.chmod(reason_file, 0o600)
        return ImportResult(path.name, None, reason)


class StagingConfig(BaseModel):
    """Where the candidates land, and nothing else: the importer needs no secret of its own."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    dir: Path = Field(default=Path(STAGING_PATH))
    poll_seconds: float = Field(default=60, gt=0)

    def importer(self, vault: Vault, schema: VaultSchema) -> StagingImporter:
        return StagingImporter(self.dir, vault, schema)


__all__ = [
    "DONE_DIR",
    "FAILED_DIR",
    "KEPT_KEYS",
    "MAX_BYTES",
    "REASON_SUFFIX",
    "STAGING_PATH",
    "TEXT_SUFFIXES",
    "ImportResult",
    "StagingConfig",
    "StagingError",
    "StagingImporter",
    "body_of",
    "describe_findings",
]
