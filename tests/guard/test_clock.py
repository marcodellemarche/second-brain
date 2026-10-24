"""The current time the guard adds to every turn."""

from __future__ import annotations

from datetime import UTC, datetime

from second_brain.guard.clock import now_line

MOMENT = datetime(2026, 10, 23, 15, 34, tzinfo=UTC)


def test_the_line_is_in_the_owner_timezone_and_language() -> None:
    expected = "Adesso: venerdì 23 ottobre 2026, 16:34 (Europe/Lisbon)."
    assert now_line("Europe/Lisbon", "it", MOMENT) == expected


def test_the_english_line() -> None:
    assert now_line("Europe/Lisbon", "en", MOMENT) == "Now: Friday, 23 October 2026, 16:34 (Europe/Lisbon)."


def test_the_date_follows_the_zone_not_utc() -> None:
    late = datetime(2026, 10, 23, 23, 30, tzinfo=UTC)
    assert now_line("Europe/Rome", "it", late).startswith("Adesso: sabato 24 ottobre 2026, 01:30")
