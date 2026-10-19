"""Effect classes, autonomy levels, vault zones and the tool spec."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class Effect(StrEnum):
    READ = "read"
    INTERNAL = "internal"
    VISIBLE = "visible"
    EXTERNAL = "external"


class Autonomy(StrEnum):
    AUTO = "auto"
    NOTIFY = "notify"
    ASK = "ask"
    NEVER = "never"


_RANK: dict[Autonomy, int] = {
    Autonomy.AUTO: 0,
    Autonomy.NOTIFY: 1,
    Autonomy.ASK: 2,
    Autonomy.NEVER: 3,
}


def stricter(a: Autonomy, b: Autonomy) -> Autonomy:
    """Return the more restrictive of two autonomy levels."""
    return a if _RANK[a] >= _RANK[b] else b


class Zone(StrEnum):
    INBOX = "inbox"
    RAW = "raw"
    CALLS = "calls"
    TOPICS = "topics"
    KNOWLEDGE = "knowledge"
    IDEAS = "ideas"
    PROJECTS = "projects"
    PEOPLE = "people"
    WORK = "work"
    DOCUMENTS = "documents"
    ANSWERS = "answers"
    DRAWINGS = "drawings"
    ARCHIVE = "archive"
    BRAIN = "_brain"
    META = "_meta"


FRONTMATTER_KEYS: frozenset[str] = frozenset(
    {
        "type",
        "created",
        "updated",
        "tags",
        "domain",
        "share",
        "sources",
        "confidence",
        "contested",
        "trust",
        "status",
        "date",
        "start",
        "participants",
        "organizer",
        "event",
        "audio",
        "transcript",
        "category",
        "parties",
        "issued",
        "expires",
        "original",
        "original_sha256",
        "project",
        "jira",
        "due",
        "index",
    }
)


class ToolSpec(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    description: str
    params: type[BaseModel]
    effect: Effect
    autonomy: Autonomy
    hosts: tuple[str, ...] = ()
    credentials: tuple[str, ...] = ()
    # The result carries content written by someone other than the user: mail, web, a calendar
    # description, another brain's answer.
    taints_untrusted: bool = False
    # The result carries private data: vault notes, documents, mail.
    taints_private: bool = False
