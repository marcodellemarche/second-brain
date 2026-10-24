"""Tests for the calendar tools. Every fake value is built at runtime."""

from __future__ import annotations

import base64
import json
import subprocess
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from second_brain.core.types import Autonomy, Effect
from second_brain.tools.calendar import (
    CALENDAR_HOSTS,
    SERVICE_ACCOUNT_HOSTS,
    CalendarConfig,
    CalendarError,
    CalendarEvent,
    EventDraft,
    GoogleCalendarBackend,
    InviteParams,
    ListParams,
    MainWriteParams,
    ServiceAccountToken,
    calendar_tools,
)
from second_brain.tools.registry import Tool
from second_brain.tools.vault import Provenance

ASSISTANT = "assistant-calendar@example.test"
MAIN = "main-calendar@example.test"


def _provenance(**overrides: object) -> Provenance:
    values: dict[str, object] = {
        "requested_by": "user-1",
        "reason": "test call",
        "source_integrity": "trusted",
        "session": "session-1",
    }
    values.update(overrides)
    return Provenance(**values)  # type: ignore[arg-type]


class FakeBackend:
    """A calendar in memory, recording every call so a test can check what was sent."""

    def __init__(self) -> None:
        self.events: dict[str, dict[str, CalendarEvent]] = {ASSISTANT: {}, MAIN: {}}
        self.calls: list[tuple[Any, ...]] = []
        self._next_id = 1

    def _store(self, calendar_id: str) -> dict[str, CalendarEvent]:
        try:
            return self.events[calendar_id]
        except KeyError:
            raise CalendarError("not_found", f"unknown calendar: {calendar_id}") from None

    def list_events(self, calendar_id: str, date_from: date, date_to: date) -> list[CalendarEvent]:
        self.calls.append(("list", calendar_id, date_from, date_to))
        return list(self._store(calendar_id).values())

    def get_event(self, calendar_id: str, event_id: str) -> CalendarEvent:
        self.calls.append(("get", calendar_id, event_id))
        try:
            return self._store(calendar_id)[event_id]
        except KeyError:
            raise CalendarError("not_found", f"no event {event_id}") from None

    def create_event(
        self, calendar_id: str, draft: EventDraft, *, send_updates: str = "none"
    ) -> CalendarEvent:
        self.calls.append(("create", calendar_id, draft, send_updates))
        event_id = f"evt-{self._next_id}"
        self._next_id += 1
        event = CalendarEvent(
            id=event_id,
            title=draft.title,
            start=draft.start,
            end=draft.end,
            organizer="owner@example.test",
            attendees=draft.attendees,
            description=draft.description,
        )
        self._store(calendar_id)[event_id] = event
        return event

    def update_event(
        self, calendar_id: str, event_id: str, draft: EventDraft, *, send_updates: str = "none"
    ) -> CalendarEvent:
        self.calls.append(("update", calendar_id, event_id, draft, send_updates))
        existing = self.get_event(calendar_id, event_id)
        event = CalendarEvent(
            id=event_id,
            title=draft.title,
            start=draft.start,
            end=draft.end,
            organizer=existing.organizer,
            attendees=existing.attendees,
            description=draft.description,
        )
        self._store(calendar_id)[event_id] = event
        return event

    def delete_event(self, calendar_id: str, event_id: str, *, send_updates: str = "none") -> None:
        self.calls.append(("delete", calendar_id, event_id, send_updates))
        self.get_event(calendar_id, event_id)
        del self._store(calendar_id)[event_id]

    def add_attendees(
        self,
        calendar_id: str,
        event_id: str,
        attendees: list[str],
        *,
        send_updates: str = "none",
    ) -> CalendarEvent:
        self.calls.append(("invite", calendar_id, event_id, tuple(attendees), send_updates))
        existing = self.get_event(calendar_id, event_id)
        event = CalendarEvent(
            id=event_id,
            title=existing.title,
            start=existing.start,
            end=existing.end,
            organizer=existing.organizer,
            attendees=tuple(attendees),
            description=existing.description,
        )
        self._store(calendar_id)[event_id] = event
        return event

    def calls_of(self, kind: str) -> list[tuple[Any, ...]]:
        return [call for call in self.calls if call[0] == kind]


@pytest.fixture
def backend() -> FakeBackend:
    return FakeBackend()


@pytest.fixture
def config() -> CalendarConfig:
    return CalendarConfig(
        assistant_calendar_id=ASSISTANT,
        main_calendar_id=MAIN,
        timezone="Europe/Rome",
    )


@pytest.fixture
def tools(backend: FakeBackend, config: CalendarConfig) -> dict[str, Tool]:
    return {tool.spec.name: tool for tool in calendar_tools(backend, config)}


def _seed(backend: FakeBackend, calendar_id: str, title: str = "existing") -> str:
    event = backend.create_event(
        calendar_id,
        EventDraft(title=title, start="2026-01-05T09:00:00+01:00", end="2026-01-05T10:00:00+01:00"),
    )
    backend.calls.clear()
    return event.id


def _call(tools: dict[str, Tool], name: str, params: dict[str, Any], **prov: object) -> dict[str, Any]:
    tool = tools[name]
    parsed = tool.spec.params.model_validate(params)
    return tool.handler(parsed, _provenance(**prov))


# --- specs ---------------------------------------------------------------------------------


def test_tools_are_declared_with_their_effect_and_autonomy(tools: dict[str, Tool]) -> None:
    declared = {name: (tool.spec.effect, tool.spec.autonomy) for name, tool in tools.items()}
    assert declared == {
        "calendar_list": (Effect.READ, Autonomy.AUTO),
        "calendar_create": (Effect.INTERNAL, Autonomy.NOTIFY),
        "calendar_update": (Effect.INTERNAL, Autonomy.NOTIFY),
        "calendar_delete": (Effect.INTERNAL, Autonomy.NOTIFY),
        "calendar_main_write": (Effect.VISIBLE, Autonomy.ASK),
        "calendar_invite": (Effect.VISIBLE, Autonomy.ASK),
    }


def test_list_description_says_the_result_is_untrusted(tools: dict[str, Tool]) -> None:
    description = tools["calendar_list"].spec.description.lower()
    assert "untrusted" in description


def test_invite_description_says_no_email_is_sent(tools: dict[str, Tool]) -> None:
    description = tools["calendar_invite"].spec.description
    assert "sendUpdates=none" in description
    assert "does not" in description


def test_create_has_no_attendees_and_no_calendar_param(tools: dict[str, Tool]) -> None:
    fields = set(tools["calendar_create"].spec.params.model_fields)
    assert fields == {"title", "start", "end", "description"}


def test_config_forbids_extra_keys() -> None:
    with pytest.raises(ValidationError):
        CalendarConfig.model_validate(
            {
                "assistant_calendar_id": ASSISTANT,
                "main_calendar_id": MAIN,
                "timezone": "Europe/Rome",
                "root": "/tmp",
            }
        )


# --- list ----------------------------------------------------------------------------------


def test_list_reads_both_calendars_by_default(tools: dict[str, Tool], backend: FakeBackend) -> None:
    _seed(backend, ASSISTANT, "on assistant")
    _seed(backend, MAIN, "on main")
    result = _call(tools, "calendar_list", {"date_from": "2026-01-01", "date_to": "2026-01-31"})
    assert result["ok"] is True
    assert {event["calendar"] for event in result["events"]} == {"assistant", "main"}
    assert {event["title"] for event in result["events"]} == {"on assistant", "on main"}


def test_list_can_target_one_calendar(tools: dict[str, Tool], backend: FakeBackend) -> None:
    _seed(backend, ASSISTANT, "on assistant")
    _seed(backend, MAIN, "on main")
    result = _call(
        tools,
        "calendar_list",
        {"date_from": "2026-01-01", "date_to": "2026-01-31", "calendar": "main"},
    )
    assert [event["calendar"] for event in result["events"]] == ["main"]


def test_list_returns_the_attendee_count_and_the_description(
    tools: dict[str, Tool], backend: FakeBackend
) -> None:
    backend.events[ASSISTANT]["evt-9"] = CalendarEvent(
        id="evt-9",
        title="standup",
        start="2026-01-05T09:00:00+01:00",
        end="2026-01-05T09:15:00+01:00",
        attendees=("a@example.test", "b@example.test"),
        description="agenda",
    )
    result = _call(tools, "calendar_list", {"date_from": "2026-01-01", "date_to": "2026-01-31"})
    event = next(item for item in result["events"] if item["id"] == "evt-9")
    assert event["attendees"] == 2
    assert event["description"] == "agenda"


def test_list_refuses_a_backwards_range() -> None:
    with pytest.raises(ValidationError):
        ListParams.model_validate({"date_from": "2026-01-31", "date_to": "2026-01-01"})


# --- create --------------------------------------------------------------------------------


def test_create_goes_to_the_assistant_calendar_only(tools: dict[str, Tool], backend: FakeBackend) -> None:
    result = _call(
        tools,
        "calendar_create",
        {
            "title": "dentist",
            "start": "2026-01-05T09:00:00+01:00",
            "end": "2026-01-05T10:00:00+01:00",
        },
    )
    assert result["ok"] is True
    assert [call[1] for call in backend.calls_of("create")] == [ASSISTANT]
    assert backend.events[MAIN] == {}
    assert result["event"]["calendar"] == "assistant"


def test_create_never_sends_updates(tools: dict[str, Tool], backend: FakeBackend) -> None:
    _call(
        tools,
        "calendar_create",
        {
            "title": "dentist",
            "start": "2026-01-05T09:00:00+01:00",
            "end": "2026-01-05T10:00:00+01:00",
        },
    )
    assert backend.calls_of("create")[0][-1] == "none"


# --- update and delete ---------------------------------------------------------------------


def test_update_changes_an_assistant_event(tools: dict[str, Tool], backend: FakeBackend) -> None:
    event_id = _seed(backend, ASSISTANT)
    result = _call(
        tools,
        "calendar_update",
        {
            "event_id": event_id,
            "title": "renamed",
            "start": "2026-01-05T11:00:00+01:00",
            "end": "2026-01-05T12:00:00+01:00",
        },
    )
    assert result["ok"] is True
    assert backend.events[ASSISTANT][event_id].title == "renamed"


def test_update_refuses_an_event_of_the_main_calendar(tools: dict[str, Tool], backend: FakeBackend) -> None:
    event_id = _seed(backend, MAIN)
    result = _call(
        tools,
        "calendar_update",
        {
            "event_id": event_id,
            "title": "renamed",
            "start": "2026-01-05T11:00:00+01:00",
            "end": "2026-01-05T12:00:00+01:00",
        },
    )
    assert result["ok"] is False
    assert result["code"] == "foreign_calendar"
    assert backend.calls_of("update") == []
    assert backend.events[MAIN][event_id].title == "existing"


def test_delete_removes_an_assistant_event(tools: dict[str, Tool], backend: FakeBackend) -> None:
    event_id = _seed(backend, ASSISTANT)
    result = _call(tools, "calendar_delete", {"event_id": event_id})
    assert result["ok"] is True
    assert backend.events[ASSISTANT] == {}


def test_delete_refuses_an_event_of_the_main_calendar(tools: dict[str, Tool], backend: FakeBackend) -> None:
    event_id = _seed(backend, MAIN)
    result = _call(tools, "calendar_delete", {"event_id": event_id})
    assert result["ok"] is False
    assert result["code"] == "foreign_calendar"
    assert backend.calls_of("delete") == []
    assert event_id in backend.events[MAIN]


def test_update_and_delete_refuse_an_unknown_event(tools: dict[str, Tool], backend: FakeBackend) -> None:
    update = _call(
        tools,
        "calendar_update",
        {
            "event_id": "missing",
            "title": "renamed",
            "start": "2026-01-05T11:00:00+01:00",
            "end": "2026-01-05T12:00:00+01:00",
        },
    )
    delete = _call(tools, "calendar_delete", {"event_id": "missing"})
    assert update["code"] == "not_found"
    assert delete["code"] == "not_found"


# --- main write ----------------------------------------------------------------------------


def test_main_write_refuses_without_approval(tools: dict[str, Tool], backend: FakeBackend) -> None:
    result = _call(
        tools,
        "calendar_main_write",
        {
            "action": "create",
            "title": "board meeting",
            "start": "2026-01-05T09:00:00+01:00",
            "end": "2026-01-05T10:00:00+01:00",
        },
    )
    assert result["ok"] is False
    assert result["code"] == "approval_required"
    assert backend.calls == []


def test_main_write_creates_with_approval(tools: dict[str, Tool], backend: FakeBackend) -> None:
    result = _call(
        tools,
        "calendar_main_write",
        {
            "action": "create",
            "title": "board meeting",
            "start": "2026-01-05T09:00:00+01:00",
            "end": "2026-01-05T10:00:00+01:00",
        },
        approval="approved:call-1",
    )
    assert result["ok"] is True
    assert [call[1] for call in backend.calls_of("create")] == [MAIN]
    assert backend.calls_of("create")[0][-1] == "none"


def test_main_write_updates_with_approval(tools: dict[str, Tool], backend: FakeBackend) -> None:
    event_id = _seed(backend, MAIN)
    result = _call(
        tools,
        "calendar_main_write",
        {
            "action": "update",
            "event_id": event_id,
            "title": "renamed",
            "start": "2026-01-05T11:00:00+01:00",
            "end": "2026-01-05T12:00:00+01:00",
        },
        approval="approved:call-2",
    )
    assert result["ok"] is True
    assert backend.events[MAIN][event_id].title == "renamed"


def test_main_write_deletes_with_approval(tools: dict[str, Tool], backend: FakeBackend) -> None:
    event_id = _seed(backend, MAIN)
    result = _call(
        tools,
        "calendar_main_write",
        {"action": "delete", "event_id": event_id},
        approval="approved:call-3",
    )
    assert result["ok"] is True
    assert backend.events[MAIN] == {}


def test_main_write_needs_the_fields_of_its_action() -> None:
    with pytest.raises(ValidationError):
        MainWriteParams.model_validate({"action": "update", "event_id": "evt-1"})
    with pytest.raises(ValidationError):
        MainWriteParams.model_validate({"action": "delete"})
    with pytest.raises(ValidationError):
        MainWriteParams.model_validate({"action": "create", "event_id": "evt-1", "title": "x"})


# --- invite --------------------------------------------------------------------------------


def test_invite_refuses_without_approval(tools: dict[str, Tool], backend: FakeBackend) -> None:
    event_id = _seed(backend, ASSISTANT)
    result = _call(
        tools,
        "calendar_invite",
        {"event_id": event_id, "calendar": "assistant", "attendees": ["a@example.test"]},
    )
    assert result["ok"] is False
    assert result["code"] == "approval_required"
    assert backend.calls_of("invite") == []


def test_invite_adds_attendees_with_approval(tools: dict[str, Tool], backend: FakeBackend) -> None:
    event_id = _seed(backend, MAIN)
    result = _call(
        tools,
        "calendar_invite",
        {"event_id": event_id, "calendar": "main", "attendees": ["a@example.test"]},
        approval="approved:call-4",
    )
    assert result["ok"] is True
    assert backend.events[MAIN][event_id].attendees == ("a@example.test",)
    assert result["event"]["attendees"] == 1


def test_invite_always_uses_send_updates_none(tools: dict[str, Tool], backend: FakeBackend) -> None:
    event_id = _seed(backend, ASSISTANT)
    _call(
        tools,
        "calendar_invite",
        {"event_id": event_id, "calendar": "assistant", "attendees": ["a@example.test"]},
        approval="approved:call-5",
    )
    assert backend.calls_of("invite")[0][-1] == "none"


def test_invite_refuses_more_than_ten_attendees(tools: dict[str, Tool]) -> None:
    attendees = [f"person{index}@example.test" for index in range(11)]
    with pytest.raises(ValidationError):
        InviteParams.model_validate({"event_id": "evt-1", "calendar": "assistant", "attendees": attendees})


def test_invite_accepts_ten_attendees(tools: dict[str, Tool]) -> None:
    attendees = [f"person{index}@example.test" for index in range(10)]
    params = InviteParams.model_validate(
        {"event_id": "evt-1", "calendar": "assistant", "attendees": attendees}
    )
    assert len(params.attendees) == 10


@pytest.mark.parametrize(
    "address",
    ["not-an-email", "a@b", "@example.test", "a b@example.test", "a@example", ""],
)
def test_invite_validates_the_email_addresses(address: str) -> None:
    with pytest.raises(ValidationError):
        InviteParams.model_validate({"event_id": "evt-1", "calendar": "assistant", "attendees": [address]})


def test_invite_refuses_an_empty_attendee_list() -> None:
    with pytest.raises(ValidationError):
        InviteParams.model_validate({"event_id": "evt-1", "calendar": "assistant", "attendees": []})


def test_invite_needs_a_known_calendar() -> None:
    with pytest.raises(ValidationError):
        InviteParams.model_validate(
            {"event_id": "evt-1", "calendar": "shared", "attendees": ["a@example.test"]}
        )


# --- backend -------------------------------------------------------------------------------


def test_backend_builds_the_event_body_and_never_asks_for_updates(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[dict[str, Any]] = []

    def fake_urlopen(request: Any, timeout: float = 0) -> Any:
        seen.append(
            {
                "url": request.full_url,
                "method": request.get_method(),
                "body": request.data,
                "auth": request.get_header("Authorization"),
            }
        )

        class Response:
            def __enter__(self) -> Response:
                return self

            def __exit__(self, *args: object) -> None:
                return None

            def read(self) -> bytes:
                return b'{"id": "evt-1", "summary": "dentist"}'

        return Response()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    backend = GoogleCalendarBackend(lambda: "token-1", "Europe/Rome")
    event = backend.create_event(
        ASSISTANT,
        EventDraft(
            title="dentist",
            start="2026-01-05T09:00:00+01:00",
            end="2026-01-05T10:00:00+01:00",
        ),
    )
    assert event.id == "evt-1"
    assert seen[0]["method"] == "POST"
    assert "sendUpdates=none" in seen[0]["url"]
    assert seen[0]["auth"] == "Bearer token-1"
    assert b"attendees" not in seen[0]["body"]


def test_backend_answers_in_the_configured_timezone(monkeypatch: pytest.MonkeyPatch) -> None:
    # The calendar itself is set to another zone: the times come back in the owner's one.
    payload = (
        b'{"items": [{"id": "evt-1", "summary": "gym",'
        b' "start": {"dateTime": "2026-10-21T19:30:00+02:00"},'
        b' "end": {"dateTime": "2026-10-21T20:50:00+02:00"}},'
        b' {"id": "evt-2", "summary": "holiday",'
        b' "start": {"date": "2026-10-22"}, "end": {"date": "2026-10-23"}}]}'
    )

    def fake_urlopen(request: Any, timeout: float = 0) -> Any:
        class Response:
            def __enter__(self) -> Response:
                return self

            def __exit__(self, *args: object) -> None:
                return None

            def read(self) -> bytes:
                return payload

        return Response()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    backend = GoogleCalendarBackend(lambda: "token-1", "Europe/Lisbon")
    events = backend.list_events(ASSISTANT, date(2026, 10, 21), date(2026, 10, 22))
    assert (events[0].start, events[0].end) == ("2026-10-21T18:30:00+01:00", "2026-10-21T19:50:00+01:00")
    assert (events[1].start, events[1].end) == ("2026-10-22", "2026-10-23")


def test_backend_asks_for_the_zone_on_every_call(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = (
        b'{"items": [{"id": "evt-1", "summary": "gym",'
        b' "start": {"dateTime": "2026-10-21T17:30:00Z"}, "end": {"dateTime": "2026-10-21T18:50:00Z"}}]}'
    )

    def fake_urlopen(request: Any, timeout: float = 0) -> Any:
        class Response:
            def __enter__(self) -> Response:
                return self

            def __exit__(self, *args: object) -> None:
                return None

            def read(self) -> bytes:
                return payload

        return Response()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    zones = ["Europe/Lisbon"]
    backend = GoogleCalendarBackend(lambda: "token-1", lambda: zones[0])
    assert backend.list_events(ASSISTANT, date(2026, 10, 21), date(2026, 10, 21))[0].start.endswith("+01:00")
    zones[0] = "Europe/Rome"
    assert backend.list_events(ASSISTANT, date(2026, 10, 21), date(2026, 10, 21))[0].start.endswith("+02:00")


def test_backend_turns_a_404_into_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    import urllib.error

    def fake_urlopen(request: Any, timeout: float = 0) -> Any:
        raise urllib.error.HTTPError(request.full_url, 404, "gone", {}, None)  # type: ignore[arg-type]

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    backend = GoogleCalendarBackend(lambda: "token-1", "Europe/Rome")
    with pytest.raises(CalendarError) as excinfo:
        backend.get_event(ASSISTANT, "evt-1")
    assert excinfo.value.code == "not_found"


def _service_account_key(tmp_path: Path, *, key_id: str = "sa-1") -> Path:
    """A real RSA key and a service-account JSON around it, so `openssl` really signs."""
    private = tmp_path / f"{key_id}.pem"
    subprocess.run(
        ["openssl", "genpkey", "-algorithm", "RSA", "-pkeyopt", "rsa_keygen_bits:2048", "-out", str(private)],
        check=True,
        capture_output=True,
    )
    key = {
        "type": "service_account",
        "client_email": f"{key_id}@example.iam.gserviceaccount.com",
        "private_key": private.read_text(encoding="utf-8"),
        "token_uri": "https://oauth2.googleapis.com/token",
    }
    path = tmp_path / f"{key_id}.json"
    path.write_text(json.dumps(key), encoding="utf-8")
    return path


def test_a_service_account_mints_a_token_and_caches_it(tmp_path: Path) -> None:
    key_file = _service_account_key(tmp_path)
    calls: list[bytes] = []
    clock = [1000.0]

    def exchange(_uri: str, body: bytes) -> dict[str, Any]:
        calls.append(body)
        return {"access_token": "minted-1", "expires_in": 3600}

    token = ServiceAccountToken(key_file, clock=lambda: clock[0], timeout=5.0, exchange=exchange)

    assert token() == "minted-1"
    assert token() == "minted-1"
    assert len(calls) == 1
    clock[0] += 3600
    assert token() == "minted-1"
    assert len(calls) == 2


def test_the_assertion_is_a_signed_jwt_with_the_right_claims(tmp_path: Path) -> None:
    key_file = _service_account_key(tmp_path)
    key = json.loads(key_file.read_text(encoding="utf-8"))
    token = ServiceAccountToken(key_file, clock=lambda: 1000.0, timeout=5.0)

    assertion = token._assertion(key)
    header, claims, signature = assertion.split(".")
    assert json.loads(_b64decode(header))["alg"] == "RS256"
    payload = json.loads(_b64decode(claims))
    assert payload["iss"] == key["client_email"]
    assert payload["scope"] == "https://www.googleapis.com/auth/calendar.events"
    assert payload["exp"] - payload["iat"] == 3600
    assert len(_b64decode(signature)) == 256


def test_a_key_that_cannot_be_signed_is_a_backend_error(tmp_path: Path) -> None:
    key_file = tmp_path / "broken.json"
    key_file.write_text(
        json.dumps({"client_email": "x@example.test", "private_key": "not a key"}), encoding="utf-8"
    )
    token = ServiceAccountToken(key_file, timeout=5.0)

    with pytest.raises(CalendarError) as error:
        token()
    assert error.value.code == "backend_error"


def _b64decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


def test_a_service_account_deployment_also_reaches_the_token_host() -> None:
    # The assertion is traded for a token at oauth2.googleapis.com before any API call, so a
    # deployment with a service account has to be allowed to reach it.
    assert "oauth2.googleapis.com" in SERVICE_ACCOUNT_HOSTS
    assert "oauth2.googleapis.com" not in CALENDAR_HOSTS


def test_extra_hosts_land_on_every_calendar_tool() -> None:
    backend = GoogleCalendarBackend(lambda: "token-1", "Europe/Rome")
    config = CalendarConfig(
        assistant_calendar_id="a@example.test",
        main_calendar_id="m@example.test",
        timezone="Europe/Rome",
    )

    tools = calendar_tools(backend, config, extra_hosts=SERVICE_ACCOUNT_HOSTS)

    assert tools
    for tool in tools:
        assert "oauth2.googleapis.com" in tool.spec.hosts
        assert "www.googleapis.com" in tool.spec.hosts
