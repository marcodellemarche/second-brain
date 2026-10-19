"""The document archive tools: store an original, list the categories, discard a mistake.

The originals of personal documents live in a folder outside the vault, organized with numeric
codes in the Johnny.Decimal style: `10-19 Home/11 House/11.03 Insurance policy.pdf`. Hermes never
reads this archive. brain_tools is the only writer and it only ever adds: no tool here deletes or
overwrites anything in the archive. `archive_store` copies a chat attachment from Hermes' media
folder (mounted read-only) into a category and refuses to replace an existing file, and
`archive_discard` moves a file into the discard folder, keeping its relative path and a timestamp
in the name. The model picks a code from `archive_categories`, so it cannot invent a category.

Item numbering: a two-digit `category_code` (`11`) gets the next free item number of that category
(`11.01`, `11.02`, ...). An item-level `category_code` (`11.03`) is kept as given, so the file is
`11.03 <name>.<ext>`: dropping the code from the name would lose the only pointer the vault note
has to the original. A name already taken in the same category gets ` (2)`, ` (3)` appended.

Error codes: `outside_archive`, `unknown_category`, `not_found`, `bad_source`, `bad_extension`,
`already_discarded` and `no_number_left`.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from second_brain.core.types import Autonomy, Effect, ToolSpec
from second_brain.tools.registry import Tool
from second_brain.tools.vault import Provenance

ARCHIVE_HOSTS: tuple[str, ...] = ()
ARCHIVE_CREDENTIALS: tuple[str, ...] = ()

_PATH_SEPARATORS = re.compile(r"[\\/]+")
_NAME = re.compile(r"^[\w .-]+$")
_AREA_FOLDER = re.compile(r"^(\d{2})(?:-\d{2})?[\s._-]+(\S.*)$")
_CATEGORY_FOLDER = re.compile(r"^(\d{2})(?:\.\d{2})?[\s._-]+(\S.*)$")
_CODE = re.compile(r"^(\d{2})(?:\.(\d{2}))?$")
_MAX_COPIES = 100


class ArchiveError(Exception):
    """A refused or failed archive operation. `code` is stable and machine-readable."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


class ArchiveConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    root: Path
    inbox_media_dir: Path
    discard_dir: str = "_scarti"
    allowed_extensions: tuple[str, ...] = ("pdf", "jpg", "jpeg", "png", "heic", "webp")

    @field_validator("discard_dir")
    @classmethod
    def _check_discard_dir(cls, value: str) -> str:
        segments = [part for part in _PATH_SEPARATORS.split(value.strip()) if part]
        if len(segments) != 1 or segments[0] in (".", ".."):
            raise ValueError(f"discard_dir must be a single folder name, got {value!r}")
        return segments[0]

    @field_validator("allowed_extensions")
    @classmethod
    def _check_extensions(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        cleaned: list[str] = []
        for item in value:
            extension = item.strip().lower().lstrip(".")
            if not extension or not extension.isalnum():
                raise ValueError(f"not an extension: {item!r}")
            if extension not in cleaned:
                cleaned.append(extension)
        if not cleaned:
            raise ValueError("at least one extension must be allowed")
        return tuple(cleaned)


class CategoriesParams(BaseModel):
    model_config = ConfigDict(extra="forbid")


class StoreParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: str = Field(
        min_length=1,
        max_length=255,
        description="File name of the attachment inside the media folder, without any folder.",
    )
    category_code: str = Field(
        description="Code from archive_categories: '11' to get the next item number, or '11.03' "
        "to keep that item code.",
    )
    name: str = Field(
        min_length=2,
        max_length=80,
        description="What the document is, without the extension: letters, digits, spaces, "
        "hyphen, underscore, dot.",
    )

    @field_validator("source")
    @classmethod
    def _check_source(cls, value: str) -> str:
        if _PATH_SEPARATORS.search(value) or value.strip() in (".", ".."):
            raise ValueError("source must be a file name, not a path")
        return value.strip()

    @field_validator("category_code")
    @classmethod
    def _check_category_code(cls, value: str) -> str:
        if not _CODE.match(value.strip()):
            raise ValueError(f"category_code must be two digits or two digits and two digits: {value!r}")
        return value.strip()

    @field_validator("name")
    @classmethod
    def _check_name(cls, value: str) -> str:
        if not _NAME.match(value):
            raise ValueError("name may only contain letters, digits, spaces, '-', '_' and '.'")
        if value != value.strip():
            raise ValueError("name cannot start or end with a space")
        if value.startswith(".") or ".." in value:
            raise ValueError(f"not a usable name: {value!r}")
        return value


class DiscardParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str = Field(min_length=1, description="Archive-relative path of the file, as stored.")
    reason: str = Field(min_length=1, max_length=200, description="Why the file is discarded, in one line.")


@dataclass(frozen=True, slots=True)
class Category:
    code: str
    name: str
    path: str
    area_code: str
    area_name: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "name": self.name,
            "path": self.path,
            "area_code": self.area_code,
            "area_name": self.area_name,
        }


@dataclass(frozen=True, slots=True)
class StoredItem:
    path: str
    sha256: str

    def as_dict(self) -> dict[str, Any]:
        return {"path": self.path, "sha256": self.sha256}


def _segments(path: str) -> list[str]:
    return [part for part in _PATH_SEPARATORS.split(path.strip()) if part not in ("", ".")]


def _inside(rel: str, prefix: str) -> bool:
    return rel == prefix or rel.startswith(prefix + "/")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _copy_exclusive(source: Path, target: Path) -> None:
    """Copy without ever replacing a file: O_EXCL makes an existing target an error."""
    handle_fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    try:
        with os.fdopen(handle_fd, "wb") as out, source.open("rb") as origin:
            shutil.copyfileobj(origin, out, 1 << 20)
            out.flush()
            os.fsync(out.fileno())
    except BaseException:
        target.unlink(missing_ok=True)
        raise


class Archive:
    """One archive folder. Every write adds a file, none replaces or removes one."""

    def __init__(self, config: ArchiveConfig) -> None:
        self._config = config
        self._root = config.root.resolve()
        self._media = config.inbox_media_dir.resolve()
        if not self._root.is_dir():
            raise ArchiveError("not_found", f"archive root is not a directory: {self._root}")
        self._discard = self._resolve(config.discard_dir)[0]

    @property
    def config(self) -> ArchiveConfig:
        return self._config

    def categories(self) -> list[Category]:
        """The category folders, two levels deep: area folder, then category folder."""
        found: list[Category] = []
        for area_path in self._folders(self._root):
            area = _AREA_FOLDER.match(area_path.name)
            if area is None:
                continue
            for category_path in self._folders(area_path):
                category = _CATEGORY_FOLDER.match(category_path.name)
                if category is None:
                    continue
                found.append(
                    Category(
                        code=category.group(1),
                        name=category.group(2).strip(),
                        path=f"{area_path.name}/{category_path.name}",
                        area_code=area.group(1),
                        area_name=area.group(2).strip(),
                    )
                )
        return sorted(found, key=lambda entry: (entry.code, entry.path))

    def store(self, source: str, category_code: str, name: str) -> StoredItem:
        code, item = _CODE.match(category_code).group(1, 2)  # type: ignore[union-attr]
        category = self._category(code)
        origin = self._source(source)
        extension = origin.suffix.lower().lstrip(".")
        if extension not in self._config.allowed_extensions:
            allowed = ", ".join(self._config.allowed_extensions)
            raise ArchiveError("bad_extension", f"{extension or 'no extension'} is not one of {allowed}")
        folder = self._root / category.path
        if not folder.is_dir():
            raise ArchiveError("unknown_category", f"{category.path} is no longer a folder")
        prefix = f"{code}.{item}" if item else f"{code}.{self._next_item_number(folder, code):02d}"
        target = self._place(origin, folder, prefix, _strip_extension(name, extension), extension)
        return StoredItem(path=target.relative_to(self._root).as_posix(), sha256=_sha256(target))

    def discard(self, path: str) -> str:
        rel, target = self._resolve(path)
        if _inside(rel, self._discard):
            raise ArchiveError("already_discarded", f"{rel} is already in the discard folder")
        if not target.is_file():
            raise ArchiveError("not_found", f"{rel} does not exist")
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%f")
        destination = self._root / self._discard / f"{rel}.{stamp}"
        destination.parent.mkdir(parents=True, exist_ok=True)
        index = 2
        while destination.exists():
            destination = self._root / self._discard / f"{rel}.{stamp}-{index}"
            index += 1
        target.rename(destination)
        return destination.relative_to(self._root).as_posix()

    def _category(self, code: str) -> Category:
        available = self.categories()
        for category in available:
            if category.code == code:
                return category
        known = ", ".join(entry.code for entry in available) or "none"
        raise ArchiveError("unknown_category", f"no category {code} in the archive, known: {known}")

    def _source(self, name: str) -> Path:
        candidate = self._media / name
        try:
            resolved = candidate.resolve(strict=True)
        except OSError:
            raise ArchiveError("not_found", f"{name} is not in the media folder") from None
        if self._media not in resolved.parents:
            raise ArchiveError("outside_archive", f"{name} resolves outside the media folder")
        if not resolved.is_file():
            raise ArchiveError("not_found", f"{name} is not a file")
        return resolved

    def _place(self, origin: Path, folder: Path, prefix: str, name: str, extension: str) -> Path:
        for index in range(1, _MAX_COPIES):
            suffix = "" if index == 1 else f" ({index})"
            target = folder / f"{prefix} {name}{suffix}.{extension}"
            try:
                _copy_exclusive(origin, target)
            except FileExistsError:
                continue
            return target
        raise ArchiveError("exists", f"no free name left for {prefix} {name}")

    def _next_item_number(self, folder: Path, code: str) -> int:
        pattern = re.compile(rf"^{re.escape(code)}\.(\d{{2}})[\s._-]")
        used = {
            int(match.group(1))
            for entry in folder.iterdir()
            if (match := pattern.match(entry.name)) is not None
        }
        for number in range(1, 100):
            if number not in used:
                return number
        raise ArchiveError("no_number_left", f"category {code} has no free item number left")

    def _folders(self, parent: Path) -> Iterator[Path]:
        """Subfolders that stay inside the archive, so a symlink cannot point the scan outside."""
        try:
            entries = sorted(parent.iterdir())
        except OSError:
            return
        for entry in entries:
            try:
                resolved = entry.resolve(strict=True)
            except OSError:
                continue
            if resolved.is_dir() and self._root in resolved.parents:
                yield entry

    def _resolve(self, path: str) -> tuple[str, Path]:
        """Resolve an archive-relative path, refusing anything that lands outside the root."""
        segments = _segments(path)
        if not segments:
            raise ArchiveError("outside_archive", f"empty path: {path!r}")
        if ".." in segments:
            raise ArchiveError("outside_archive", f"parent segment in path: {path!r}")
        if Path(path.strip()).is_absolute() or path.strip().startswith(("/", "\\")):
            raise ArchiveError("outside_archive", f"absolute path: {path!r}")
        target = (self._root / Path(*segments)).resolve()
        if self._root not in target.parents:
            raise ArchiveError("outside_archive", f"path leaves the archive: {path!r}")
        return target.relative_to(self._root).as_posix(), target


def _strip_extension(name: str, extension: str) -> str:
    suffix = f".{extension}"
    if name.lower().endswith(suffix) and len(name) > len(suffix):
        return name[: -len(suffix)].strip()
    return name


def _refused(exc: ArchiveError) -> dict[str, Any]:
    return {"ok": False, "code": exc.code, "message": exc.message}


def _guarded(run: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    """Run an archive operation, turning a refusal into a result instead of an exception."""
    try:
        return {"ok": True, **run()}
    except ArchiveError as exc:
        return _refused(exc)


def archive_tools(config: ArchiveConfig) -> list[Tool]:
    archive = Archive(config)

    def categories(params: BaseModel, provenance: Provenance) -> dict[str, Any]:
        assert isinstance(params, CategoriesParams)
        return _guarded(lambda: {"categories": [entry.as_dict() for entry in archive.categories()]})

    def store(params: BaseModel, provenance: Provenance) -> dict[str, Any]:
        assert isinstance(params, StoreParams)
        return _guarded(lambda: archive.store(params.source, params.category_code, params.name).as_dict())

    def discard(params: BaseModel, provenance: Provenance) -> dict[str, Any]:
        assert isinstance(params, DiscardParams)
        return _guarded(lambda: {"path": archive.discard(params.path), "reason": params.reason})

    return [
        Tool(
            spec=ToolSpec(
                name="archive_categories",
                description=(
                    "List the category folders of the document archive, two levels deep, with "
                    "their numeric code and name. Use a code from this list for archive_store: a "
                    "code that is not here is refused."
                ),
                params=CategoriesParams,
                effect=Effect.READ,
                autonomy=Autonomy.AUTO,
                hosts=ARCHIVE_HOSTS,
                credentials=ARCHIVE_CREDENTIALS,
            ),
            handler=categories,
        ),
        Tool(
            spec=ToolSpec(
                name="archive_store",
                description=(
                    "Copy an attachment from the media folder into a category of the document "
                    "archive and return its path and sha256. It copies, never moves, and never "
                    "overwrites: a name already taken gets a number appended. Allowed extensions "
                    "only, and the source must be a file name inside the media folder."
                ),
                params=StoreParams,
                effect=Effect.INTERNAL,
                autonomy=Autonomy.NOTIFY,
                hosts=ARCHIVE_HOSTS,
                credentials=ARCHIVE_CREDENTIALS,
            ),
            handler=store,
        ),
        Tool(
            spec=ToolSpec(
                name="archive_discard",
                description=(
                    "Move a file of the archive into the discard folder, keeping its relative "
                    "path plus a UTC timestamp. Nothing is deleted, so a mistake can be undone by "
                    "hand. It always needs an approval."
                ),
                params=DiscardParams,
                effect=Effect.INTERNAL,
                autonomy=Autonomy.ASK,
                hosts=ARCHIVE_HOSTS,
                credentials=ARCHIVE_CREDENTIALS,
            ),
            handler=discard,
        ),
    ]
