"""Watchdog: heartbeat freshness and the `/ok` endpoint the model gate asks."""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from second_brain.gate.watchdog import Watchdog, make_health_server

MAX_AGE = 45.0


def _beat(path: Path, ts: float) -> Path:
    path.write_text(json.dumps({"ts": ts, "pid": 1}), encoding="utf-8")
    return path


class FakeClock:
    def __init__(self, now: float) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def _watchdog(path: Path, *, now: float) -> Watchdog:
    return Watchdog(path, max_age=MAX_AGE, clock=FakeClock(now))


def test_check_fresh(tmp_path: Path) -> None:
    path = _beat(tmp_path / "heartbeat.json", 1000.0)
    assert _watchdog(path, now=1010.0).check() is True


def test_check_stale(tmp_path: Path) -> None:
    path = _beat(tmp_path / "heartbeat.json", 1000.0)
    assert _watchdog(path, now=1000.0 + MAX_AGE + 1).check() is False


def test_check_missing_file(tmp_path: Path) -> None:
    assert _watchdog(tmp_path / "heartbeat.json", now=1010.0).check() is False


def test_check_corrupt_file(tmp_path: Path) -> None:
    path = tmp_path / "heartbeat.json"
    path.write_text("{not json", encoding="utf-8")
    assert _watchdog(path, now=1010.0).check() is False


def test_check_heartbeat_without_a_timestamp(tmp_path: Path) -> None:
    path = tmp_path / "heartbeat.json"
    path.write_text(json.dumps({"pid": 1}), encoding="utf-8")
    watchdog = _watchdog(path, now=1010.0)
    assert watchdog.check() is False
    assert "without a timestamp" in watchdog.reason()


def test_reason_mentions_the_problem(tmp_path: Path) -> None:
    path = _beat(tmp_path / "heartbeat.json", 1000.0)
    assert "stale" in _watchdog(path, now=1000.0 + MAX_AGE + 5).reason()


def test_reason_mentions_a_future_timestamp(tmp_path: Path) -> None:
    path = _beat(tmp_path / "heartbeat.json", 5000.0)
    assert "future" in _watchdog(path, now=1000.0).reason()


class HealthServer:
    def __init__(self, check, reason=None) -> None:
        self.server = make_health_server(check, "127.0.0.1:0", reason)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/ok"
        self.thread = threading.Thread(target=self.server.serve_forever, args=(0.02,), daemon=True)
        self.thread.start()

    @classmethod
    def for_watchdog(cls, watchdog: Watchdog) -> HealthServer:
        return cls(watchdog.check, watchdog.reason)

    def get(self, path: str = "/ok") -> tuple[int, str]:
        url = self.url.rsplit("/", 1)[0] + path
        try:
            with urllib.request.urlopen(url, timeout=5) as response:
                return response.status, response.read().decode()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


@pytest.fixture
def health(tmp_path: Path):
    path = _beat(tmp_path / "heartbeat.json", 1000.0)
    clock = FakeClock(1000.0)
    watchdog = Watchdog(path, max_age=MAX_AGE, clock=clock)
    server = HealthServer.for_watchdog(watchdog)
    try:
        yield clock, path, server
    finally:
        server.close()


def test_health_endpoint_ok(health) -> None:
    _, _, server = health
    status, body = server.get()
    assert status == 200
    assert body == "ok"


def test_health_endpoint_503_when_stale(health) -> None:
    clock, path, server = health
    _beat(path, 1000.0)
    clock.now = 1000.0 + MAX_AGE + 1
    status, body = server.get()
    assert status == 503
    assert "stale" in body


def test_health_endpoint_503_when_heartbeat_missing(health) -> None:
    _, path, server = health
    path.unlink()
    status, body = server.get()
    assert status == 503
    assert "no heartbeat" in body


def test_health_endpoint_503_when_the_check_raises() -> None:
    def boom() -> bool:
        raise RuntimeError("unexpected")

    server = HealthServer(boom)
    try:
        status, body = server.get()
    finally:
        server.close()
    assert status == 503
    assert "check failed" in body


def test_health_endpoint_404_for_other_paths(health) -> None:
    _, _, server = health
    status, _ = server.get("/check")
    assert status == 404
