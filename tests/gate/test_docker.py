"""Docker socket calls: the stop request and the container start time, over a fake Unix socket."""

from __future__ import annotations

import json
import socket
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

from second_brain.gate.watchdog import (
    DockerError,
    docker_started_at,
    docker_stop,
)


class FakeDocker:
    """A Unix socket that answers one canned HTTP response per request."""

    def __init__(self, status: int, body: bytes = b"") -> None:
        self.status = status
        self.body = body
        self.requests: list[str] = []
        self._stop = threading.Event()
        self._path = ""
        self._server: socket.socket | None = None
        self._thread: threading.Thread | None = None

    def __enter__(self) -> FakeDocker:
        return self.start()

    def __exit__(self, *_: object) -> None:
        self.close()

    def start(self) -> FakeDocker:
        directory = Path(__import__("tempfile").mkdtemp())
        self._path = str(directory / "docker.sock")
        self._server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._server.bind(self._path)
        self._server.listen(8)
        self._server.settimeout(0.1)
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()
        return self

    @property
    def path(self) -> str:
        return self._path

    def _serve(self) -> None:
        assert self._server is not None
        while not self._stop.is_set():
            try:
                conn, _ = self._server.accept()
            except (TimeoutError, OSError):
                continue
            with conn:
                data = b""
                while b"\r\n\r\n" not in data:
                    part = conn.recv(4096)
                    if not part:
                        break
                    data += part
                self.requests.append(data.split(b"\r\n", 1)[0].decode())
                reason = "No Content" if self.status == 204 else "OK"
                if self.status >= 400:
                    reason = "Error"
                head = (
                    f"HTTP/1.1 {self.status} {reason}\r\n"
                    f"Content-Length: {len(self.body)}\r\n"
                    "Content-Type: application/json\r\n"
                    "Connection: close\r\n\r\n"
                )
                conn.sendall(head.encode() + self.body)

    def close(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
        if self._server is not None:
            self._server.close()
        Path(self._path).unlink(missing_ok=True)


@pytest.fixture
def stopped() -> Iterator[FakeDocker]:
    with FakeDocker(204) as fake:
        yield fake


def test_stop_posts_to_the_container(stopped: FakeDocker) -> None:
    assert docker_stop(stopped.path, "hermes") == 204
    assert stopped.requests == ["POST /containers/hermes/stop?t=10 HTTP/1.1"]


def test_stop_reports_a_docker_error() -> None:
    with FakeDocker(500) as fake:
        with pytest.raises(DockerError, match="500"):
            docker_stop(fake.path, "hermes")


def test_stop_rejects_a_container_name_that_would_escape_the_path() -> None:
    with FakeDocker(204) as fake:
        with pytest.raises(ValueError):
            docker_stop(fake.path, "../images/json?")
        assert fake.requests == []


def test_stop_fails_when_the_socket_is_missing(tmp_path: Path) -> None:
    with pytest.raises(OSError):
        docker_stop(str(tmp_path / "absent.sock"), "hermes")


def _inspect(state: dict[str, object]) -> FakeDocker:
    return FakeDocker(200, json.dumps({"State": state}).encode())


def test_started_at_parses_nanoseconds() -> None:
    with _inspect({"Running": True, "StartedAt": "2024-05-06T07:08:09.123456789Z"}) as fake:
        value = docker_started_at(fake.path, "hermes")
    assert value is not None
    assert value == pytest.approx(1714979289.123456, abs=1e-6)


def test_started_at_handles_no_fraction() -> None:
    with _inspect({"Running": True, "StartedAt": "2024-05-06T07:08:09Z"}) as fake:
        value = docker_started_at(fake.path, "hermes")
    assert value == pytest.approx(1714979289.0, abs=1e-6)


def test_started_at_is_none_when_not_running() -> None:
    with _inspect({"Running": False, "StartedAt": "2024-05-06T07:08:09Z"}) as fake:
        assert docker_started_at(fake.path, "hermes") is None


def test_started_at_is_none_when_the_container_does_not_exist() -> None:
    with FakeDocker(404, b'{"message": "no such container"}') as fake:
        assert docker_started_at(fake.path, "hermes") is None


def test_started_at_rejects_garbage() -> None:
    with _inspect({"Running": True, "StartedAt": "yesterday"}) as fake:
        with pytest.raises(DockerError):
            docker_started_at(fake.path, "hermes")
