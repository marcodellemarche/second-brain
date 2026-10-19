"""Model gate: fail closed on health, forward only /v1, replace the key, stream the answer."""

from __future__ import annotations

import http.client
import http.server
import json
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from second_brain.gate.proxy import (
    MAX_BODY,
    ProxyConfig,
    ProxyConfigError,
    make_proxy_server,
    read_key,
)

REAL_KEY = "Bearer test-real-key-2f7c"
CLIENT_KEY = "Bearer client-key-should-be-dropped"


class FakeUpstream:
    """A local OpenAI-ish upstream that records what it received."""

    def __init__(self, handler=None) -> None:
        self.requests: list[dict[str, Any]] = []
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length)
                outer.requests.append(
                    {
                        "method": self.command,
                        "path": self.path,
                        "headers": {k.lower(): v for k, v in self.headers.items()},
                        "body": body,
                    }
                )
                if handler is not None:
                    handler(self)
                    return
                payload = json.dumps({"ok": True, "path": self.path}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def do_GET(self) -> None:  # noqa: N802
                self.do_POST()

            def log_message(self, *args: Any) -> None:
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.thread = threading.Thread(target=self.server.serve_forever, args=(0.02,), daemon=True)
        self.thread.start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


class FakeHealth:
    def __init__(self, status: int) -> None:
        self.status = status
        self.hits = 0

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_GET(self) -> None:  # noqa: N802
                outer.hits += 1
                body = b"ok" if outer.status == 200 else b"no heartbeat"
                self.send_response(outer.status)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args: Any) -> None:
                pass

        outer = self
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/ok"
        self.thread = threading.Thread(target=self.server.serve_forever, args=(0.02,), daemon=True)
        self.thread.start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


class Gate:
    def __init__(self, upstream: str, health_url: str) -> None:
        self.config = ProxyConfig(
            upstream=upstream,
            api_key=REAL_KEY,
            health_url=health_url,
            health_timeout=2.0,
        )
        self.server = make_proxy_server(self.config, "127.0.0.1:0")
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, args=(0.02,), daemon=True)
        self.thread.start()

    def connection(self) -> http.client.HTTPConnection:
        return http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)

    def post(
        self,
        path: str = "/v1/chat/completions",
        body: bytes = b'{"model": "x"}',
        headers: dict[str, str] | None = None,
    ) -> tuple[int, bytes]:
        sent = {"Content-Type": "application/json", **(headers or {})}
        conn = self.connection()
        try:
            conn.request("POST", path, body=body, headers=sent)
            response = conn.getresponse()
            return response.status, response.read()
        finally:
            conn.close()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


@pytest.fixture
def upstream() -> Iterator[FakeUpstream]:
    server = FakeUpstream()
    try:
        yield server
    finally:
        server.close()


@pytest.fixture
def health_ok() -> Iterator[FakeHealth]:
    server = FakeHealth(200)
    try:
        yield server
    finally:
        server.close()


@pytest.fixture
def gate(upstream: FakeUpstream, health_ok: FakeHealth) -> Iterator[Gate]:
    server = Gate(upstream.url, health_ok.url)
    try:
        yield server
    finally:
        server.close()


def test_health_failure_returns_503_without_calling_upstream(upstream: FakeUpstream) -> None:
    health = FakeHealth(503)
    server = Gate(upstream.url, health.url)
    try:
        status, body = server.post()
    finally:
        server.close()
        health.close()
    assert status == 503
    assert json.loads(body) == {"error": "guard not loaded"}
    assert upstream.requests == []


def test_unreachable_health_returns_503_without_calling_upstream(upstream: FakeUpstream) -> None:
    server = Gate(upstream.url, "http://127.0.0.1:1/ok")
    try:
        status, body = server.post()
    finally:
        server.close()
    assert status == 503
    assert json.loads(body) == {"error": "guard not loaded"}
    assert upstream.requests == []


def test_health_redirect_is_not_healthy(upstream: FakeUpstream) -> None:
    redirect = FakeHealth(302)
    server = Gate(upstream.url, redirect.url)
    try:
        status, body = server.post()
    finally:
        server.close()
        redirect.close()
    assert status == 503
    assert json.loads(body) == {"error": "guard not loaded"}
    assert upstream.requests == []


def test_forward_replaces_the_client_key(gate: Gate, upstream: FakeUpstream) -> None:
    status, body = gate.post(headers={"Authorization": CLIENT_KEY})
    assert status == 200
    assert json.loads(body)["ok"] is True
    assert len(upstream.requests) == 1
    sent = upstream.requests[0]
    assert sent["headers"]["authorization"] == REAL_KEY
    assert sent["path"] == "/v1/chat/completions"
    assert sent["body"] == b'{"model": "x"}'
    assert "client-key-should-be-dropped" not in str(sent["headers"])


def test_forward_without_client_key_still_adds_the_real_one(
    gate: Gate, upstream: FakeUpstream
) -> None:
    gate.post()
    assert upstream.requests[0]["headers"]["authorization"] == REAL_KEY


def test_chunked_request_body_is_forwarded(gate: Gate, upstream: FakeUpstream) -> None:
    conn = gate.connection()
    try:
        conn.putrequest("POST", "/v1/chat/completions")
        conn.putheader("Transfer-Encoding", "chunked")
        conn.endheaders()
        for part in (b'{"model":', b' "x"}'):
            conn.send(f"{len(part):x}\r\n".encode() + part + b"\r\n")
        conn.send(b"0\r\n\r\n")
        response = conn.getresponse()
        status = response.status
        response.read()
    finally:
        conn.close()
    assert status == 200
    assert upstream.requests[0]["body"] == b'{"model": "x"}'


def test_oversized_body_is_refused(gate: Gate, upstream: FakeUpstream) -> None:
    status, body = gate.post(headers={"Content-Length": str(MAX_BODY + 1)})
    assert status == 400
    assert json.loads(body) == {"error": "unsupported request body framing"}
    assert upstream.requests == []

def test_non_v1_path_is_404(gate: Gate, upstream: FakeUpstream) -> None:
    status, body = gate.post(path="/admin/containers")
    assert status == 404
    assert json.loads(body) == {"error": "not found"}
    assert upstream.requests == []


def test_non_v1_path_is_404_even_when_the_guard_is_down(upstream: FakeUpstream) -> None:
    health = FakeHealth(503)
    server = Gate(upstream.url, health.url)
    try:
        status, _ = server.post(path="/admin")
    finally:
        server.close()
        health.close()
    assert status == 404
    assert upstream.requests == []


def test_path_lookalike_is_404(gate: Gate, upstream: FakeUpstream) -> None:
    status, _ = gate.post(path="/v1secret")
    assert status == 404
    assert upstream.requests == []


@pytest.mark.parametrize(
    "path",
    [
        "/v1/../../admin/containers",
        "/v1/%2e%2e/admin",
        "/v1/%2E%2E%2Fadmin",
        "/v1/..\\admin",
        "/",
        "/admin",
    ],
)
def test_path_traversal_is_404(gate: Gate, upstream: FakeUpstream, path: str) -> None:
    status, _ = gate.post(path=path)
    assert status == 404
    assert upstream.requests == []


def test_query_string_is_forwarded(gate: Gate, upstream: FakeUpstream) -> None:
    status, _ = gate.post(path="/v1/models?limit=1")
    assert status == 200
    assert upstream.requests[0]["path"] == "/v1/models?limit=1"


def test_upstream_base_path_is_kept(health_ok: FakeHealth) -> None:
    upstream = FakeUpstream()
    config = ProxyConfig(
        upstream=f"{upstream.url}/api/",
        api_key=REAL_KEY,
        health_url=health_ok.url,
    )
    server = make_proxy_server(config, "127.0.0.1:0")
    thread = threading.Thread(target=server.serve_forever, args=(0.02,), daemon=True)
    thread.start()
    try:
        conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=10)
        conn.request("POST", "/v1/chat/completions", body=b"{}")
        response = conn.getresponse()
        response.read()
        conn.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        upstream.close()
    assert upstream.requests[0]["path"] == "/api/v1/chat/completions"


def test_streaming_chunks_arrive_in_order(health_ok: FakeHealth) -> None:
    written: list[bytes] = []
    chunks = [f"data: chunk-{index}\n\n".encode() for index in range(5)]

    def stream(handler: http.server.BaseHTTPRequestHandler) -> None:
        handler.send_response(200)
        handler.send_header("Content-Type", "text/event-stream")
        handler.send_header("Transfer-Encoding", "chunked")
        handler.end_headers()
        for chunk in chunks:
            handler.wfile.write(f"{len(chunk):x}\r\n".encode() + chunk + b"\r\n")
            handler.wfile.flush()
            written.append(chunk)
            time.sleep(0.05)
        handler.wfile.write(b"0\r\n\r\n")
        handler.wfile.flush()

    upstream = FakeUpstream(stream)
    gate = Gate(upstream.url, health_ok.url)
    received: list[bytes] = []
    seen_before_upstream_finished = None
    try:
        conn = gate.connection()
        conn.request("POST", "/v1/chat/completions", body=b"{}")
        response = conn.getresponse()
        assert response.status == 200
        while True:
            line = response.readline()
            if not line:
                break
            received.append(line)
            if line.startswith(b"data:") and seen_before_upstream_finished is None:
                seen_before_upstream_finished = len(written)
        conn.close()
    finally:
        gate.close()
        upstream.close()
    assert b"".join(received) == b"".join(chunks)
    data_lines = [line for line in received if line.startswith(b"data:")]
    assert data_lines == [chunk.removesuffix(b"\n") for chunk in chunks]
    # The first event reached the client while the upstream was still producing: no buffering.
    assert seen_before_upstream_finished is not None
    assert seen_before_upstream_finished < len(chunks)


def test_upstream_failure_is_502(health_ok: FakeHealth) -> None:
    server = Gate("http://127.0.0.1:1", health_ok.url)
    try:
        status, body = server.post()
    finally:
        server.close()
    assert status == 502
    assert json.loads(body) == {"error": "upstream unavailable"}


def test_config_from_env(tmp_path: Path, upstream: FakeUpstream, health_ok: FakeHealth) -> None:
    key_file = tmp_path / "key"
    key_file.write_text("sk-test-abc\n", encoding="utf-8")
    config = ProxyConfig.from_env(
        {
            "GATE_UPSTREAM": upstream.url,
            "GATE_API_KEY_FILE": str(key_file),
            "GATE_HEALTH_URL": health_ok.url,
        }
    )
    assert config.api_key == "Bearer sk-test-abc"
    assert config.listen == "0.0.0.0:8080"


def test_config_requires_every_variable() -> None:
    with pytest.raises(ProxyConfigError, match="GATE_UPSTREAM"):
        ProxyConfig.from_env({})


def test_config_rejects_bad_upstream(tmp_path: Path) -> None:
    key_file = tmp_path / "key"
    key_file.write_text("sk-test-abc", encoding="utf-8")
    with pytest.raises(ProxyConfigError):
        ProxyConfig.from_env(
            {
                "GATE_UPSTREAM": "ftp://model",
                "GATE_API_KEY_FILE": str(key_file),
                "GATE_HEALTH_URL": "http://127.0.0.1:1/ok",
            }
        )


def test_config_rejects_empty_key_file(tmp_path: Path) -> None:
    key_file = tmp_path / "key"
    key_file.write_text("  \n", encoding="utf-8")
    with pytest.raises(ProxyConfigError, match="empty"):
        ProxyConfig.from_env(
            {
                "GATE_UPSTREAM": "http://127.0.0.1:1",
                "GATE_API_KEY_FILE": str(key_file),
                "GATE_HEALTH_URL": "http://127.0.0.1:1/ok",
            }
        )


def test_read_key_keeps_an_explicit_scheme(tmp_path: Path) -> None:
    key_file = tmp_path / "key"
    key_file.write_text("bearer sk-test-abc", encoding="utf-8")
    assert read_key(key_file) == "bearer sk-test-abc"


def test_the_key_never_appears_in_errors(gate: Gate, upstream: FakeUpstream) -> None:
    status, body = gate.post(path="/nope")
    assert status == 404
    assert b"test-real-key" not in body
