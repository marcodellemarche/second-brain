"""The Nextcloud link tool: a vault path in, an internal Files URL out.

The URL points at the user's own Files app and needs a login: it opens the folder of the file with
the file selected, so the user lands on it and can read, edit or share it from the browser. The
tool builds the URL, it does not call Nextcloud: no egress, no credential, nothing to authorize
beyond reading a path.

There is no tool for public share links, on purpose. A public link is a capability anyone holding
it can use, it survives the conversation, and creating one is a `visible` action the model should
never be able to take by itself. Sharing stays a decision the user makes in the Nextcloud UI.

Refusals: an absolute path or a `..` segment would point outside the configured folder, so both are
refused instead of being normalized away. Error code: `invalid_path`.
"""

from __future__ import annotations

import urllib.parse
from collections.abc import Callable
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from second_brain.core.types import Autonomy, Effect, ToolSpec
from second_brain.tools.registry import Tool
from second_brain.tools.vault import Provenance

LinkTarget = Literal["vault", "archive"]


class LinkError(Exception):
    """A refused link build. `code` is stable and machine-readable."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


def _folder(value: str) -> str:
    """A folder inside the Nextcloud user files, as a clean relative path with `/` separators."""
    segments = _segments(value)
    if not segments:
        raise ValueError("folder must not be empty")
    return "/".join(segments)


def _segments(path: str) -> list[str]:
    segments: list[str] = []
    for part in path.strip().split("/"):
        part = part.strip()
        if part in ("", "."):
            continue
        if part == "..":
            raise ValueError("a path may not contain '..'")
        segments.append(part)
    return segments


class LinkConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    base_url: str = Field(description="Nextcloud base URL, https only.")
    vault_folder: str = Field(description="Folder of the vault inside the Nextcloud user files.")
    archive_folder: str = Field(description="Folder of the archive inside the user files.")

    @field_validator("base_url")
    @classmethod
    def _check_base_url(cls, value: str) -> str:
        parsed = urllib.parse.urlsplit(value.strip())
        if parsed.scheme != "https":
            raise ValueError("base_url must be https")
        if not parsed.netloc:
            raise ValueError("base_url must name a host")
        if parsed.query or parsed.fragment:
            raise ValueError("base_url may not carry a query or a fragment")
        return f"{parsed.scheme}://{parsed.netloc}{parsed.path.rstrip('/')}"

    @field_validator("vault_folder", "archive_folder")
    @classmethod
    def _check_folder(cls, value: str) -> str:
        return _folder(value)

    @model_validator(mode="after")
    def _check_distinct(self) -> LinkConfig:
        if self.vault_folder == self.archive_folder:
            raise ValueError("vault_folder and archive_folder must differ")
        return self

    def folder(self, target: LinkTarget) -> str:
        return self.archive_folder if target == "archive" else self.vault_folder


class LinkParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target: LinkTarget = Field(
        default="vault", description="Which area of the vault the file is in: vault or archive."
    )
    path: str = Field(
        description="Path of the file relative to that area, for example 'topics/foo.md'.",
    )


def _relative(path: str) -> list[str]:
    """The segments of a relative file path, refusing anything that could climb out of the area."""
    if path.startswith(("/", "\\")):
        raise LinkError("invalid_path", "the path must be relative to the area, not absolute")
    if "\\" in path:
        raise LinkError("invalid_path", "the path must use forward slashes")
    if any(ord(char) < 32 for char in path):
        raise LinkError("invalid_path", "the path may not contain control characters")
    try:
        segments = _segments(path)
    except ValueError as exc:
        raise LinkError("invalid_path", str(exc)) from exc
    if not segments:
        raise LinkError("invalid_path", "the path is empty")
    return segments


def build_url(config: LinkConfig, target: LinkTarget, path: str) -> dict[str, Any]:
    segments = _relative(path)
    folder = "/".join([config.folder(target), *segments[:-1]])
    name = segments[-1]
    query = urllib.parse.urlencode({"dir": folder, "scrollto": name}, quote_via=urllib.parse.quote, safe="")
    return {
        "url": f"{config.base_url}/apps/files/?{query}",
        "target": target,
        "folder": folder,
        "name": name,
    }


def _refused(exc: LinkError) -> dict[str, Any]:
    return {"ok": False, "code": exc.code, "message": exc.message}


def _guarded(run: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    """Run a link build, turning a refusal into a result instead of an exception."""
    try:
        return {"ok": True, **run()}
    except LinkError as exc:
        return _refused(exc)


def link_tools(config: LinkConfig) -> list[Tool]:
    def nextcloud_link(params: BaseModel, provenance: Provenance) -> dict[str, Any]:
        assert isinstance(params, LinkParams)
        return _guarded(lambda: build_url(config, params.target, params.path))

    return [
        Tool(
            spec=ToolSpec(
                name="nextcloud_link",
                description=(
                    "Give the user a link to a file in Nextcloud. It opens the Files app on the "
                    "folder of the file with the file selected, and it requires a login. It does "
                    "not create a public share: sharing is something the user does in Nextcloud."
                ),
                params=LinkParams,
                effect=Effect.READ,
                autonomy=Autonomy.AUTO,
                hosts=(),
                credentials=(),
            ),
            handler=nextcloud_link,
        )
    ]
