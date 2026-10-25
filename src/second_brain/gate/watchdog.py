"""Tell the model gate whether the brain_guard heartbeat is fresh.

The watchdog is the last line of defence: if the plugin is not loaded, the model must not be
reachable. It answers `GET /ok` for the model gate, which refuses every model call on anything
but a 200. Without the model, Hermes cannot decide to call a tool. The watchdog holds no Docker
socket: it only reads the heartbeat file.

A heartbeat left over from a previous run cannot vouch for a new one: the Hermes image deletes
the file before Hermes starts, so a run whose guard never loads has no heartbeat at all.
"""

from __future__ import annotations

import http.server
import logging
import os
import signal
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from second_brain.gate import parse_listen
from second_brain.guard.heartbeat import is_fresh, read_heartbeat

log = logging.getLogger(__name__)

DEFAULT_MAX_AGE = 45.0
DEFAULT_HEALTH_LISTEN = "0.0.0.0:8081"


class Watchdog:
    """Freshness check over the heartbeat file the guard writes."""

    def __init__(
        self,
        heartbeat_path: Path | str,
        max_age: float = DEFAULT_MAX_AGE,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.heartbeat_path = Path(heartbeat_path)
        self.max_age = float(max_age)
        self.clock = clock or time.time

    def _evaluate(self, now: float) -> tuple[bool, str]:
        beat = read_heartbeat(self.heartbeat_path)
        if beat is None:
            return False, "no heartbeat"
        ts = beat.get("ts")
        if isinstance(ts, bool) or not isinstance(ts, (int, float)):
            return False, "heartbeat without a timestamp"
        if not is_fresh(self.heartbeat_path, self.max_age, now):
            delta = now - ts
            if delta < 0:
                return False, f"heartbeat timestamp in the future ({-delta:.0f}s)"
            return False, f"heartbeat stale ({delta:.0f}s)"
        return True, f"heartbeat {now - ts:.0f}s old"

    def check(self, now: float | None = None) -> bool:
        """True only when the heartbeat exists and is fresh."""
        return self._evaluate(self.clock() if now is None else now)[0]

    def reason(self, now: float | None = None) -> str:
        """Short human explanation of the last check, for the health endpoint and the log."""
        return self._evaluate(self.clock() if now is None else now)[1]


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


def main() -> None:  # pragma: no cover - process entry point
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    heartbeat = Path(os.environ.get("WATCHDOG_HEARTBEAT", "/guard/heartbeat.json"))
    max_age = float(os.environ.get("WATCHDOG_MAX_AGE", DEFAULT_MAX_AGE))
    listen = os.environ.get("WATCHDOG_LISTEN", DEFAULT_HEALTH_LISTEN)

    watchdog = Watchdog(heartbeat, max_age=max_age)
    server = make_health_server(watchdog.check, listen, watchdog.reason)

    def _stop(*_args: object) -> None:
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, _stop)
    log.info("watchdog reading %s, health on %s", heartbeat, listen)
    server.serve_forever()


if __name__ == "__main__":
    main()
