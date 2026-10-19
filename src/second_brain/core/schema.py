"""The vault schema: logical zones resolved to the folder names of one vault language."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from second_brain.core.types import Zone

SCHEMA_FILE = Path("_meta") / "SCHEMA.md"

_FENCED_YAML = re.compile(r"^```ya?ml[ \t]*\r?\n(.*?)^```[ \t]*$", re.DOTALL | re.MULTILINE)
_PATH_SEPARATORS = re.compile(r"[\\/]+")


class SchemaError(Exception):
    """The schema file is missing, unreadable, or does not describe a usable vault."""


class VaultSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")

    language: str = Field(pattern=r"^[a-z]{2}$")
    zones: dict[Zone, str]
    types: dict[str, str] = Field(min_length=1)
    tags: tuple[str, ...] = ()
    untrusted_zones: frozenset[Zone] = frozenset()

    @model_validator(mode="after")
    def _complete_and_flat(self) -> VaultSchema:
        missing = sorted(set(Zone) - set(self.zones))
        if missing:
            raise ValueError(f"zones is missing: {', '.join(missing)}")
        for zone, folder in self.zones.items():
            if not folder or folder in {".", ".."} or "/" in folder or "\\" in folder:
                raise ValueError(f"zone {zone} maps to an invalid folder name: {folder!r}")
        folders = list(self.zones.values())
        shared = sorted({folder for folder in folders if folders.count(folder) > 1})
        if shared:
            raise ValueError(f"two zones share a folder: {', '.join(shared)}")
        empty = sorted(name for name, meaning in self.types.items() if not meaning.strip())
        if empty:
            raise ValueError(f"types without a meaning: {', '.join(empty)}")
        return self

    def folder(self, zone: Zone) -> str:
        """The folder name of a zone in this vault."""
        folder = self.zones.get(zone)
        if folder is None:
            raise SchemaError(f"zone {zone} is not mapped")
        return folder

    def zone_of(self, path: str | Path) -> Zone | None:
        """The zone a vault-relative path belongs to, or None when it belongs to none."""
        parts = [part for part in _PATH_SEPARATORS.split(str(path)) if part and part != "."]
        if not parts or ".." in parts:
            return None
        head = parts[0]
        for zone, folder in self.zones.items():
            if folder == head:
                return zone
        return None


def _yaml_block(text: str, path: Path) -> dict[str, Any]:
    match = _FENCED_YAML.search(text)
    if match is None:
        raise SchemaError(f"no fenced YAML block in {path}")
    try:
        raw = yaml.safe_load(match.group(1))
    except yaml.YAMLError as exc:
        raise SchemaError(f"invalid YAML in {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise SchemaError(f"the YAML block in {path} is not a mapping")
    return raw


def load_schema(vault_root: str | Path) -> VaultSchema:
    """Load the schema of the vault at `vault_root` from its `_meta/SCHEMA.md`."""
    path = Path(vault_root) / SCHEMA_FILE
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise SchemaError(f"no schema at {path}") from None
    except OSError as exc:
        raise SchemaError(f"cannot read {path}: {exc}") from exc
    try:
        return VaultSchema.model_validate(_yaml_block(text, path))
    except ValidationError as exc:
        raise SchemaError(f"invalid schema in {path}: {exc}") from exc
