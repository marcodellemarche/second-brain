"""The current timezone from the profile's "where I am" table. Invented places and dates only."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from second_brain.core.location import current_timezone

PROFILE = """---
type: brain
---

# Profilo

## Dove sono

| Luogo | Dal | Valido fino a | Fuso orario | Nota |
|---|---|---|---|---|
| Lisbona | 2026-01-01 |  |  | casa |
| Firenze | 2026-12-10 | 2026-12-20 | Europe/Rome |  |
| Atene | 2027-03-01 | 2027-03-05 | Not/AZone |  |
"""


@pytest.fixture
def profile(tmp_path: Path) -> Path:
    page = tmp_path / "profilo.md"
    page.write_text(PROFILE, encoding="utf-8")
    return page


def test_home_entry_without_a_zone_keeps_the_default(profile: Path) -> None:
    assert current_timezone(profile, "Europe/Lisbon", date(2026, 11, 2)) == "Europe/Lisbon"


def test_a_trip_entry_gives_its_zone_while_it_lasts(profile: Path) -> None:
    assert current_timezone(profile, "Europe/Lisbon", date(2026, 12, 10)) == "Europe/Rome"
    assert current_timezone(profile, "Europe/Lisbon", date(2026, 12, 20)) == "Europe/Rome"


def test_after_the_trip_the_default_is_back(profile: Path) -> None:
    assert current_timezone(profile, "Europe/Lisbon", date(2026, 12, 21)) == "Europe/Lisbon"


def test_an_unknown_zone_falls_back_to_the_default(profile: Path) -> None:
    assert current_timezone(profile, "Europe/Lisbon", date(2027, 3, 2)) == "Europe/Lisbon"


def test_english_headers_work_too(tmp_path: Path) -> None:
    page = tmp_path / "profile.md"
    page.write_text(
        "| Place | From | Valid until | Timezone | Note |\n|---|---|---|---|---|\n"
        "| Berlin | 2026-05-01 | 2026-05-03 | Europe/Berlin |  |\n",
        encoding="utf-8",
    )
    assert current_timezone(page, "UTC", date(2026, 5, 2)) == "Europe/Berlin"


def test_no_page_or_no_table_gives_the_default(tmp_path: Path) -> None:
    assert current_timezone(None, "Europe/Lisbon") == "Europe/Lisbon"
    assert current_timezone(tmp_path / "missing.md", "Europe/Lisbon") == "Europe/Lisbon"
    page = tmp_path / "plain.md"
    page.write_text("| Campo | Valore |\n|---|---|\n| Nome | Esempio |\n", encoding="utf-8")
    assert current_timezone(page, "Europe/Lisbon", date(2026, 1, 1)) == "Europe/Lisbon"
