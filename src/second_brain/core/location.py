"""The owner's timezone today, from the "where I am" table the `location` skill keeps in the profile.

An entry may name a timezone: while it is the current one, the guard's current time line and the
calendar tool use it instead of the configured zone. Anything unreadable falls back to the
configured zone, because a wrong guess here moves every reminder by an hour.
"""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

_FROM = ("from", "dal")
_UNTIL = ("valid until", "valido fino a")
_ZONE = ("timezone", "fuso orario")


def current_timezone(profile: Path | None, default: str, today: date | None = None) -> str:
    """The zone of the current entry, or `default` when there is none or it names no zone."""
    if profile is None:
        return default
    try:
        text = profile.read_text(encoding="utf-8")
    except OSError:
        return default
    day = today or datetime.now(ZoneInfo(default)).date()
    best: tuple[date, str] | None = None
    for start, until, zone in _entries(text):
        if start > day or (until is not None and until < day):
            continue
        if best is None or start >= best[0]:
            best = (start, zone)
    if best is None or not best[1]:
        return default
    try:
        ZoneInfo(best[1])
    except (ZoneInfoNotFoundError, ValueError):
        return default
    return best[1]


def _entries(text: str) -> list[tuple[date, date | None, str]]:
    entries: list[tuple[date, date | None, str]] = []
    columns: dict[str, int] | None = None
    for line in text.splitlines():
        if not line.strip().startswith("|"):
            columns = None
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if columns is None:
            columns = _columns(cells)
            continue
        if all(set(cell) <= set("-: ") for cell in cells):
            continue
        entry = _entry(cells, columns)
        if entry is not None:
            entries.append(entry)
    return entries


def _columns(header: list[str]) -> dict[str, int] | None:
    names = [cell.lower() for cell in header]
    found: dict[str, int] = {}
    for key, aliases in (("from", _FROM), ("until", _UNTIL), ("zone", _ZONE)):
        for index, name in enumerate(names):
            if name in aliases:
                found[key] = index
    return found if len(found) == 3 else None


def _entry(cells: list[str], columns: dict[str, int] | None) -> tuple[date, date | None, str] | None:
    if columns is None or max(columns.values()) >= len(cells):
        return None
    try:
        start = date.fromisoformat(cells[columns["from"]])
        until_cell = cells[columns["until"]]
        until = date.fromisoformat(until_cell) if until_cell else None
    except ValueError:
        return None
    return start, until, cells[columns["zone"]]
