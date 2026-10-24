"""The document archive tools: store an original, list the categories, discard a mistake.

The originals of personal documents live in a folder outside the vault, organized with numeric
codes in the Johnny.Decimal style: `10-19 Home/11 House/11.03 Insurance policy.pdf`. Hermes never
reads this archive. brain_tools is the only writer and it only ever adds: no tool here deletes or
overwrites anything in the archive. `archive_store` copies a chat attachment into a category and
refuses to replace an existing file. The attachment is read from Hermes' media folder or from the
attachment inbox brain_guard fills from Hermes' cache, both mounted read-only. `archive_discard`
moves a file into the discard folder, keeping its relative path and a timestamp in the name. The
model picks a code from `archive_categories`, so it cannot invent a category.

Item numbering: a two-digit `category_code` (`11`) gets the next free item number of that category
(`11.01`, `11.02`, ...). An item-level `category_code` (`11.03`) is kept as given, so the file is
`11.03 <name>.<ext>`: dropping the code from the name would lose the only pointer the vault note
has to the original. A name already taken in the same category gets ` (2)`, ` (3)` appended.

An archive kept in item folders (`11 House/11.04 Porto/lease.pdf`) keeps that shape: an item code
with a folder files into it under the given name, and a new item folder is created only from a
`folder` name, after checking that the category has no folder by that name already. Once any
category has item folders, a bare category code is refused, so a file never lands loose.

Error codes: `outside_archive`, `unknown_category`, `not_found`, `bad_source`, `bad_extension`,
`already_discarded`, `no_number_left`, `needs_item` and `bad_folder`.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import unicodedata
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
# The volume brain_guard copies chat attachments into, at the same path in Hermes and brain_tools.
ATTACHMENT_INBOX = Path("/attachments")

_PATH_SEPARATORS = re.compile(r"[\\/]+")
_NAME = re.compile(r"^[\w .-]+$")
_AREA_FOLDER = re.compile(r"^(\d{2})(?:-\d{2})?[\s._-]+(\S.*)$")
_CATEGORY_FOLDER = re.compile(r"^(\d{2})(?:\.\d{2})?[\s._-]+(\S.*)$")
_CODE = re.compile(r"^(\d{2})(?:\.(\d{2}))?$")
_ITEM_FOLDER = re.compile(r"^(\d{2})\.(\d{2})[\s._-]+(\S.*)$")
_EXAMPLES = 3
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
    # Where brain_guard copies a chat attachment from Hermes' own cache, which brain_tools cannot read.
    attachment_inbox: Path | None = None
    discard_dir: str = "_scarti"
    # Wider when the archive lives in another service's data folder, as the vault's file_mode.
    file_mode: int = Field(default=0o644, ge=0o600, le=0o666)
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
        description="File name of the chat attachment: the last part of the path Hermes gave, no folder.",
    )
    category_code: str = Field(
        description="Code from archive_categories: '11' to get the next item number, or '11.03' "
        "to keep that item code.",
    )
    name: str = Field(
        min_length=2,
        max_length=80,
        description="What the document is, without the extension: letters, digits, spaces, "
        "hyphen, underscore, dot. Inside an item folder, follow the style of its examples.",
    )
    folder: str | None = Field(
        default=None,
        min_length=2,
        max_length=60,
        description="With a two-digit category_code: the item folder to file into. An existing "
        "one with that name is reused, otherwise a new numbered one is created. Leave it out "
        "with an item code like '11.04'.",
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

    @field_validator("name", "folder")
    @classmethod
    def _check_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
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
class Item:
    """An item folder inside a category, `11.04 Porto`, with a few of the file names it holds."""

    code: str
    name: str
    path: str
    examples: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {"code": self.code, "name": self.name, "path": self.path, "examples": list(self.examples)}


@dataclass(frozen=True, slots=True)
class Category:
    code: str
    name: str
    path: str
    area_code: str
    area_name: str
    items: tuple[Item, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "name": self.name,
            "path": self.path,
            "area_code": self.area_code,
            "area_name": self.area_name,
            "items": [item.as_dict() for item in self.items],
        }


@dataclass(frozen=True, slots=True)
class StoredItem:
    path: str
    sha256: str
    created_folder: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {"path": self.path, "sha256": self.sha256, "created_folder": self.created_folder}


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


def _copy_exclusive(source: Path, target: Path, mode: int) -> None:
    """Copy without ever replacing a file: O_EXCL makes an existing target an error."""
    handle_fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(handle_fd, "wb") as out, source.open("rb") as origin:
            shutil.copyfileobj(origin, out, 1 << 20)
            out.flush()
            os.fsync(out.fileno())
            os.fchmod(out.fileno(), mode)
    except BaseException:
        target.unlink(missing_ok=True)
        raise


class Archive:
    """One archive folder. Every write adds a file, none replaces or removes one."""

    def __init__(self, config: ArchiveConfig) -> None:
        self._config = config
        self._root = config.root.resolve()
        self._sources = [config.inbox_media_dir.resolve()]
        if config.attachment_inbox is not None:
            self._sources.append(config.attachment_inbox.resolve())
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
                code = category.group(1)
                path = f"{area_path.name}/{category_path.name}"
                found.append(
                    Category(
                        code=code,
                        name=category.group(2).strip(),
                        path=path,
                        area_code=area.group(1),
                        area_name=area.group(2).strip(),
                        items=tuple(self._items(category_path, code, path)),
                    )
                )
        return sorted(found, key=lambda entry: (entry.code, entry.path))

    def store(self, source: str, category_code: str, name: str, folder: str | None = None) -> StoredItem:
        """File the attachment into the item folder that fits, or as a numbered file.

        An archive organized in item folders (`11 House/11.04 Porto/...`) keeps that shape: an item
        code that has a folder files into it, a new folder is made only from a name the model gave
        and only when no folder of that category already has the same name.
        """
        code, item = _CODE.match(category_code).group(1, 2)  # type: ignore[union-attr]
        category = self._category(code)
        origin = self._source(source)
        extension = origin.suffix.lower().lstrip(".")
        if extension not in self._config.allowed_extensions:
            allowed = ", ".join(self._config.allowed_extensions)
            raise ArchiveError("bad_extension", f"{extension or 'no extension'} is not one of {allowed}")
        category_dir = self._root / category.path
        if not category_dir.is_dir():
            raise ArchiveError("unknown_category", f"{category.path} is no longer a folder")
        stem = _strip_extension(name, extension)
        if item is not None and folder is not None:
            raise ArchiveError("bad_folder", "give either an item code or a folder name, not both")
        if item is not None:
            existing = next((entry for entry in category.items if entry.code == f"{code}.{item}"), None)
            if existing is not None:
                return self._stored(self._place(origin, self._root / existing.path, "", stem, extension))
            return self._stored(self._place(origin, category_dir, f"{code}.{item}", stem, extension))
        if folder is not None:
            return self._store_in_folder(origin, category, category_dir, folder, stem, extension)
        if category.items:
            known = "; ".join(f"{entry.code} {entry.name}" for entry in category.items)
            raise ArchiveError(
                "needs_item",
                f"category {code} keeps its documents in item folders ({known}): pass one of those "
                "codes, or a `folder` name for a new one",
            )
        if any(other.items for other in self.categories()):
            raise ArchiveError(
                "needs_item",
                f"this archive keeps documents in item folders and category {code} has none yet: "
                "pass a `folder` name for its first one",
            )
        prefix = f"{code}.{self._next_item_number(category_dir, code):02d}"
        return self._stored(self._place(origin, category_dir, prefix, stem, extension))

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

    def _store_in_folder(
        self, origin: Path, category: Category, category_dir: Path, folder: str, stem: str, extension: str
    ) -> StoredItem:
        wanted = _comparable(folder)
        for entry in category.items:
            if _comparable(entry.name) == wanted:
                return self._stored(self._place(origin, self._root / entry.path, "", stem, extension))
        number = self._next_item_number(category_dir, category.code)
        created = category_dir / f"{category.code}.{number:02d} {folder}"
        created.mkdir()
        target = self._place(origin, created, "", stem, extension)
        return self._stored(target, created_folder=created.relative_to(self._root).as_posix())

    def _stored(self, target: Path, created_folder: str | None = None) -> StoredItem:
        return StoredItem(
            path=target.relative_to(self._root).as_posix(),
            sha256=_sha256(target),
            created_folder=created_folder,
        )

    def _items(self, category_dir: Path, code: str, category_path: str) -> list[Item]:
        items: list[Item] = []
        for entry in self._folders(category_dir):
            match = _ITEM_FOLDER.match(entry.name)
            if match is None or match.group(1) != code:
                continue
            files = sorted(
                (child for child in entry.iterdir() if child.is_file() and not child.name.startswith(".")),
                key=lambda child: child.stat().st_mtime,
                reverse=True,
            )
            items.append(
                Item(
                    code=f"{code}.{match.group(2)}",
                    name=match.group(3).strip(),
                    path=f"{category_path}/{entry.name}",
                    examples=tuple(child.name for child in files[:_EXAMPLES]),
                )
            )
        return items

    def _category(self, code: str) -> Category:
        available = self.categories()
        for category in available:
            if category.code == code:
                return category
        known = ", ".join(entry.code for entry in available) or "none"
        raise ArchiveError("unknown_category", f"no category {code} in the archive, known: {known}")

    def _source(self, name: str) -> Path:
        for folder in self._sources:
            try:
                resolved = (folder / name).resolve(strict=True)
            except OSError:
                continue
            if folder not in resolved.parents:
                raise ArchiveError("outside_archive", f"{name} resolves outside the folder it was found in")
            if not resolved.is_file():
                raise ArchiveError("not_found", f"{name} is not a file")
            return resolved
        raise ArchiveError("not_found", f"{name} is not among the attachments")

    def _place(self, origin: Path, folder: Path, prefix: str, name: str, extension: str) -> Path:
        lead = f"{prefix} " if prefix else ""
        for index in range(1, _MAX_COPIES):
            suffix = "" if index == 1 else f" ({index})"
            target = folder / f"{lead}{name}{suffix}.{extension}"
            try:
                _copy_exclusive(origin, target, self._config.file_mode)
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


def _comparable(name: str) -> str:
    """A folder name reduced to what a person means by it: no case, accents or separators."""
    plain = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().casefold()
    return " ".join(re.split(r"[\s._-]+", plain)).strip()


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
        return _guarded(
            lambda: archive.store(params.source, params.category_code, params.name, params.folder).as_dict()
        )

    def discard(params: BaseModel, provenance: Provenance) -> dict[str, Any]:
        assert isinstance(params, DiscardParams)
        return _guarded(lambda: {"path": archive.discard(params.path), "reason": params.reason})

    return [
        Tool(
            spec=ToolSpec(
                name="archive_categories",
                description=(
                    "List the category folders of the document archive with their numeric code "
                    "and name, and the item folders inside each one (`11.04 Porto`) with a few "
                    "file names as examples. Use a code from this list for archive_store: a code "
                    "that is not here is refused."
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
                    "Copy a chat attachment into a category of the document "
                    "archive and return its path and sha256. It copies, never moves, and never "
                    "overwrites: a name already taken gets a number appended. Allowed extensions "
                    "only, and the source is the attachment's file name, never a path."
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
