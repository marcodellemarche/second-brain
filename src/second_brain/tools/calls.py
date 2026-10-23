"""Call tools: copy a transcript into raw verbatim, and hand a call over to the team brain.

Both move other people's words around, so the words are moved by code and never retyped by the
model: a transcript of several hundred lines does not survive being written back token by token,
and an empty transcript in raw cannot be fixed afterwards because raw is immutable.

The hand-off has no federation behind it. The note is written into a folder outside the vault and
removed from the vault; the owner moves the folder to the team brain, whose staging importer takes
it in as an untrusted capture. The transcript stays in raw: it is the owner's record of the call,
and raw is never rewritten.
"""

from __future__ import annotations

import os
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field

from second_brain.core.schema import SchemaError, VaultSchema, load_schema
from second_brain.core.types import Autonomy, Effect, ToolSpec, Zone
from second_brain.tools.registry import Tool
from second_brain.tools.secrets import scan
from second_brain.tools.vault import Provenance, Vault, VaultError, is_empty_note

HANDOFF_PATH = "/handoff"
FRONTMATTER = "---"


class HandoffConfig(BaseModel):
    """The folder a call handed to the team brain is written into, and how the owner moves it."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    dir: Path = Field(default=Path(HANDOFF_PATH))
    move_hint: str = Field(default="", description="What the owner runs to move the folder over.")


class TranscriptParams(BaseModel):
    source: str = Field(min_length=1, description="The call note in the capture folder, vault-relative.")
    path: str = Field(min_length=1, description="Where the transcript goes in the raw zone, vault-relative.")
    from_heading: str | None = Field(
        default=None,
        description=(
            "The heading the transcript starts under in the source, for example 'Full transcript'. "
            "Everything after that heading is copied. Absent, the whole body is copied."
        ),
    )
    date: str = Field(min_length=1, description="Date of the call, YYYY-MM-DD.")
    start: str = Field(default="", description="Start time of the call, HH:MM.")
    participants: list[str] = Field(default_factory=list)
    organizer: str = ""
    created: str = Field(min_length=1, description="Today, YYYY-MM-DD.")
    revision: bool = Field(
        default=False,
        description=(
            "The source is a new version of a call already in the vault. A transcript already in "
            "raw is kept, and a different one goes next to it under a new name."
        ),
    )


class HandoffParams(BaseModel):
    path: str = Field(min_length=1, description="The call note to hand over, vault-relative.")
    reason: str = Field(min_length=1, description="Why it belongs to the team, in one line.")


def split_note(content: str) -> tuple[dict[str, Any], list[str]]:
    """The frontmatter of a note and the lines of its body. No frontmatter is an empty mapping."""
    lines = content.splitlines()
    if not lines or lines[0].strip() != FRONTMATTER:
        return {}, lines
    closing = next((i for i, line in enumerate(lines[1:], 1) if line.strip() == FRONTMATTER), None)
    if closing is None:
        return {}, lines
    try:
        parsed = yaml.safe_load("\n".join(lines[1:closing]))
    except yaml.YAMLError:
        parsed = None
    return (dict(parsed) if isinstance(parsed, dict) else {}), lines[closing + 1 :]


def transcript_body(body: list[str], from_heading: str | None) -> tuple[str, list[str]]:
    """The title of the source and the lines to copy into raw."""
    title = next((line for line in body if line.startswith("# ")), "")
    if from_heading is None:
        rest = [line for line in body if line != title]
        return title, rest
    wanted = from_heading.strip().lstrip("#").strip().casefold()
    for index, line in enumerate(body):
        if line.lstrip().startswith("#") and line.strip().lstrip("#").strip().casefold() == wanted:
            return title, body[index + 1 :]
    raise VaultError("not_found", f"the source has no heading {from_heading!r}")


def render_transcript(params: TranscriptParams, title: str, lines: list[str]) -> str:
    frontmatter = {
        "type": "transcript",
        "created": params.created,
        "date": params.date,
        "start": params.start,
        "participants": params.participants,
        "organizer": params.organizer,
        "trust": "untrusted",
        "sources": [params.source],
    }
    header = yaml.safe_dump(frontmatter, sort_keys=False, allow_unicode=True).rstrip("\n")
    while lines and not lines[0].strip():
        lines = lines[1:]
    parts = [FRONTMATTER, header, FRONTMATTER, ""]
    if title:
        parts += [title, ""]
    return "\n".join(parts + lines).rstrip("\n") + "\n"


def _in_zone(schema: VaultSchema, path: str, zones: tuple[Zone, ...]) -> bool:
    return schema.zone_of(path) in zones


def _refused(exc: VaultError) -> dict[str, Any]:
    return {"ok": False, "code": exc.code, "message": exc.message}


def _write_outside(directory: Path, name: str, content: str) -> Path:
    """Write a new file into the hand-off folder. A name already there gets a number."""
    directory.mkdir(parents=True, exist_ok=True)
    stem, suffix = os.path.splitext(name)
    target = directory / name
    counter = 2
    while target.exists():
        target = directory / f"{stem}-{counter}{suffix}"
        counter += 1
    handle_fd, tmp = tempfile.mkstemp(dir=directory, prefix=".tmp-")
    try:
        with os.fdopen(handle_fd, "w", encoding="utf-8") as handle:
            handle.write(content)
        os.chmod(tmp, 0o644)
        os.replace(tmp, target)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return target


class CallTools:
    def __init__(
        self,
        vault: Vault,
        handoff: HandoffConfig | None = None,
        schema: Callable[[], VaultSchema] | None = None,
    ) -> None:
        self._vault = vault
        self._handoff = handoff
        self._schema = schema or (lambda: load_schema(vault.root))

    def transcript(self, params: TranscriptParams, provenance: Provenance) -> dict[str, Any]:
        schema = self._load_schema()
        if not _in_zone(schema, params.source, (Zone.INBOX,)):
            raise VaultError("zone_denied", f"{params.source} is not in the capture folder")
        if not _in_zone(schema, params.path, (Zone.RAW,)):
            raise VaultError("zone_denied", f"{params.path} is not in the raw zone")
        source, _ = self._vault.read(params.source)
        _, body = split_note(source)
        title, lines = transcript_body(body, params.from_heading)
        if not any(line.strip() for line in lines):
            raise VaultError("not_found", f"{params.source} has no transcript to copy")
        content = render_transcript(params, title, lines)
        try:
            existing, sha256 = self._vault.read(params.path)
        except VaultError as exc:
            if exc.code != "not_found":
                raise
            commit = self._vault.write(params.path, content, provenance)
            return {"ok": True, "commit": commit, "lines": len(lines)}
        if is_empty_note(existing):
            commit = self._vault.fill(params.path, content, provenance, sha256)
            return {"ok": True, "commit": commit, "lines": len(lines), "filled": True}
        if not params.revision:
            raise VaultError("exists", f"{params.path} already holds the transcript: skip this step")
        return self._revise(params.path, lines, content, provenance)

    def _revise(self, path: str, lines: list[str], content: str, provenance: Provenance) -> dict[str, Any]:
        """Raw is never rewritten, so a changed transcript gets the first free `(rev N)` name."""
        stem, suffix = os.path.splitext(path)
        candidate, number = path, 1
        while True:
            try:
                existing, _ = self._vault.read(candidate)
            except VaultError as exc:
                if exc.code != "not_found":
                    raise
                commit = self._vault.write(candidate, content, provenance)
                return {"ok": True, "commit": commit, "lines": len(lines), "path": candidate}
            if _same_words(existing, content):
                return {"ok": True, "unchanged": True, "path": candidate}
            number += 1
            candidate = f"{stem} (rev {number}){suffix}"

    def handoff(self, params: HandoffParams, provenance: Provenance) -> dict[str, Any]:
        assert self._handoff is not None
        schema = self._load_schema()
        if not _in_zone(schema, params.path, (Zone.CALLS, Zone.INBOX)):
            raise VaultError("zone_denied", f"{params.path} is not a call note or a capture")
        content, _ = self._vault.read(params.path)
        findings = scan(content)
        if findings:
            found = ", ".join(f"{finding.rule} at line {finding.line}" for finding in findings)
            raise VaultError("secret_detected", f"not handing over {params.path}: {found}")
        frontmatter, _ = split_note(content)
        target = _write_outside(self._handoff.dir, Path(params.path).name, content)
        try:
            commits = [self._vault.delete(params.path, provenance)]
        except VaultError:
            target.unlink(missing_ok=True)
            raise
        for source in _list(frontmatter.get("sources")):
            if source != params.path and _in_zone(schema, source, (Zone.INBOX,)):
                try:
                    commits.append(self._vault.delete(source, provenance))
                except VaultError as exc:
                    if exc.code != "not_found":
                        raise
        result: dict[str, Any] = {"ok": True, "handoff": target.name, "commits": commits}
        transcript = frontmatter.get("transcript")
        if isinstance(transcript, str) and transcript.strip():
            result["transcript_kept"] = transcript.strip()
        if self._handoff.move_hint:
            result["move_hint"] = self._handoff.move_hint
        return result

    def _load_schema(self) -> VaultSchema:
        try:
            return self._schema()
        except SchemaError as exc:
            raise VaultError("not_found", f"the vault schema cannot be read: {exc}") from exc


def _same_words(existing: str, content: str) -> bool:
    """Same body, whatever the frontmatter says: `created` changes on every run."""

    def body(note: str) -> list[str]:
        return [line.rstrip() for line in split_note(note)[1]]

    return body(existing) == body(content)


def _list(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, list | tuple):
        return [str(item).strip() for item in value if str(item).strip()]
    return []


def call_tools(vault: Vault, handoff: HandoffConfig | None = None) -> list[Tool]:
    calls = CallTools(vault, handoff)

    def guarded(run: Callable[[], dict[str, Any]]) -> dict[str, Any]:
        try:
            return run()
        except VaultError as exc:
            return _refused(exc)

    def transcript(params: BaseModel, provenance: Provenance) -> dict[str, Any]:
        assert isinstance(params, TranscriptParams)
        return guarded(lambda: calls.transcript(params, provenance))

    def handoff_call(params: BaseModel, provenance: Provenance) -> dict[str, Any]:
        assert isinstance(params, HandoffParams)
        return guarded(lambda: calls.handoff(params, provenance))

    tools = [
        Tool(
            spec=ToolSpec(
                name="call_transcript",
                description=(
                    "Copy the transcript of a call note in the capture folder into the raw zone, "
                    "verbatim, with the transcript frontmatter. The words are copied by the server, "
                    "so never pass them yourself. A transcript already in raw is left alone "
                    "(code `exists`), an empty one is completed. With `revision`, a different "
                    "transcript is written next to the old one and the result says its `path`."
                ),
                params=TranscriptParams,
                effect=Effect.INTERNAL,
                autonomy=Autonomy.NOTIFY,
            ),
            handler=transcript,
        ),
    ]
    if handoff is not None:
        tools.append(
            Tool(
                spec=ToolSpec(
                    name="team_handoff",
                    description=(
                        "Hand a call note over to the team brain: it is written to the hand-off "
                        "folder and removed from this vault, together with its source in the capture "
                        "folder. The transcript stays in raw. The owner moves the folder to the team "
                        "brain: tell them with the `move_hint` of the result. Undo with vault_undo on "
                        "the commits, then remove the file from the hand-off folder."
                    ),
                    params=HandoffParams,
                    effect=Effect.INTERNAL,
                    autonomy=Autonomy.ASK,
                ),
                handler=handoff_call,
            )
        )
    return tools


__all__ = [
    "HANDOFF_PATH",
    "CallTools",
    "HandoffConfig",
    "HandoffParams",
    "TranscriptParams",
    "call_tools",
    "is_empty_note",
    "render_transcript",
    "split_note",
    "transcript_body",
]
