"""Fire the tasks Hermes is not allowed to schedule itself.

Hermes' cron is off on purpose: the model never decides when work happens. The digest (morning and
evening), the nightly lint and the brief before a call still need a trigger, so this sidecar sends
a signed webhook at fixed local times instead. The payload names the task and nothing else: no vault
content, no instructions. What the task does is the skill's business, behind brain_guard.

Every entry fires at most once per due moment. The last due moment that actually succeeded is kept
in a small JSON file, so a restart does not lose the day and a failed send is retried on the next
poll. A restart within `catch_up_minutes` still sends a task that was due minutes ago; a brain that
was off all night does not send yesterday's lint at noon.

Local times are built with `zoneinfo`, so a time that does not exist (spring forward) fires once
that day, and an ambiguous one (fall back) fires once, at its first occurrence (`fold=0`).
"""

from __future__ import annotations

import json
import logging
import os
import signal
import tempfile
import time
from collections.abc import Callable, Mapping
from datetime import date, datetime, timedelta
from datetime import time as dt_time
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from second_brain.sidecars.webhook import SendEvent, WebhookError

log = logging.getLogger(__name__)

DEFAULT_POLL_SECONDS = 30.0
DEFAULT_CATCH_UP_MINUTES = 30
MAX_CATCH_UP_MINUTES = 720

NAME_PATTERN = r"^[a-z0-9][a-z0-9_-]{0,62}$"
AT_PATTERN = r"^([01][0-9]|2[0-3]):[0-5][0-9]$"

WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


def _weekday_name(day: date) -> str:
    return WEEKDAYS[day.weekday()]


class ScheduleEntry(BaseModel):
    """One task at one local time, on the weekdays it is allowed to run."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(pattern=NAME_PATTERN, description="Task name, sent as-is to Hermes.")
    route: str = Field(pattern=NAME_PATTERN, description="Webhook route this task is posted to.")
    at: str = Field(pattern=AT_PATTERN, description="Local time of day, HH:MM, 00:00 to 23:59.")
    days: list[str] = Field(
        default_factory=lambda: list(WEEKDAYS),
        description="Weekdays the task runs on: mon, tue, wed, thu, fri, sat, sun.",
    )

    @field_validator("days")
    @classmethod
    def _check_days(cls, value: list[str]) -> list[str]:
        if not value:
            raise ValueError("days must not be empty")
        unknown = sorted(set(value) - set(WEEKDAYS))
        if unknown:
            raise ValueError(f"unknown weekday(s): {', '.join(unknown)}")
        if len(set(value)) != len(value):
            raise ValueError("days must not repeat a weekday")
        return value

    def at_time(self) -> dt_time:
        hour, minute = self.at.split(":")
        return dt_time(int(hour), int(minute))


class ScheduleConfig(BaseModel):
    """The whole schedule. An unknown key is refused: a typo must not silently drop a task."""

    model_config = ConfigDict(extra="forbid")

    timezone: str = Field(description="IANA timezone the local times are read in.")
    entries: list[ScheduleEntry] = Field(default_factory=list)
    catch_up_minutes: int = Field(
        default=DEFAULT_CATCH_UP_MINUTES,
        ge=0,
        le=MAX_CATCH_UP_MINUTES,
        description="How late a task may still fire after its due moment.",
    )

    @field_validator("timezone")
    @classmethod
    def _check_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"unknown timezone: {value}") from exc
        return value

    @model_validator(mode="after")
    def _check_names(self) -> ScheduleConfig:
        names = [entry.name for entry in self.entries]
        duplicates = sorted({name for name in names if names.count(name) > 1})
        if duplicates:
            raise ValueError(f"duplicate entry name(s): {', '.join(duplicates)}")
        return self


class Scheduler:
    """Poll the clock and send the tasks that are due, once each."""

    def __init__(
        self,
        config: ScheduleConfig,
        senders: Mapping[str, SendEvent],
        *,
        state_file: Path | str,
        poll_seconds: float = DEFAULT_POLL_SECONDS,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if poll_seconds <= 0:
            raise ValueError("poll_seconds must be positive")
        for entry in config.entries:
            if entry.route not in senders:
                raise ValueError(f"no sender for route {entry.route!r} of entry {entry.name!r}")
        self._config = config
        self._senders = dict(senders)
        self._state_file = Path(state_file)
        self._poll_seconds = poll_seconds
        self._clock = clock
        self._sleep = sleep
        self._zone = ZoneInfo(config.timezone)
        self._fired: dict[str, datetime] = self._load_state()
        self._stopped = False

    @property
    def config(self) -> ScheduleConfig:
        return self._config

    @property
    def state_file(self) -> Path:
        return self._state_file

    def poll_once(self) -> list[dict[str, Any]]:
        """One pass over the entries. Returns the payloads Hermes accepted during it."""
        now = datetime.fromtimestamp(self._clock(), tz=self._zone)
        sent: list[dict[str, Any]] = []
        for entry in self._config.entries:
            due = self._due_moment(entry, now)
            if due is None:
                continue
            last = self._fired.get(entry.name)
            # Compared as instants, not wall clock: a time skipped by DST carries the offset
            # from before the transition, so wall-clock comparison would be an hour off.
            if last is not None and due.timestamp() <= last.timestamp():
                continue
            if now.timestamp() - due.timestamp() > self._config.catch_up_minutes * 60:
                continue
            payload = {"task": entry.name, "due": due.isoformat(), "weekday": _weekday_name(due.date())}
            if not self._deliver(entry, payload):
                continue
            self._fired[entry.name] = due
            self._save_state()
            sent.append(payload)
        return sent

    def run_forever(self, poll_seconds: float | None = None) -> None:
        """Poll until stopped. A failing poll logs and the next one runs: the sidecar stays up."""
        interval = self._poll_seconds if poll_seconds is None else poll_seconds
        if interval <= 0:
            raise ValueError("poll_seconds must be positive")
        log.info("scheduler polling %s every %.0fs", self._config.timezone, interval)
        while not self._stopped:
            try:
                self.poll_once()
            except Exception:
                log.exception("scheduler poll failed")
            self._sleep(interval)

    def stop(self) -> None:
        self._stopped = True

    def _due_moment(self, entry: ScheduleEntry, now: datetime) -> datetime | None:
        """The most recent moment this entry was due, at or before now, on an allowed weekday."""
        at = entry.at_time()
        for offset in range(len(WEEKDAYS) + 1):
            day = now.date() - timedelta(days=offset)
            if _weekday_name(day) not in entry.days:
                continue
            candidate = datetime.combine(day, at, tzinfo=self._zone)
            if candidate.timestamp() <= now.timestamp():
                return candidate
        return None

    def _deliver(self, entry: ScheduleEntry, payload: Mapping[str, Any]) -> bool:
        sender = self._senders[entry.route]
        try:
            status = sender(payload)
        except (WebhookError, OSError, ValueError) as exc:
            log.warning("task %s not sent: %s", entry.name, exc)
            return False
        if not 200 <= status < 300:
            log.warning("task %s refused with %d", entry.name, status)
            return False
        log.info("task %s sent, due %s", entry.name, payload["due"])
        return True

    def _load_state(self) -> dict[str, datetime]:
        try:
            raw = json.loads(self._state_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        if not isinstance(raw, dict):
            return {}
        fired: dict[str, datetime] = {}
        for name, value in raw.items():
            if not isinstance(name, str) or not isinstance(value, str):
                continue
            try:
                moment = datetime.fromisoformat(value)
            except ValueError:
                continue
            if moment.tzinfo is None:
                continue
            fired[name] = moment
        return fired

    def _save_state(self) -> None:
        current = {entry.name for entry in self._config.entries}
        data = {name: moment.isoformat() for name, moment in self._fired.items() if name in current}
        self._state_file.parent.mkdir(parents=True, exist_ok=True)
        handle = tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=self._state_file.parent,
            prefix=f".{self._state_file.name}.",
            suffix=".tmp",
            delete=False,
        )
        try:
            with handle:
                json.dump(data, handle, sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(handle.name, self._state_file)
        except BaseException:
            try:
                os.unlink(handle.name)
            except OSError:
                pass
            raise


def main() -> None:  # pragma: no cover - process entry point
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    from second_brain.sidecars.config import SidecarsConfig

    config = SidecarsConfig.load()
    section = config.require("scheduler")
    routes = {entry.route for entry in section.schedule.entries}
    scheduler = Scheduler(
        section.schedule,
        {route: config.sender(route) for route in routes},
        state_file=section.state_file,
        poll_seconds=section.poll_seconds,
    )
    log.info("scheduler on %s, %d entr(ies)", section.schedule.timezone, len(section.schedule.entries))

    def _stop(*_args: object) -> None:
        scheduler.stop()

    signal.signal(signal.SIGTERM, _stop)
    scheduler.run_forever()


if __name__ == "__main__":
    main()


__all__ = [
    "DEFAULT_CATCH_UP_MINUTES",
    "DEFAULT_POLL_SECONDS",
    "MAX_CATCH_UP_MINUTES",
    "WEEKDAYS",
    "ScheduleConfig",
    "ScheduleEntry",
    "Scheduler",
]
