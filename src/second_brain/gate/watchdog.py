"""Stop Hermes when the brain_guard heartbeat is missing or stale.

The watchdog is the last line of defence: if the plugin is not loaded, the model must not be
reachable. It answers `GET /ok` for the model gate (fail closed on any doubt) and, when the
heartbeat stays stale past the grace period, stops the Hermes container once through the Docker
socket API.

The heartbeat timestamp is compared against the container start time as well, so a file left over
from a previous run cannot keep a guard-less Hermes alive.
"""

from __future__ import annotations

import http.client
import http.server
import json
import logging
import os
import re
import socket
import threading
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from second_brain.gate import parse_listen
from second_brain.guard.heartbeat import is_fresh, read_heartbeat

log = logging.getLogger(__name__)

StopAction = Callable[[], None]

DEFAULT_MAX_AGE = 45.0
DEFAULT_GRACE = 60.0
DEFAULT_INTERVAL = 5.0
DEFAULT_HEALTH_LISTEN = "0.0.0.0:8081"

_DOCKER_TIMEOUT = 5.0
_CONTAINER_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")


class DockerError(RuntimeError):
    """The Docker socket answered something we cannot use."""


class _UnixHTTPConnection(http.client.HTTPConnection):
    """HTTP over a Unix domain socket, which is how the Docker API is exposed."""

    def __init__(self, socket_path: str, timeout: float = _DOCKER_TIMEOUT) -> None:
        super().__init__("localhost", timeout=timeout)
        self._socket_path = socket_path

    def connect(self) -> None:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        try:
            sock.connect(self._socket_path)
        except OSError:
            sock.close()
            raise
        self.sock = sock


def _docker_request(socket_path: str, method: str, path: str) -> tuple[int, bytes]:
    conn = _UnixHTTPConnection(socket_path)
    try:
        conn.request(method, path, headers={"Host": "docker"})
        response = conn.getresponse()
        return response.status, response.read()
    finally:
        conn.close()


def docker_stop(socket_path: str, container: str, timeout: float = 10.0) -> int:
    """`POST /containers/<name>/stop`; returns the HTTP status Docker answered with."""
    if not _CONTAINER_RE.fullmatch(container):
        raise ValueError(f"not a container name: {container!r}")
    status, _ = _docker_request(socket_path, "POST", f"/containers/{container}/stop?t={int(timeout)}")
    if status not in (204, 304):
        raise DockerError(f"docker stop {container} answered {status}")
    return status


def docker_started_at(socket_path: str, container: str) -> float | None:
    """Start time of a running container, or None when it is not running."""
    if not _CONTAINER_RE.fullmatch(container):
        raise ValueError(f"not a container name: {container!r}")
    status, body = _docker_request(socket_path, "GET", f"/containers/{container}/json")
    if status == 404:
        return None
    if status != 200:
        raise DockerError(f"docker inspect {container} answered {status}")
    try:
        state = json.loads(body)["State"]
    except (ValueError, KeyError, TypeError) as exc:
        raise DockerError(f"docker inspect {container} returned no usable state") from exc
    if not isinstance(state, dict) or not state.get("Running"):
        return None
    raw = state.get("StartedAt")
    if not isinstance(raw, str):
        return None
    return _parse_started_at(container, raw)


def _parse_started_at(container: str, raw: str) -> float:
    """Docker sends RFC 3339 with nanoseconds; datetime handles microseconds."""
    match = re.fullmatch(r"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(?:\.(\d+))?(Z|[+-]\d\d:\d\d)?", raw.strip())
    if match is None:
        raise DockerError(f"docker inspect {container} has an unparseable StartedAt")
    head, fraction, offset = match.groups()
    micros = (fraction or "0")[:6].ljust(6, "0")
    zone = "+00:00" if offset in (None, "Z") else offset
    try:
        return datetime.fromisoformat(f"{head}.{micros}{zone}").timestamp()
    except ValueError as exc:
        raise DockerError(f"docker inspect {container} has an unparseable StartedAt") from exc


class Watchdog:
    """Freshness check over a heartbeat file, plus the grace period before stopping Hermes."""

    def __init__(
        self,
        heartbeat_path: Path | str,
        max_age: float,
        stop_action: StopAction,
        grace: float = DEFAULT_GRACE,
        clock: Callable[[], float] = time.time,
        started_at: float | None = None,
    ) -> None:
        self.heartbeat_path = Path(heartbeat_path)
        self.max_age = float(max_age)
        self.grace = float(grace)
        self.stop_action = stop_action
        self.clock = clock
        self.started_at = clock() if started_at is None else started_at
        self.stopped = False

    def _evaluate(self, now: float) -> tuple[bool, str]:
        if not is_fresh(self.heartbeat_path, self.max_age, now):
            return False, self._stale_reason(now)
        beat = read_heartbeat(self.heartbeat_path)
        ts = None if beat is None else beat.get("ts")
        if isinstance(ts, bool) or not isinstance(ts, (int, float)):
            return False, "heartbeat changed while reading it"
        if ts < self.started_at:
            return False, f"heartbeat older than this run ({now - self.started_at:.0f}s since start)"
        return True, f"heartbeat {now - ts:.0f}s old"

    def _stale_reason(self, now: float) -> str:
        beat = read_heartbeat(self.heartbeat_path)
        if beat is None:
            return "no heartbeat"
        ts = beat.get("ts")
        if isinstance(ts, bool) or not isinstance(ts, (int, float)):
            return "heartbeat without a timestamp"
        delta = now - ts
        if delta < 0:
            return f"heartbeat timestamp in the future ({-delta:.0f}s)"
        return f"heartbeat stale ({delta:.0f}s)"

    def check(self, now: float | None = None) -> bool:
        """True only when the heartbeat exists, is fresh and was written by this run."""
        return self._evaluate(self.clock() if now is None else now)[0]

    def reason(self, now: float | None = None) -> str:
        """Short human explanation of the last check, for the health endpoint and the log."""
        return self._evaluate(self.clock() if now is None else now)[1]

    def tick(self, now: float | None = None) -> bool:
        """One watchdog iteration. Returns True when the guard looks alive."""
        moment = self.clock() if now is None else now
        fresh, reason = self._evaluate(moment)
        if fresh:
            return True
        if moment - self.started_at < self.grace:
            log.info("waiting: %s", reason)
            return False
        if self.stopped:
            return False
        self.stopped = True
        log.error("stopping: %s", reason)
        try:
            self.stop_action()
        except Exception:
            log.exception("stop action failed")
        return False

    def run(self, interval: float = DEFAULT_INTERVAL) -> None:
        """Check forever, calling the stop action once when the grace period has passed."""
        while True:
            self.tick()
            time.sleep(interval)


def _make_handler(
    check: Callable[[], bool], reason: Callable[[], str] | None
) -> type[http.server.BaseHTTPRequestHandler]:
    class HealthHandler(http.server.BaseHTTPRequestHandler):
        server_version = "second-brain-watchdog"

        def do_GET(self) -> None:  # noqa: N802 - http.server naming
            path = self.path.split("?", 1)[0]
            if path != "/ok":
                self._respond(404, "not found")
                return
            try:
                fresh = check()
                detail = "ok" if fresh else (reason() if reason else "not ok")
            except Exception as exc:  # any doubt means no model
                fresh, detail = False, f"check failed: {exc}"
            self._respond(200 if fresh else 503, detail)
            if not fresh:
                log.warning("deny model request: %s", detail)

        def _respond(self, status: int, text: str) -> None:
            body = text.encode()
            self.send_response(status)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: Any) -> None:
            log.debug("%s - %s", self.address_string(), format % args)

    return HealthHandler


def make_health_server(
    check: Callable[[], bool],
    listen: str = DEFAULT_HEALTH_LISTEN,
    reason: Callable[[], str] | None = None,
) -> http.server.ThreadingHTTPServer:
    """Build (but do not start) the HTTP server that answers `GET /ok`."""
    host, port = parse_listen(listen)
    return http.server.ThreadingHTTPServer((host, port), _make_handler(check, reason))


def health_app(
    check: Callable[[], bool],
    listen: str = DEFAULT_HEALTH_LISTEN,
    reason: Callable[[], str] | None = None,
) -> http.server.ThreadingHTTPServer:
    """The health endpoint the model gate calls before letting a request through."""
    return make_health_server(check, listen, reason)


def serve_health(watchdog: Watchdog, listen: str = DEFAULT_HEALTH_LISTEN) -> threading.Thread:
    """Start the health endpoint in a daemon thread and return it."""
    server = health_app(watchdog.check, listen, watchdog.reason)
    thread = threading.Thread(target=server.serve_forever, name="watchdog-health", daemon=True)
    thread.start()
    return thread


def main() -> None:  # pragma: no cover - process entry point
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    heartbeat = Path(os.environ.get("WATCHDOG_HEARTBEAT", "/guard/heartbeat.json"))
    socket_path = os.environ.get("WATCHDOG_DOCKER_SOCKET", "/var/run/docker.sock")
    container = os.environ.get("WATCHDOG_CONTAINER", "hermes")
    max_age = float(os.environ.get("WATCHDOG_MAX_AGE", DEFAULT_MAX_AGE))
    grace = float(os.environ.get("WATCHDOG_GRACE", DEFAULT_GRACE))
    interval = float(os.environ.get("WATCHDOG_INTERVAL", DEFAULT_INTERVAL))
    listen = os.environ.get("WATCHDOG_LISTEN", DEFAULT_HEALTH_LISTEN)

    try:
        started_at = docker_started_at(socket_path, container)
    except (OSError, DockerError):
        log.exception("cannot read the %s start time, using now", container)
        started_at = None

    watchdog = Watchdog(
        heartbeat,
        max_age=max_age,
        stop_action=lambda: docker_stop(socket_path, container),
        grace=grace,
        started_at=started_at,
    )
    serve_health(watchdog, listen)
    log.info("watchdog watching %s, health on %s", heartbeat, listen)
    watchdog.run(interval)


if __name__ == "__main__":
    main()
