"""Reverse proxy in front of an OpenAI-compatible endpoint.

The model key lives here, never in Hermes. Every request first asks the watchdog's `/ok`; if that
is not a 200, the answer is 503 and the upstream is never contacted, so a Hermes running without
brain_guard cannot reach the model at all. Only `/v1/...` paths are forwarded.

Responses are relayed chunk by chunk with connection-close framing, because the model answers with
SSE streams that must reach the client as they are produced.
"""

from __future__ import annotations

import http.client
import http.server
import json
import logging
import os
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from second_brain.gate import parse_listen

log = logging.getLogger(__name__)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


_NO_REDIRECT = urllib.request.build_opener(_NoRedirect)

DEFAULT_LISTEN = "0.0.0.0:8080"
DEFAULT_HEALTH_TIMEOUT = 2.0
DEFAULT_UPSTREAM_TIMEOUT = 300.0
ALLOWED_PREFIX = "/v1/"
CHUNK = 64 * 1024
MAX_BODY = 32 * 1024 * 1024

_HOP_BY_HOP = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "proxy-connection",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
    }
)


class ProxyConfigError(RuntimeError):
    """The gate cannot start without a usable configuration."""


@dataclass(frozen=True)
class ProxyConfig:
    upstream: str
    api_key: str
    health_url: str
    listen: str = DEFAULT_LISTEN
    health_timeout: float = DEFAULT_HEALTH_TIMEOUT
    upstream_timeout: float = DEFAULT_UPSTREAM_TIMEOUT

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> ProxyConfig:
        env = os.environ if environ is None else environ
        upstream = env.get("GATE_UPSTREAM", "").strip()
        key_file = env.get("GATE_API_KEY_FILE", "").strip()
        health_url = env.get("GATE_HEALTH_URL", "").strip()
        listen = env.get("GATE_LISTEN", "").strip() or DEFAULT_LISTEN
        missing = [
            name
            for name, value in (
                ("GATE_UPSTREAM", upstream),
                ("GATE_API_KEY_FILE", key_file),
                ("GATE_HEALTH_URL", health_url),
            )
            if not value
        ]
        if missing:
            raise ProxyConfigError(f"missing configuration: {', '.join(missing)}")
        parse_listen(listen)
        _split_upstream(upstream)
        return cls(
            upstream=upstream,
            api_key=read_key(key_file),
            health_url=health_url,
            listen=listen,
        )


def read_key(path: str | Path) -> str:
    """Read the model key once at start. The value is never logged."""
    try:
        raw = Path(path).read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise ProxyConfigError(f"cannot read the key file: {exc.strerror}") from exc
    if not raw:
        raise ProxyConfigError("the key file is empty")
    return raw if raw.lower().startswith("bearer ") else f"Bearer {raw}"


def _split_upstream(upstream: str) -> tuple[str, str, int, str]:
    parts = urllib.parse.urlsplit(upstream)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ProxyConfigError(f"GATE_UPSTREAM must be an http(s) URL, got {upstream!r}")
    port = parts.port or (443 if parts.scheme == "https" else 80)
    return parts.scheme, parts.hostname, port, parts.path.rstrip("/")


def healthy(url: str, timeout: float = DEFAULT_HEALTH_TIMEOUT) -> bool:
    """Ask the watchdog. Any doubt counts as unhealthy, including a redirect."""
    try:
        with _NO_REDIRECT.open(url, timeout=timeout) as response:  # noqa: S310 - configured internal URL
            return response.status == 200
    except (urllib.error.URLError, OSError, ValueError):
        return False


def _safe_path(path: str) -> bool:
    """Only plain `/v1/...` paths, so nothing can climb out of the API prefix upstream."""
    raw = path.split("?", 1)[0].split("#", 1)[0]
    if not raw.startswith(ALLOWED_PREFIX):
        return False
    if "\\" in raw or ".." in raw:
        return False
    lowered = raw.lower()
    return not any(encoded in lowered for encoded in ("%2e", "%2f", "%5c"))


class _GateHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "second-brain-gate"

    config: ProxyConfig

    def do_GET(self) -> None:  # noqa: N802 - http.server naming
        self._forward("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._forward("POST")

    def do_PUT(self) -> None:  # noqa: N802
        self._forward("PUT")

    def do_PATCH(self) -> None:  # noqa: N802
        self._forward("PATCH")

    def do_DELETE(self) -> None:  # noqa: N802
        self._forward("DELETE")

    def do_OPTIONS(self) -> None:  # noqa: N802
        self._forward("OPTIONS")

    def do_HEAD(self) -> None:  # noqa: N802
        self._forward("HEAD")

    def _error(self, status: int, message: str) -> None:
        body = json.dumps({"error": message}).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        if self.command != "HEAD":
            self.wfile.write(body)

    def _target_path(self) -> str | None:
        path = self.path
        if not _safe_path(path):
            return None
        _, _, _, base = _split_upstream(self.config.upstream)
        return f"{base}{path}" if base else path

    def _read_body(self) -> bytes | None:
        """None means the body framing is broken and the request cannot be forwarded."""
        if "chunked" in self.headers.get("Transfer-Encoding", "").lower():
            return self._read_chunked()
        length = self.headers.get("Content-Length")
        if not length:
            return b""
        try:
            size = int(length)
        except ValueError:
            return None
        if size < 0 or size > MAX_BODY:
            return None
        return self.rfile.read(size)

    def _read_chunked(self) -> bytes | None:
        body = bytearray()
        while True:
            line = self.rfile.readline(1024)
            try:
                size = int(line.split(b";", 1)[0].strip(), 16)
            except ValueError:
                return None
            if size < 0 or len(body) + size > MAX_BODY:
                return None
            if size == 0:
                while True:
                    trailer = self.rfile.readline(1024)
                    if trailer in (b"\r\n", b"\n", b""):
                        return bytes(body)
            chunk = self.rfile.read(size)
            if len(chunk) != size:
                return None
            body += chunk
            self.rfile.read(2)

    def _request_headers(self) -> dict[str, str]:
        headers: dict[str, str] = {}
        for name, value in self.headers.items():
            lowered = name.lower()
            if lowered in _HOP_BY_HOP or lowered in ("authorization", "host", "content-length"):
                continue
            headers[name] = value
        headers["Authorization"] = self.config.api_key
        headers["Accept-Encoding"] = "identity"
        return headers

    def _forward(self, method: str) -> None:
        target = self._target_path()
        if target is None:
            self._error(404, "not found")
            return

        if not healthy(self.config.health_url, self.config.health_timeout):
            log.warning("deny %s %s: guard not loaded", method, self.path)
            self._error(503, "guard not loaded")
            return

        body = self._read_body()
        if body is None:
            self._error(400, "unsupported request body framing")
            return

        scheme, host, port, _ = _split_upstream(self.config.upstream)
        connection_class = (
            http.client.HTTPSConnection if scheme == "https" else http.client.HTTPConnection
        )
        connection = connection_class(host, port, timeout=self.config.upstream_timeout)
        started = False
        try:
            connection.request(method, target, body=body or None, headers=self._request_headers())
            response = connection.getresponse()
            started = True
            self._relay(response, method)
        except (OSError, http.client.HTTPException) as exc:
            log.warning("upstream failed for %s %s: %s", method, self.path, type(exc).__name__)
            # Headers already went out: a second status line would corrupt the response.
            if not started:
                self._error(502, "upstream unavailable")
            else:
                self.close_connection = True
        finally:
            connection.close()

    def _relay(self, response: http.client.HTTPResponse, method: str) -> None:
        self.send_response(response.status, response.reason)
        for name, value in response.getheaders():
            if name.lower() in _HOP_BY_HOP or name.lower() in ("content-length", "date", "server"):
                continue
            self.send_header(name, value)
        # The body ends when the connection closes: the length is unknown while streaming.
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        if method == "HEAD":
            return
        try:
            while True:
                # read1 returns whatever arrived, so an SSE event leaves immediately instead of
                # waiting for a full buffer.
                chunk = response.read1(CHUNK)
                if not chunk:
                    break
                self.wfile.write(chunk)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            log.info("client went away during %s %s", method, self.path)

    def log_message(self, format: str, *args: Any) -> None:
        log.info("%s - %s", self.address_string(), format % args)


def make_proxy_server(config: ProxyConfig, listen: str | None = None) -> http.server.ThreadingHTTPServer:
    """Build (but do not start) the proxy. `listen` overrides the configured address."""
    host, port = parse_listen(listen or config.listen)
    handler = type("GateHandler", (_GateHandler,), {"config": config})
    server = http.server.ThreadingHTTPServer((host, port), handler)
    server.daemon_threads = True
    return server


def main() -> None:  # pragma: no cover - process entry point
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        config = ProxyConfig.from_env()
    except ProxyConfigError as exc:
        raise SystemExit(f"model gate: {exc}") from exc
    server = make_proxy_server(config)
    log.info(
        "model gate on %s forwarding %s, health %s",
        config.listen,
        urllib.parse.urlsplit(config.upstream).netloc,
        config.health_url,
    )
    server.serve_forever()


if __name__ == "__main__":
    main()
