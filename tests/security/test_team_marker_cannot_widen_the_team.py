"""A call title is written by whoever sends the invite, so it must not be enough to reach the team.

An outsider can name their invite after a team project. The team marker only counts when every
participant already sits in a team domain, and a personal marker still wins over it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from second_brain.sidecars.call_router import CallRouter, RouterConfig

TEAM = "example.com"


def _router(tmp_path: Path) -> CallRouter:
    config = RouterConfig(team_domains=[TEAM], personal_markers=["doctor"], team_markers=["ds9"])
    return CallRouter(tmp_path, config, lambda payload: 202)


@pytest.mark.parametrize(
    "frontmatter",
    [
        {"event": "DS9 sync", "participants": [f"ann@{TEAM}", "eve@outside.example"]},
        {"event": "DS9 sync", "participants": [f"ann@{TEAM}", "bob@outside.example", "eve@outside.example"]},
        {"event": "DS9 sync", "participants": [f"ann@{TEAM}", "Eve"]},
        {"event": "DS9 sync", "participants": []},
        {"event": "DS9 doctor", "participants": [f"ann@{TEAM}", f"bob@{TEAM}"]},
        {
            "event": "DS9 sync",
            "participants": ["Ann", "Bob"],
            "participant_emails": [f"ann@{TEAM}", "x@evil.example"],
        },
    ],
)
def test_a_title_alone_never_routes_to_the_team(tmp_path: Path, frontmatter: dict[str, object]) -> None:
    assert _router(tmp_path).route(frontmatter) != "team"
