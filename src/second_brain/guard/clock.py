"""The current date and time, added to every turn.

Hermes writes the date only once, when a session starts, and a chat session can stay open for
days: without this line the model reads "tonight" against a date that is already gone.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

_WEEKDAYS_IT = ("lunedì", "martedì", "mercoledì", "giovedì", "venerdì", "sabato", "domenica")
_MONTHS_IT = (
    "gennaio",
    "febbraio",
    "marzo",
    "aprile",
    "maggio",
    "giugno",
    "luglio",
    "agosto",
    "settembre",
    "ottobre",
    "novembre",
    "dicembre",
)


def now_line(timezone: str, language: str, now: datetime | None = None) -> str:
    """One line with the owner's local date and time, in the owner's language."""
    zone = ZoneInfo(timezone)
    local = (now or datetime.now(zone)).astimezone(zone)
    if language == "it":
        day = f"{_WEEKDAYS_IT[local.weekday()]} {local.day} {_MONTHS_IT[local.month - 1]} {local.year}"
        return f"Adesso: {day}, {local:%H:%M} ({timezone})."
    return f"Now: {local:%A}, {local.day} {local:%B} {local.year}, {local:%H:%M} ({timezone})."
