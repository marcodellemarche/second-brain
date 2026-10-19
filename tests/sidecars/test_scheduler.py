"""Tests for the scheduler: when a task fires, once, and what it sends."""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime
from datetime import time as dt_time
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from second_brain.sidecars.scheduler import (
    ScheduleConfig,
    ScheduleEntry,
    Scheduler,
)
from second_brain.sidecars.webhook import WebhookError

ROME = ZoneInfo("Europe/Rome")


def epoch(local: datetime) -> float:
    return local.timestamp()


def at_rome(year: int, month: int, day: int, hour: int, minute: int = 0, second: int = 0) -> float:
    return epoch(datetime(year, month, day, hour, minute, second, tzinfo=ROME))


class FakeSend:
    def __init__(self, status: int = 202, error: Exception | None = None) -> None:
        self.status = status
        self.error = error
        self.calls: list[dict[str, Any]] = []

    def __call__(self, payload: Mapping[str, Any]) -> int:
        self.calls.append(dict(payload))
        if self.error is not None:
            raise self.error
        return self.status


class ManualClock:
    def __init__(self, now: float) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def set(self, now: float) -> None:
        self.now = now


def _config(*entries: ScheduleEntry, **overrides: Any) -> ScheduleConfig:
    values: dict[str, Any] = {
        "timezone": "Europe/Rome",
        "entries": list(entries) or [_entry()],
    }
    values.update(overrides)
    return ScheduleConfig(**values)


def _entry(**overrides: Any) -> ScheduleEntry:
    values: dict[str, Any] = {"name": "digest_morning", "route": "digest", "at": "08:00"}
    values.update(overrides)
    return ScheduleEntry(**values)


def _scheduler(
    config: ScheduleConfig,
    senders: Mapping[str, FakeSend],
    state_file: Path,
    now: float,
) -> tuple[Scheduler, ManualClock]:
    clock = ManualClock(now)
    scheduler = Scheduler(config, senders, state_file=state_file, clock=clock, sleep=lambda _s: None)
    return scheduler, clock


def test_does_not_fire_before_the_due_time(tmp_path: Path) -> None:
    send = FakeSend()
    scheduler, _ = _scheduler(
        _config(), {"digest": send}, tmp_path / "state.json", at_rome(2026, 6, 1, 7, 59, 59)
    )
    assert scheduler.poll_once() == []
    assert send.calls == []


def test_fires_at_the_due_time(tmp_path: Path) -> None:
    send = FakeSend()
    scheduler, _ = _scheduler(
        _config(), {"digest": send}, tmp_path / "state.json", at_rome(2026, 6, 1, 8, 0, 0)
    )
    sent = scheduler.poll_once()
    assert len(sent) == 1
    assert send.calls == [{"task": "digest_morning", "due": "2026-06-01T08:00:00+02:00", "weekday": "mon"}]


def test_payload_has_exactly_the_three_keys(tmp_path: Path) -> None:
    send = FakeSend()
    scheduler, _ = _scheduler(_config(), {"digest": send}, tmp_path / "state.json", at_rome(2026, 6, 1, 8, 5))
    scheduler.poll_once()
    assert set(send.calls[0]) == {"task", "due", "weekday"}


def test_fires_once_and_not_on_every_poll(tmp_path: Path) -> None:
    send = FakeSend()
    scheduler, clock = _scheduler(
        _config(), {"digest": send}, tmp_path / "state.json", at_rome(2026, 6, 1, 8, 0)
    )
    assert len(scheduler.poll_once()) == 1
    clock.set(at_rome(2026, 6, 1, 8, 1))
    assert scheduler.poll_once() == []
    clock.set(at_rome(2026, 6, 1, 23, 59))
    assert scheduler.poll_once() == []
    assert len(send.calls) == 1


def test_fires_again_the_next_day(tmp_path: Path) -> None:
    send = FakeSend()
    scheduler, clock = _scheduler(
        _config(), {"digest": send}, tmp_path / "state.json", at_rome(2026, 6, 1, 8, 0)
    )
    scheduler.poll_once()
    clock.set(at_rome(2026, 6, 2, 8, 0))
    assert len(scheduler.poll_once()) == 1
    assert send.calls[-1]["due"] == "2026-06-02T08:00:00+02:00"
    assert send.calls[-1]["weekday"] == "tue"


def test_catch_up_within_the_window_after_a_restart(tmp_path: Path) -> None:
    state = tmp_path / "state.json"
    send = FakeSend()
    first, _ = _scheduler(_config(), {"digest": send}, state, at_rome(2026, 5, 31, 8, 0))
    first.poll_once()

    restarted, _ = _scheduler(_config(), {"digest": send}, state, at_rome(2026, 6, 1, 8, 10))
    assert len(restarted.poll_once()) == 1
    assert send.calls[-1]["due"] == "2026-06-01T08:00:00+02:00"


def test_first_start_within_the_window_fires(tmp_path: Path) -> None:
    send = FakeSend()
    scheduler, _ = _scheduler(
        _config(), {"digest": send}, tmp_path / "state.json", at_rome(2026, 6, 1, 8, 10)
    )
    assert len(scheduler.poll_once()) == 1


def test_no_catch_up_beyond_the_window(tmp_path: Path) -> None:
    state = tmp_path / "state.json"
    send = FakeSend()
    first, _ = _scheduler(_config(), {"digest": send}, state, at_rome(2026, 5, 31, 8, 0))
    first.poll_once()

    restarted, _ = _scheduler(_config(), {"digest": send}, state, at_rome(2026, 6, 1, 12, 0))
    assert restarted.poll_once() == []
    assert len(send.calls) == 1


def test_weekday_filter(tmp_path: Path) -> None:
    send = FakeSend()
    config = _config(_entry(days=["mon", "wed"]))
    scheduler, clock = _scheduler(
        config, {"digest": send}, tmp_path / "state.json", at_rome(2026, 6, 2, 9, 0)
    )
    assert scheduler.poll_once() == []

    clock.set(at_rome(2026, 6, 3, 8, 0))
    assert len(scheduler.poll_once()) == 1
    assert send.calls[-1]["weekday"] == "wed"


def test_non_2xx_is_retried_on_the_next_poll_then_succeeds(tmp_path: Path) -> None:
    send = FakeSend(status=500)
    scheduler, clock = _scheduler(
        _config(), {"digest": send}, tmp_path / "state.json", at_rome(2026, 6, 1, 8, 0)
    )
    assert scheduler.poll_once() == []
    clock.set(at_rome(2026, 6, 1, 8, 0, 30))
    assert scheduler.poll_once() == []

    send.status = 202
    clock.set(at_rome(2026, 6, 1, 8, 1))
    assert len(scheduler.poll_once()) == 1
    clock.set(at_rome(2026, 6, 1, 8, 2))
    assert scheduler.poll_once() == []
    assert len(send.calls) == 3


def test_raised_webhook_error_is_retried_then_succeeds_once(tmp_path: Path) -> None:
    send = FakeSend(error=WebhookError("hermes is down"))
    scheduler, clock = _scheduler(
        _config(), {"digest": send}, tmp_path / "state.json", at_rome(2026, 6, 1, 8, 0)
    )
    assert scheduler.poll_once() == []
    send.error = None
    clock.set(at_rome(2026, 6, 1, 8, 0, 30))
    assert len(scheduler.poll_once()) == 1
    clock.set(at_rome(2026, 6, 1, 8, 1))
    assert scheduler.poll_once() == []
    assert len(send.calls) == 2


def test_retry_gives_up_after_the_catch_up_window(tmp_path: Path) -> None:
    send = FakeSend(status=503)
    scheduler, clock = _scheduler(
        _config(), {"digest": send}, tmp_path / "state.json", at_rome(2026, 6, 1, 8, 0)
    )
    assert scheduler.poll_once() == []
    send.status = 202
    clock.set(at_rome(2026, 6, 1, 8, 31))
    assert scheduler.poll_once() == []
    assert len(send.calls) == 1


def test_corrupt_state_file_is_treated_as_empty(tmp_path: Path) -> None:
    state = tmp_path / "state.json"
    state.write_text("{not json at all", encoding="utf-8")
    send = FakeSend()
    scheduler, _ = _scheduler(_config(), {"digest": send}, state, at_rome(2026, 6, 1, 8, 5))
    assert len(scheduler.poll_once()) == 1
    assert json.loads(state.read_text(encoding="utf-8"))["digest_morning"] == "2026-06-01T08:00:00+02:00"


def test_state_file_with_wrong_shape_is_treated_as_empty(tmp_path: Path) -> None:
    state = tmp_path / "state.json"
    state.write_text(json.dumps({"digest_morning": 12345}), encoding="utf-8")
    send = FakeSend()
    scheduler, _ = _scheduler(_config(), {"digest": send}, state, at_rome(2026, 6, 1, 8, 5))
    assert len(scheduler.poll_once()) == 1


def test_state_file_is_written_atomically_and_drops_removed_entries(tmp_path: Path) -> None:
    state = tmp_path / "state.json"
    send = FakeSend()
    both = _config(_entry(name="digest_morning"), _entry(name="lint_nightly"))
    scheduler, clock = _scheduler(both, {"digest": send}, state, at_rome(2026, 6, 1, 8, 0))
    scheduler.poll_once()
    assert set(json.loads(state.read_text(encoding="utf-8"))) == {"digest_morning", "lint_nightly"}

    only_digest = _config(_entry(name="digest_morning"))
    clock.set(at_rome(2026, 6, 2, 8, 0))
    restarted, _ = _scheduler(only_digest, {"digest": send}, state, clock.now)
    restarted.poll_once()
    assert set(json.loads(state.read_text(encoding="utf-8"))) == {"digest_morning"}
    assert [p.name for p in tmp_path.iterdir() if p.name != "state.json"] == []


def test_missing_state_file_means_never_fired(tmp_path: Path) -> None:
    send = FakeSend()
    state = tmp_path / "nested" / "state.json"
    scheduler, _ = _scheduler(_config(), {"digest": send}, state, at_rome(2026, 6, 1, 8, 0))
    assert len(scheduler.poll_once()) == 1
    assert state.is_file()


def test_unknown_route_in_senders_raises(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="no sender for route"):
        Scheduler(
            _config(_entry(route="missing")),
            {"digest": FakeSend()},
            state_file=tmp_path / "state.json",
            clock=lambda: at_rome(2026, 6, 1, 8, 0),
            sleep=lambda _s: None,
        )


def test_run_forever_survives_a_failing_poll(tmp_path: Path) -> None:
    class Boom(FakeSend):
        def __call__(self, payload: Mapping[str, Any]) -> int:
            self.calls.append(dict(payload))
            raise OSError("socket closed")

    send = Boom()
    scheduler, _ = _scheduler(_config(), {"digest": send}, tmp_path / "state.json", at_rome(2026, 6, 1, 8, 0))
    sleeps: list[float] = []

    def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        if len(sleeps) >= 2:
            scheduler.stop()

    scheduler._sleep = sleep  # noqa: SLF001
    scheduler.run_forever(1.0)
    assert len(sleeps) == 2
    assert len(send.calls) == 2


def test_stop_ends_run_forever(tmp_path: Path) -> None:
    send = FakeSend()
    scheduler, _ = _scheduler(_config(), {"digest": send}, tmp_path / "state.json", at_rome(2026, 6, 1, 7, 0))
    calls: list[float] = []

    def sleep(seconds: float) -> None:
        calls.append(seconds)
        scheduler.stop()

    scheduler._sleep = sleep  # noqa: SLF001
    scheduler.run_forever()
    assert calls == [30.0]
    assert send.calls == []


def test_spring_forward_nonexistent_time_fires_once(tmp_path: Path) -> None:
    send = FakeSend()
    config = _config(_entry(at="02:30", days=["sun"]))
    scheduler, clock = _scheduler(
        config, {"digest": send}, tmp_path / "state.json", at_rome(2026, 3, 29, 1, 0)
    )
    assert scheduler.poll_once() == []

    clock.set(at_rome(2026, 3, 29, 3, 45))
    sent = scheduler.poll_once()
    assert len(sent) == 1
    assert sent[0]["due"] == "2026-03-29T02:30:00+01:00"

    clock.set(at_rome(2026, 3, 29, 4, 0))
    assert scheduler.poll_once() == []
    assert len(send.calls) == 1


def test_fall_back_ambiguous_time_fires_once(tmp_path: Path) -> None:
    send = FakeSend()
    config = _config(_entry(at="02:30", days=["sun"]))
    scheduler, clock = _scheduler(
        config, {"digest": send}, tmp_path / "state.json", at_rome(2026, 10, 25, 2, 45)
    )
    sent = scheduler.poll_once()
    assert len(sent) == 1
    assert sent[0]["due"] == "2026-10-25T02:30:00+02:00"

    clock.set(at_rome(2026, 10, 25, 3, 0))
    assert scheduler.poll_once() == []
    clock.set(at_rome(2026, 10, 25, 3, 30))
    assert scheduler.poll_once() == []
    assert len(send.calls) == 1


def test_entry_defaults_to_every_day() -> None:
    assert _entry().days == ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


@pytest.mark.parametrize("bad_at", ["24:00", "8:00", "08:60", "0800", "08:00:00", "", "ab:cd"])
def test_bad_at_is_refused(bad_at: str) -> None:
    with pytest.raises(ValueError):
        _entry(at=bad_at)


def test_bad_day_is_refused() -> None:
    with pytest.raises(ValueError):
        _entry(days=["monday"])


def test_empty_days_are_refused() -> None:
    with pytest.raises(ValueError):
        _entry(days=[])


def test_duplicate_days_are_refused() -> None:
    with pytest.raises(ValueError):
        _entry(days=["mon", "mon"])


def test_duplicate_names_are_refused() -> None:
    with pytest.raises(ValueError, match="duplicate entry name"):
        _config(_entry(), _entry())


def test_bad_timezone_is_refused() -> None:
    with pytest.raises(ValueError, match="unknown timezone"):
        _config(timezone="Mars/Olympus")


def test_extra_keys_are_refused() -> None:
    with pytest.raises(ValueError):
        _entry(extra="nope")
    with pytest.raises(ValueError):
        ScheduleConfig(timezone="Europe/Rome", entries=[], poll_seconds=1)  # type: ignore[call-arg]


@pytest.mark.parametrize("bad_name", ["Digest", "-digest", "a" * 64, "", "with space"])
def test_bad_name_is_refused(bad_name: str) -> None:
    with pytest.raises(ValueError):
        _entry(name=bad_name)


def test_catch_up_bounds() -> None:
    with pytest.raises(ValueError):
        _config(catch_up_minutes=-1)
    with pytest.raises(ValueError):
        _config(catch_up_minutes=721)
    assert _config(catch_up_minutes=0).catch_up_minutes == 0


def test_at_time_parses() -> None:
    assert _entry(at="03:05").at_time() == dt_time(3, 5)


def test_scheduler_keeps_config_and_state_file(tmp_path: Path) -> None:
    state = tmp_path / "state.json"
    config = _config()
    scheduler = Scheduler(config, {"digest": FakeSend()}, state_file=state)
    assert scheduler.config is config
    assert scheduler.state_file == state


def test_poll_seconds_must_be_positive(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="poll_seconds"):
        Scheduler(
            _config(),
            {"digest": FakeSend()},
            state_file=tmp_path / "state.json",
            poll_seconds=0,
        )
