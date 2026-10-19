"""Proactive messages to the owner, on a daily budget the owner writes in the vault.

The assistant may speak first: a deadline coming up, an idea it developed, the prep for a meeting.
Useful until it becomes noise, so the owner sets a daily budget in a note of the vault
(`_brain/initiative.md`, localized name from the schema): a total for the day and an optional limit
per category, in a fenced YAML block. The note is read on every call, so an edit applies at once:

    ```yaml
    total: 6
    categories:
      deadline: 3
      ideas: 2
    ```

A category the note does not mention is bounded only by the total. A note that is missing or does
not parse means the configured `default_daily` and no per-category limit, with a warning in the
log: the assistant keeps working, it just has no reason to stop for the day.

A message goes out while today's count is under both the category limit and the total. Past that it
is not sent: it is stored as `deferred` and the next digest includes it. An `urgent` message is for
a real deadline within 48 hours, and it goes out without using the budget up, since a deadline is not
something to ration. That escape hatch is capped at `urgent_daily` messages a day: past the cap an
urgent message is handled like any other one, so a model that marks everything urgent still hits the
budget. The morning and evening digests are not sent through here.

Today is the local date in the configured timezone, recorded on each row, so the count resets at the
owner's midnight and not at the server's.

The tool does not send anything itself: it answers `send` or `defer` and Hermes' own reply carries
the message to the owner's chat. That is also why it is `visible` with autonomy `auto`, since the
only person it can reach is the owner.

`feedback` is what the digest skill calls when the owner says there are too many or too few messages
of one kind. The budget note is written only through `vault_write`, so it does not touch the file: it
returns the new fenced block, and the caller writes the note back with it.

Error codes: `budget_unavailable` when there is no usable budget note to change, `invalid_since` for
a `since` that is not ISO 8601.
"""

from __future__ import annotations

import logging
import re
import sqlite3
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

from second_brain.core.types import Autonomy, Effect, ToolSpec
from second_brain.tools.registry import Tool
from second_brain.tools.vault import Provenance

log = logging.getLogger(__name__)

NOTIFY_HOSTS: tuple[str, ...] = ()
NOTIFY_CREDENTIALS: tuple[str, ...] = ()

MAX_TEXT = 1500
DEFAULT_DAILY = 6
DEFAULT_URGENT_DAILY = 5
STATUS_SENT = "sent"
STATUS_DEFERRED = "deferred"

Decision = Literal["send", "defer"]

_CATEGORY = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
_BLOCK = re.compile(r"^```(?:ya?ml)?[ \t]*\r?\n(.*?)^```", re.DOTALL | re.MULTILINE | re.IGNORECASE)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS notifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created REAL NOT NULL,
    local_date TEXT NOT NULL,
    category TEXT NOT NULL,
    text TEXT NOT NULL,
    urgent INTEGER NOT NULL,
    status TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS notifications_date_status ON notifications (local_date, status);
CREATE INDEX IF NOT EXISTS notifications_status_created ON notifications (status, created);
"""


class NotifyError(Exception):
    """A refused notification call. `code` is stable and machine-readable."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


def _category_name(value: str) -> str:
    name = value.strip().lower()
    if not _CATEGORY.match(name):
        raise ValueError(f"not a category name: {value!r}: letters, digits, '-' and '_', up to 32, no spaces")
    return name


def _vault_relative(value: str) -> str:
    """A vault-relative note path: forward slashes, no way out of the vault, a markdown file."""
    path = value.strip()
    if not path:
        raise ValueError("initiative_path must not be empty")
    if path.startswith(("/", "\\")):
        raise ValueError("initiative_path must be relative to the vault")
    if "\\" in path:
        raise ValueError("initiative_path must use forward slashes")
    if any(ord(char) < 32 for char in path):
        raise ValueError("initiative_path may not contain control characters")
    if any(segment in ("", ".", "..") for segment in path.split("/")):
        raise ValueError("initiative_path may not contain empty, '.' or '..' segments")
    if not path.endswith(".md"):
        raise ValueError("initiative_path must be a markdown file")
    return path


class NotifyConfig(BaseModel):
    """The notify section: where the counters live, where the owner's budget is, which day is today."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    store_path: Path = Field(description="SQLite file holding the counters and the deferred queue.")
    initiative_path: str = Field(
        min_length=1,
        description="Vault-relative path of the note holding the daily budget, for example "
        "'_brain/initiative.md'.",
    )
    default_daily: int = Field(
        default=DEFAULT_DAILY,
        ge=0,
        description="Messages a day when the budget note is missing or does not parse.",
    )
    urgent_daily: int = Field(
        default=DEFAULT_URGENT_DAILY,
        ge=0,
        description="Urgent messages a day that skip the budget. Past that an urgent message uses the "
        "budget like any other, and 0 disables the bypass entirely.",
    )
    timezone: str = Field(min_length=1, description="IANA timezone deciding when the day rolls over.")

    @field_validator("initiative_path")
    @classmethod
    def _check_initiative_path(cls, value: str) -> str:
        return _vault_relative(value)

    @field_validator("timezone")
    @classmethod
    def _check_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value.strip())
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"unknown timezone: {value!r}") from exc
        return value.strip()

    def zone(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    def store(self) -> NotifyStore:
        return NotifyStore(self.store_path, self.zone())


class Budget(BaseModel):
    """The daily budget as the owner wrote it: a total and the categories with their own limit."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    total: int = Field(ge=0, description="Messages a day, all categories together.")
    categories: dict[str, int] = Field(
        default_factory=dict, description="Per-category daily limits, on top of the total."
    )

    @field_validator("categories")
    @classmethod
    def _check_categories(cls, value: dict[str, int]) -> dict[str, int]:
        limits: dict[str, int] = {}
        for name, limit in value.items():
            try:
                category = _category_name(name)
            except ValueError as exc:
                raise ValueError(f"budget categories: {exc}") from exc
            if category in limits:
                raise ValueError(f"budget categories: {category!r} is listed twice")
            if not isinstance(limit, int) or isinstance(limit, bool) or limit < 0:
                raise ValueError(f"budget categories: {category} must be a whole number of 0 or more")
            limits[category] = limit
        return limits

    @classmethod
    def fallback(cls, total: int) -> Budget:
        """The budget when the note is missing or broken: the default total, no category limit."""
        return cls(total=total, categories={})


@dataclass(frozen=True, slots=True)
class Counts:
    """What already went out today: the budget messages per category, and the urgent ones.

    `total` and `categories` leave the urgent messages out, since those skipped the budget.
    """

    total: int
    categories: Mapping[str, int]
    urgent: int = 0


def decide(
    urgent: bool,
    category: str,
    counts: Counts,
    budget: Budget,
    urgent_daily: int = DEFAULT_URGENT_DAILY,
) -> tuple[Decision, int]:
    """Send or defer, plus how many messages are left today after this one."""
    limit = budget.categories.get(category)
    left_for_total = max(0, budget.total - counts.total)
    if limit is None:
        remaining = left_for_total
    else:
        remaining = min(left_for_total, max(0, limit - counts.categories.get(category, 0)))
    if urgent and counts.urgent < urgent_daily:
        return "send", remaining
    if remaining <= 0:
        return "defer", 0
    return "send", remaining - 1


@dataclass(frozen=True, slots=True)
class Notification:
    id: int
    created: float
    local_date: str
    category: str
    text: str
    urgent: bool
    status: str

    @property
    def decision(self) -> Decision:
        return "send" if self.status == STATUS_SENT else "defer"


class NotifyStore:
    """One SQLite file: every call recorded, the deferred ones waiting for a digest."""

    def __init__(self, path: Path | str, zone: ZoneInfo) -> None:
        self._path = Path(path)
        self._zone = zone
        self._lock = threading.Lock()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(self._path, check_same_thread=False, isolation_level=None)
        self._connection.row_factory = sqlite3.Row
        with self._lock:
            self._connection.execute("PRAGMA journal_mode=WAL")
            self._connection.execute("PRAGMA busy_timeout=5000")
            self._connection.executescript(_SCHEMA)

    @property
    def path(self) -> Path:
        return self._path

    def local_date(self, now: float) -> str:
        """The date the owner is living in at `now`, which is what the budget counts against."""
        return datetime.fromtimestamp(now, self._zone).date().isoformat()

    def counts(self, local_date: str) -> Counts:
        """The messages already sent that day, per category. Deferred ones were never sent."""
        with self._lock:
            return self._counts(local_date)

    def submit(
        self,
        category: str,
        text: str,
        urgent: bool,
        budget: Budget,
        now: float,
        urgent_daily: int = DEFAULT_URGENT_DAILY,
    ) -> tuple[Notification, int, bool]:
        """Decide and record in one lock, so two calls cannot both see the last free slot.

        An urgent message past the cap that goes out through the budget is stored with `urgent = 0`,
        so the counters charge it to the budget instead of the bypass.
        """
        local_date = self.local_date(now)
        with self._lock:
            counts = self._counts(local_date)
            decision, remaining = decide(urgent, category, counts, budget, urgent_daily)
            capped = urgent and counts.urgent >= urgent_daily
            stored_urgent = urgent and not (capped and decision == "send")
            status = STATUS_SENT if decision == "send" else STATUS_DEFERRED
            cursor = self._connection.execute(
                "INSERT INTO notifications (created, local_date, category, text, urgent, status)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (now, local_date, category, text, int(stored_urgent), status),
            )
            row_id = cursor.lastrowid
        assert row_id is not None
        notification = Notification(
            id=int(row_id),
            created=now,
            local_date=local_date,
            category=category,
            text=text,
            urgent=stored_urgent,
            status=status,
        )
        return notification, remaining, capped

    def deferred(self, since: float) -> list[Notification]:
        """The messages deferred from `since` on, oldest first, for the digest to include."""
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM notifications WHERE status = ? AND created >= ? ORDER BY created, id",
                (STATUS_DEFERRED, since),
            ).fetchall()
        return [_notification(row) for row in rows]

    def list(self, limit: int = 50) -> list[Notification]:
        """The most recent calls first, whatever the decision."""
        if limit < 1:
            raise ValueError("limit must be at least 1")
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM notifications ORDER BY created DESC, id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [_notification(row) for row in rows]

    def _counts(self, local_date: str) -> Counts:
        """Read under the caller's lock, so the decision and the insert see the same counts."""
        rows = self._connection.execute(
            "SELECT category, COUNT(*) AS sent FROM notifications"
            " WHERE local_date = ? AND status = ? AND urgent = 0 GROUP BY category",
            (local_date, STATUS_SENT),
        ).fetchall()
        categories = {str(row["category"]): int(row["sent"]) for row in rows}
        urgent = self._connection.execute(
            "SELECT COUNT(*) AS sent FROM notifications WHERE local_date = ? AND status = ? AND urgent = 1",
            (local_date, STATUS_SENT),
        ).fetchone()
        assert urgent is not None
        return Counts(total=sum(categories.values()), categories=categories, urgent=int(urgent["sent"]))


def _notification(row: sqlite3.Row) -> Notification:
    return Notification(
        id=int(row["id"]),
        created=float(row["created"]),
        local_date=str(row["local_date"]),
        category=str(row["category"]),
        text=str(row["text"]),
        urgent=bool(row["urgent"]),
        status=str(row["status"]),
    )


class InitiativeFile:
    """The owner's budget note. Read on every call, never written from here."""

    def __init__(self, path: Path, default_daily: int) -> None:
        self._path = path
        self._default = default_daily

    def read(self) -> Budget | None:
        """The budget in the note, or None (with a warning) when it is missing or does not parse."""
        try:
            content = self._path.read_text(encoding="utf-8")
        except OSError as exc:
            self._warn(f"cannot be read ({exc})")
            return None
        block = _BLOCK.search(content)
        if block is None:
            self._warn("has no fenced YAML block")
            return None
        try:
            return Budget.model_validate(yaml.safe_load(block.group(1)))
        except (yaml.YAMLError, ValueError) as exc:
            self._warn(f"is not a valid budget ({' '.join(str(exc).split())})")
            return None

    def budget(self) -> Budget:
        found = self.read()
        return found if found is not None else Budget.fallback(self._default)

    def require(self) -> Budget:
        found = self.read()
        if found is None:
            raise NotifyError(
                "budget_unavailable",
                f"{self._path.name} is missing or does not hold a usable budget block, "
                "so there is no limit to change",
            )
        return found

    def _warn(self, reason: str) -> None:
        log.warning(
            "initiative budget %s %s: using %d a day with no per-category limit",
            self._path,
            reason,
            self._default,
        )


def render_block(total: int, categories: Mapping[str, int]) -> str:
    """The fenced YAML block to put back into the budget note, categories in a stable order."""
    payload: dict[str, Any] = {"total": total, "categories": dict(sorted(categories.items()))}
    body = yaml.safe_dump(payload, sort_keys=False, default_flow_style=False).rstrip()
    return f"```yaml\n{body}\n```"


class Notifier:
    """The budget decision for one brain, and the note that holds it."""

    def __init__(
        self,
        config: NotifyConfig,
        vault_root: Path,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._config = config
        self._zone = config.zone()
        self._clock = clock
        self._initiative = InitiativeFile(
            self._resolve(vault_root, config.initiative_path), config.default_daily
        )
        self._store = config.store()

    @property
    def store(self) -> NotifyStore:
        return self._store

    def notify(self, category: str, text: str, urgent: bool) -> dict[str, Any]:
        notification, remaining, urgent_capped = self._store.submit(
            category,
            text,
            urgent,
            self._initiative.budget(),
            self._clock(),
            self._config.urgent_daily,
        )
        return {
            "decision": notification.decision,
            "remaining_today": remaining,
            "urgent_capped": urgent_capped,
        }

    def deferred_notifications(self, since: str) -> dict[str, Any]:
        moment = self._parse_since(since)
        return {
            "since": since,
            "notifications": [
                {
                    "category": notification.category,
                    "text": notification.text,
                    "urgent": notification.urgent,
                    "created": datetime.fromtimestamp(notification.created, self._zone).isoformat(),
                }
                for notification in self._store.deferred(moment)
            ],
        }

    def feedback(self, category: str, delta: int) -> dict[str, Any]:
        """Propose a new category limit, as the block the caller writes back with vault_write."""
        budget = self._initiative.require()
        previous = budget.categories.get(category)
        base = budget.total if previous is None else previous
        limit = min(budget.total, max(0, base + delta))
        categories = {**budget.categories, category: limit}
        return {
            "path": self._config.initiative_path,
            "category": category,
            "previous": previous,
            "limit": limit,
            "total": budget.total,
            "block": render_block(budget.total, categories),
        }

    def _parse_since(self, since: str) -> float:
        """An ISO 8601 moment. A naive one is read as local time, since that is what the digest has."""
        try:
            moment = datetime.fromisoformat(since.strip())
        except ValueError:
            raise NotifyError(
                "invalid_since", "since must be ISO 8601, for example 2024-05-01T09:00:00+02:00"
            ) from None
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=self._zone)
        return moment.timestamp()

    @staticmethod
    def _resolve(vault_root: Path, relative: str) -> Path:
        root = Path(vault_root).resolve()
        target = (root / relative).resolve()
        if root not in target.parents:
            raise ValueError(f"initiative_path leaves the vault: {relative!r}")
        return target


class NotifyParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    category: str = Field(
        min_length=1,
        max_length=32,
        description="Short name of the kind of message, for example 'deadline' or 'ideas'. It is "
        "the name the budget note uses for the per-category limit.",
    )
    text: str = Field(
        min_length=1,
        max_length=MAX_TEXT,
        description="The message itself, in the owner's language, short enough to read on a phone.",
    )
    urgent: bool = Field(
        default=False,
        description="True only for a real deadline within 48 hours: a few urgent messages a day "
        "skip the daily budget, and past that cap they use it like any other message.",
    )

    @field_validator("category")
    @classmethod
    def _check_category(cls, value: str) -> str:
        return _category_name(value)

    @field_validator("text")
    @classmethod
    def _check_text(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("text must not be empty")
        if len(text) > MAX_TEXT:
            raise ValueError(f"text must be at most {MAX_TEXT} characters")
        if any(ord(char) < 32 and char not in "\n\t" for char in text):
            raise ValueError("text may not contain control characters")
        return text


class DeferredParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    since: str = Field(
        min_length=1,
        description="The previous digest, ISO 8601. Only messages deferred from that moment on are "
        "returned, so pass the time of the last digest.",
    )


class FeedbackParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    category: str = Field(min_length=1, max_length=32, description="Category to change the limit of.")
    delta: int = Field(
        ge=-10,
        le=10,
        description="How much to change the limit by: -1 for fewer messages of this kind, +1 for more.",
    )

    @field_validator("category")
    @classmethod
    def _check_category(cls, value: str) -> str:
        return _category_name(value)

    @field_validator("delta")
    @classmethod
    def _check_delta(cls, value: int) -> int:
        if value == 0:
            raise ValueError("delta must not be zero")
        return value


def _refused(exc: NotifyError) -> dict[str, Any]:
    return {"ok": False, "code": exc.code, "message": exc.message}


def _guarded(run: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    try:
        return {"ok": True, **run()}
    except NotifyError as exc:
        return _refused(exc)


def notify_tools(
    config: NotifyConfig,
    vault_root: Path,
    *,
    clock: Callable[[], float] = time.time,
) -> list[Tool]:
    """The three notify tools: speak first, read what waited, propose a new limit."""
    notifier = Notifier(config, vault_root, clock=clock)

    def notify(params: BaseModel, provenance: Provenance) -> dict[str, Any]:
        assert isinstance(params, NotifyParams)
        return _guarded(lambda: notifier.notify(params.category, params.text, params.urgent))

    def deferred_notifications(params: BaseModel, provenance: Provenance) -> dict[str, Any]:
        assert isinstance(params, DeferredParams)
        return _guarded(lambda: notifier.deferred_notifications(params.since))

    def notify_feedback(params: BaseModel, provenance: Provenance) -> dict[str, Any]:
        assert isinstance(params, FeedbackParams)
        return _guarded(lambda: notifier.feedback(params.category, params.delta))

    return [
        Tool(
            spec=ToolSpec(
                name="notify",
                description=(
                    "Message the owner on your own initiative: a deadline coming up, an idea you "
                    "developed, the prep for a meeting. It sends nothing itself: it answers 'send' "
                    "when the message fits today's budget, and 'defer' when it does not, in which "
                    "case the message waits for the next digest and must not be written out now. "
                    "Set urgent only for a real deadline within 48 hours: a limited number of urgent "
                    "messages a day skip the budget, and past that cap they use it like any other. "
                    "The morning and evening digests are not sent through this tool."
                ),
                params=NotifyParams,
                effect=Effect.VISIBLE,
                autonomy=Autonomy.AUTO,
                hosts=NOTIFY_HOSTS,
                credentials=NOTIFY_CREDENTIALS,
            ),
            handler=notify,
        ),
        Tool(
            spec=ToolSpec(
                name="deferred_notifications",
                description=(
                    "The messages that were deferred since a moment, for a digest to include. "
                    "Pass the time of the previous digest as ISO 8601: each deferred message then "
                    "appears in exactly one digest."
                ),
                params=DeferredParams,
                effect=Effect.READ,
                autonomy=Autonomy.AUTO,
                hosts=NOTIFY_HOSTS,
                credentials=NOTIFY_CREDENTIALS,
            ),
            handler=deferred_notifications,
        ),
        Tool(
            spec=ToolSpec(
                name="notify_feedback",
                description=(
                    "Propose a new daily limit for one category of the initiative budget, after "
                    "the owner says there are too many or too few messages of that kind. It writes "
                    "nothing: it returns the new fenced YAML block, and the caller writes the whole "
                    "budget note back through vault_write."
                ),
                params=FeedbackParams,
                effect=Effect.READ,
                autonomy=Autonomy.AUTO,
                hosts=NOTIFY_HOSTS,
                credentials=NOTIFY_CREDENTIALS,
            ),
            handler=notify_feedback,
        ),
    ]
