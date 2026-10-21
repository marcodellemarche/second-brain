"""Tests for the call router: the rules, the polling and the one event it sends."""

from __future__ import annotations

import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from second_brain.sidecars.call_router import (
    DEFAULT_TEAM_MIN_PARTICIPANTS,
    CallRouter,
    RouterConfig,
    read_frontmatter,
)

TEAM = "example.com"
OTHER = "other.example"


def _config(**overrides: Any) -> RouterConfig:
    values: dict[str, Any] = {
        "team_domains": [TEAM],
        "personal_markers": ["family", "doctor"],
    }
    values.update(overrides)
    return RouterConfig(**values)


class FakeSend:
    def __init__(self, status: int = 202) -> None:
        self.status = status
        self.calls: list[dict[str, Any]] = []

    def __call__(self, payload: Mapping[str, Any]) -> int:
        self.calls.append(dict(payload))
        return self.status


class ManualClock:
    def __init__(self, now: float = 1_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def watch_dir(tmp_path: Path) -> Path:
    folder = tmp_path / "calls"
    folder.mkdir()
    return folder


@pytest.fixture
def send() -> FakeSend:
    return FakeSend()


@pytest.fixture
def router(watch_dir: Path, send: FakeSend) -> CallRouter:
    return CallRouter(watch_dir, _config(), send, clock=ManualClock())


def _router(tmp_path: Path, **overrides: Any) -> CallRouter:
    folder = tmp_path / "rules"
    folder.mkdir(exist_ok=True)
    return CallRouter(folder, _config(**overrides), FakeSend(), clock=ManualClock())


def _call(**frontmatter: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "type": "call",
        "participants": [f"ann@{TEAM}", f"bob@{TEAM}", f"cid@{TEAM}"],
        "organizer": f"ann@{TEAM}",
        "domain": "work",
        "event": "Weekly sync",
    }
    values.update(frontmatter)
    return values


# --- the rules ------------------------------------------------------------------------------


TEAM3 = [f"ann@{TEAM}", f"bob@{TEAM}", f"cid@{TEAM}"]


@pytest.mark.parametrize(
    ("case", "frontmatter", "expected"),
    [
        ("a 1:1 with a colleague stays personal", _call(participants=TEAM3[:2]), "personal"),
        ("a call with one participant stays personal", _call(participants=TEAM3[:1]), "personal"),
        ("a call with nobody on it asks", _call(participants=[]), "ask"),
        ("a team meeting goes to the team", _call(), "team"),
        ("a team meeting above the minimum goes to the team", _call(participants=TEAM3 + TEAM3), "team"),
        (
            "an external participant asks",
            _call(participants=[*TEAM3[:2], f"eve@{OTHER}"]),
            "ask",
        ),
        ("an external organizer asks", _call(organizer=f"eve@{OTHER}"), "ask"),
        ("a personal marker in the event stays personal", _call(event="Call with the doctor"), "personal"),
        ("a personal marker in the domain stays personal", _call(domain="family"), "personal"),
        (
            "a personal marker wins over a team meeting",
            _call(participants=TEAM3 + TEAM3, event="Family lunch"),
            "personal",
        ),
        ("a missing organizer asks", _call(organizer=None), "ask"),
        ("a missing participant list asks", _call(participants=None), "ask"),
        ("a named participant asks", _call(participants=[*TEAM3[:2], "Cid"]), "ask"),
        ("a named organizer asks", _call(organizer="Ann"), "ask"),
        (
            "a marker in a participant does not count",
            _call(participants=[f"doctor@{TEAM}", *TEAM3[1:]]),
            "team",
        ),
        (
            "the domain is matched case-insensitively",
            _call(participants=[f"ann@{TEAM.upper()}", *TEAM3[1:]], organizer=f"ann@{TEAM.upper()}"),
            "team",
        ),
        (
            "a display-name email still has a domain",
            _call(participants=[f"<ann@{TEAM}>", *TEAM3[1:]]),
            "team",
        ),
        ("a comma-separated list is accepted", _call(participants=", ".join(TEAM3)), "team"),
        ("an unknown frontmatter key changes nothing", _call(unexpected="x"), "team"),
        ("an empty frontmatter asks", {}, "ask"),
    ],
)
def test_the_routing_rules(case: str, frontmatter: dict[str, Any], expected: str, tmp_path: Path) -> None:
    assert _router(tmp_path).route(frontmatter) == expected, case


def test_the_reason_explains_an_ask(tmp_path: Path) -> None:
    router = _router(tmp_path)
    decision = router.decide(_call(participants=[f"ann@{TEAM}", f"bob@{TEAM}", f"eve@{OTHER}"]))
    assert decision.route == "ask"
    assert OTHER in decision.reason


def test_a_team_meeting_needs_three_people_by_default() -> None:
    assert DEFAULT_TEAM_MIN_PARTICIPANTS == 3


def test_the_team_minimum_is_configurable(tmp_path: Path) -> None:
    router = _router(tmp_path, team_min_participants=5)
    assert router.route(_call()) == "ask"
    assert router.route(_call(participants=[f"p{i}@{TEAM}" for i in range(5)])) == "team"


def test_no_team_domains_means_nothing_is_a_team_call(tmp_path: Path) -> None:
    router = _router(tmp_path, team_domains=[])
    assert router.route(_call()) == "ask"


# --- the config -----------------------------------------------------------------------------


def test_an_unknown_config_key_is_refused() -> None:
    with pytest.raises(ValueError):
        _config(team_domain=TEAM)  # type: ignore[call-arg]


def test_a_domain_with_an_at_sign_is_refused() -> None:
    with pytest.raises(ValueError):
        _config(team_domains=[f"ann@{TEAM}"])


def test_an_empty_marker_is_refused() -> None:
    with pytest.raises(ValueError):
        _config(personal_markers=["  "])


def test_a_team_of_one_person_is_refused() -> None:
    with pytest.raises(ValueError):
        _config(team_min_participants=1)


def test_the_config_is_frozen() -> None:
    config = _config()
    with pytest.raises(ValueError):
        config.team_domains = [OTHER]


def test_domains_and_markers_are_normalized() -> None:
    config = RouterConfig(team_domains=["@Example.COM"], personal_markers=["Family"])
    assert config.team_domains == ["example.com"]
    assert config.personal_markers == ["family"]


# --- reading the frontmatter -----------------------------------------------------------------


def _note(path: Path, frontmatter: str, body: str = "the transcript\n") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\n{frontmatter}---\n{body}", encoding="utf-8")


def test_read_frontmatter_parses_the_block(tmp_path: Path) -> None:
    path = tmp_path / "call.md"
    _note(path, f"type: call\nparticipants:\n  - ann@{TEAM}\norganizer: ann@{TEAM}\n")
    assert read_frontmatter(path) == {
        "type": "call",
        "participants": [f"ann@{TEAM}"],
        "organizer": f"ann@{TEAM}",
    }


def test_read_frontmatter_returns_nothing_without_a_block(tmp_path: Path) -> None:
    path = tmp_path / "call.md"
    path.write_text("just a note\n", encoding="utf-8")
    assert read_frontmatter(path) == {}


def test_read_frontmatter_returns_nothing_on_broken_yaml(tmp_path: Path) -> None:
    path = tmp_path / "call.md"
    _note(path, "participants: [unclosed\n")
    assert read_frontmatter(path) == {}


def test_read_frontmatter_returns_nothing_on_an_unterminated_block(tmp_path: Path) -> None:
    path = tmp_path / "call.md"
    path.write_text("---\ntype: call\n", encoding="utf-8")
    assert read_frontmatter(path) == {}


def test_read_frontmatter_returns_nothing_for_a_missing_file(tmp_path: Path) -> None:
    assert read_frontmatter(tmp_path / "missing.md") == {}


def test_read_frontmatter_returns_nothing_for_a_list(tmp_path: Path) -> None:
    path = tmp_path / "call.md"
    _note(path, "- one\n- two\n")
    assert read_frontmatter(path) == {}


def test_read_frontmatter_never_reads_the_body(tmp_path: Path) -> None:
    path = tmp_path / "call.md"
    _note(path, f"organizer: ann@{TEAM}\n", body="participants: [eve@other.example]\n")
    assert read_frontmatter(path) == {"organizer": f"ann@{TEAM}"}


TEAM_EVENT = {
    "kind": "call_arrived",
    "path": "meeting.md",
    "route": "team",
    "reason": "3 people, all in team domains",
}


# --- polling --------------------------------------------------------------------------------


def test_notes_waiting_at_startup_are_sent(watch_dir: Path, send: FakeSend) -> None:
    _note(watch_dir / "meeting.md", f"participants: [a@{TEAM}, b@{TEAM}, c@{TEAM}]\norganizer: a@{TEAM}\n")
    router = CallRouter(watch_dir, _config(), send, clock=ManualClock())
    assert router.poll_once() == [TEAM_EVENT]
    assert send.calls == [TEAM_EVENT]
    assert router.poll_once() == []
    assert len(send.calls) == 1


def test_a_new_call_is_sent_with_its_route(watch_dir: Path, send: FakeSend) -> None:
    router = CallRouter(watch_dir, _config(), send, clock=ManualClock())
    router.poll_once()
    _note(watch_dir / "meeting.md", f"participants: [a@{TEAM}, b@{TEAM}, c@{TEAM}]\norganizer: a@{TEAM}\n")
    assert router.poll_once() == [TEAM_EVENT]
    assert send.calls == [TEAM_EVENT]


def test_the_payload_carries_the_route_and_its_reason(watch_dir: Path, send: FakeSend) -> None:
    router = CallRouter(watch_dir, _config(), send, clock=ManualClock())
    router.poll_once()
    _note(watch_dir / "meeting.md", f"participants: [a@{TEAM}, b@{TEAM}, c@{TEAM}]\norganizer: a@{TEAM}\n")
    router.poll_once()
    assert sorted(send.calls[-1]) == ["kind", "path", "reason", "route"]


def test_a_call_in_a_subfolder_keeps_a_relative_path(watch_dir: Path, send: FakeSend) -> None:
    router = CallRouter(watch_dir, _config(), send, clock=ManualClock())
    router.poll_once()
    _note(watch_dir / "2024/meeting.md", f"organizer: a@{TEAM}\n")
    router.poll_once()
    assert send.calls[-1]["path"] == "2024/meeting.md"


def test_a_note_without_frontmatter_asks(watch_dir: Path, send: FakeSend) -> None:
    """Nothing to route on: the brain keeps it personal and asks, so no call goes to the team."""
    router = CallRouter(watch_dir, _config(), send, clock=ManualClock())
    router.poll_once()
    (watch_dir / "call.md").write_text("no frontmatter at all\n", encoding="utf-8")
    router.poll_once()
    assert send.calls[-1]["route"] == "ask"


def test_a_changed_note_is_sent_again(watch_dir: Path, send: FakeSend) -> None:
    router = CallRouter(watch_dir, _config(), send, clock=ManualClock())
    router.poll_once()
    path = watch_dir / "call.md"
    _note(path, f"participants: [a@{TEAM}, b@{TEAM}]\norganizer: a@{TEAM}\n")
    router.poll_once()
    _note(path, f"participants: [a@{TEAM}, b@{TEAM}, c@{TEAM}]\norganizer: a@{TEAM}\n")
    router.poll_once()
    assert [call["route"] for call in send.calls] == ["personal", "team"]


def test_a_note_that_disappears_and_comes_back_is_sent_again(watch_dir: Path, send: FakeSend) -> None:
    router = CallRouter(watch_dir, _config(), send, clock=ManualClock())
    router.poll_once()
    path = watch_dir / "call.md"
    _note(path, f"organizer: a@{TEAM}\n")
    router.poll_once()
    path.unlink()
    router.poll_once()
    _note(path, f"organizer: a@{TEAM}\n")
    router.poll_once()
    assert len(send.calls) == 2


def test_a_refused_call_is_sent_again_on_the_next_poll(watch_dir: Path, send: FakeSend) -> None:
    send.status = 503
    router = CallRouter(watch_dir, _config(), send, clock=ManualClock())
    router.poll_once()
    _note(watch_dir / "call.md", f"participants: [a@{TEAM}, b@{TEAM}]\norganizer: a@{TEAM}\n")
    assert router.poll_once() == []
    send.status = 202
    assert [(event["path"], event["route"]) for event in router.poll_once()] == [("call.md", "personal")]


def test_an_unreachable_hermes_does_not_raise(watch_dir: Path) -> None:
    from second_brain.sidecars.webhook import WebhookError

    def send(payload: Mapping[str, Any]) -> int:
        raise WebhookError("connection refused")

    router = CallRouter(watch_dir, _config(), send, clock=ManualClock())
    router.poll_once()
    _note(watch_dir / "call.md", "type: call\n")
    assert router.poll_once() == []


def test_hidden_and_non_markdown_files_are_ignored(watch_dir: Path, send: FakeSend) -> None:
    router = CallRouter(watch_dir, _config(), send, clock=ManualClock())
    router.poll_once()
    (watch_dir / ".hidden.md").write_text("x\n", encoding="utf-8")
    (watch_dir / "audio.m4a").write_text("x\n", encoding="utf-8")
    assert router.poll_once() == []


# --- construction and the loop --------------------------------------------------------------


def test_send_is_required(watch_dir: Path) -> None:
    with pytest.raises(ValueError):
        CallRouter(watch_dir, _config(), None)  # type: ignore[arg-type]


def test_a_missing_watch_dir_is_accepted_and_waited_for(tmp_path: Path, send: FakeSend) -> None:
    router = CallRouter(tmp_path / "missing", _config(), send)
    assert router.poll_once() == []
    assert send.calls == []

    folder = tmp_path / "missing"
    folder.mkdir()
    _note(folder / "call.md", f"participants: [a@{TEAM}, b@{TEAM}]\norganizer: a@{TEAM}\n")
    assert [event["path"] for event in router.poll_once()] == ["call.md"]
    assert [call["route"] for call in send.calls] == ["personal"]


def test_a_path_that_is_not_a_directory_is_refused(tmp_path: Path, send: FakeSend) -> None:
    file = tmp_path / "calls.md"
    file.write_text("x\n", encoding="utf-8")
    with pytest.raises(ValueError):
        CallRouter(file, _config(), send)


def test_a_useless_poll_interval_is_refused(watch_dir: Path, send: FakeSend) -> None:
    with pytest.raises(ValueError):
        CallRouter(watch_dir, _config(), send, poll_seconds=0)


class Stopper:
    def __init__(self, after: int, router: CallRouter) -> None:
        self.after = after
        self.router = router
        self.calls: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)
        if len(self.calls) >= self.after:
            self.router.stop()


def test_run_forever_polls_then_stops(watch_dir: Path, send: FakeSend) -> None:
    router = CallRouter(watch_dir, _config(), send, clock=time.time, poll_seconds=4.0)
    sleep = Stopper(2, router)
    router._sleep = sleep  # type: ignore[attr-defined]
    _note(watch_dir / "call.md", f"participants: [a@{TEAM}, b@{TEAM}]\norganizer: a@{TEAM}\n")
    router.run_forever()
    assert [call["route"] for call in send.calls] == ["personal"]
    assert sleep.calls == [4.0, 4.0]
