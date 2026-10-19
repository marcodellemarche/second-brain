"""Tests for the signed webhook every sidecar uses. The POST is a fake, so nothing here is networked."""

from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from second_brain.sidecars import job_runner, webhook
from second_brain.sidecars.webhook import (
    DEFAULT_TIMEOUT,
    SIGNATURE_HEADER,
    TIMESTAMP_HEADER,
    WebhookError,
    event_body,
    read_secret,
    send_event,
    sender_for,
    sign_body,
)

BASE_URL = "http://hermes:8644/webhooks"
SECRET = "test-secret-not-a-real-one"


class FakePost:
    def __init__(self, status: int = 202, body: bytes = b'{"status": "accepted"}') -> None:
        self.status = status
        self.body = body
        self.calls: list[tuple[str, bytes, Mapping[str, str], float]] = []
        self.error: BaseException | None = None

    def __call__(
        self, url: str, body: bytes, headers: Mapping[str, str], timeout: float
    ) -> tuple[int, bytes]:
        self.calls.append((url, body, headers, timeout))
        if self.error is not None:
            raise self.error
        return self.status, self.body

    @property
    def last(self) -> tuple[str, bytes, Mapping[str, str], float]:
        return self.calls[-1]


@pytest.fixture
def secret_file(tmp_path: Path) -> Path:
    path = tmp_path / "webhook.secret"
    path.write_text(SECRET + "\n", encoding="utf-8")
    return path


@pytest.fixture
def post() -> FakePost:
    return FakePost()


def _v2(secret: str, body: bytes, timestamp: str) -> str:
    """The signature Hermes v0.22.2 expects, computed independently of the code under test."""
    return hmac.new(secret.encode(), timestamp.encode() + b"." + body, hashlib.sha256).hexdigest()


# --- signing -------------------------------------------------------------------------------


def test_sign_body_is_the_v2_header() -> None:
    body = b'{"kind": "note_changed"}'
    timestamp = "1700000000"
    assert sign_body(SECRET.encode(), body, timestamp) == _v2(SECRET, body, timestamp)
    assert not sign_body(SECRET.encode(), body, timestamp).startswith("sha256=")


def test_the_signature_commits_to_the_timestamp_and_the_body() -> None:
    body = b'{"kind": "note_changed"}'
    assert sign_body(b"one", body, "1700000000") != sign_body(b"one", body, "1700000001")
    assert sign_body(b"one", body, "1") != sign_body(b"two", body, "1")
    assert sign_body(b"one", body, "1") != sign_body(b"one", body + b" ", "1")


def test_the_job_runner_uses_the_same_signing_function() -> None:
    """One implementation, imported by the runner: the two sidecars cannot drift apart."""
    assert job_runner.sign_body is webhook.sign_body
    assert job_runner.SIGNATURE_HEADER == webhook.SIGNATURE_HEADER
    assert job_runner.TIMESTAMP_HEADER == webhook.TIMESTAMP_HEADER


def test_event_body_is_compact_and_keeps_non_ascii_readable() -> None:
    body = event_body({"kind": "conflict", "path": "idee/riunione à.md"})
    assert b'": ' not in body and b", " not in body
    assert "à" in body.decode("utf-8")
    assert json.loads(body) == {"kind": "conflict", "path": "idee/riunione à.md"}


def test_read_secret_strips_the_trailing_newline(secret_file: Path) -> None:
    assert read_secret(secret_file) == SECRET.encode()


def test_read_secret_refuses_a_missing_file(tmp_path: Path) -> None:
    with pytest.raises(WebhookError):
        read_secret(tmp_path / "missing.secret")


def test_read_secret_refuses_an_empty_file(tmp_path: Path) -> None:
    path = tmp_path / "empty.secret"
    path.write_text(" \n", encoding="utf-8")
    with pytest.raises(WebhookError):
        read_secret(path)


# --- sending -------------------------------------------------------------------------------


def test_send_event_posts_to_the_route_with_the_v2_signature(secret_file: Path, post: FakePost) -> None:
    payload = {"kind": "note_changed", "path": "topics/note.md"}
    status = send_event(BASE_URL, "vault-event", payload, secret_file, post=post)
    assert status == 202
    url, body, headers, _ = post.last
    assert url == f"{BASE_URL}/vault-event"
    assert headers[SIGNATURE_HEADER] == _v2(SECRET, body, headers[TIMESTAMP_HEADER])
    assert headers[TIMESTAMP_HEADER].isdigit()
    assert headers["Content-Type"] == "application/json"
    assert json.loads(body) == payload


def test_send_event_never_sends_the_legacy_header(secret_file: Path, post: FakePost) -> None:
    send_event(BASE_URL, "vault-event", {"kind": "note_created", "path": "a.md"}, secret_file, post=post)
    assert "X-Hub-Signature-256" not in post.last[2]


def test_send_event_returns_the_status_of_a_refusal(secret_file: Path, post: FakePost) -> None:
    post.status = 401
    assert (
        send_event(BASE_URL, "vault-event", {"kind": "conflict", "path": "a.md"}, secret_file, post=post)
        == 401
    )


def test_send_event_reads_the_secret_at_every_send(secret_file: Path, post: FakePost) -> None:
    send_event(BASE_URL, "vault-event", {"kind": "note_created", "path": "a.md"}, secret_file, post=post)
    rotated = "rotated-secret-not-a-real-one"
    secret_file.write_text(rotated, encoding="utf-8")
    send_event(BASE_URL, "vault-event", {"kind": "note_created", "path": "b.md"}, secret_file, post=post)
    _, body, headers, _ = post.last
    assert headers[SIGNATURE_HEADER] == _v2(rotated, body, headers[TIMESTAMP_HEADER])


def test_send_event_strips_the_trailing_slash_of_the_base_url(secret_file: Path, post: FakePost) -> None:
    send_event(
        BASE_URL + "//", "/vault-event/", {"kind": "note_created", "path": "a.md"}, secret_file, post=post
    )
    assert post.last[0] == f"{BASE_URL}/vault-event"


def test_send_event_uses_the_default_timeout(secret_file: Path, post: FakePost) -> None:
    send_event(BASE_URL, "vault-event", {"kind": "note_created", "path": "a.md"}, secret_file, post=post)
    assert post.last[3] == DEFAULT_TIMEOUT


def test_send_event_refuses_an_empty_base_url_or_route(secret_file: Path, post: FakePost) -> None:
    with pytest.raises(ValueError):
        send_event("  ", "vault-event", {}, secret_file, post=post)
    with pytest.raises(ValueError):
        send_event(BASE_URL, "  ", {}, secret_file, post=post)


def test_a_sender_is_bound_to_one_route(secret_file: Path, post: FakePost) -> None:
    send = sender_for(BASE_URL, "vault-event", secret_file, post)
    assert send({"kind": "note_created", "path": "a.md"}) == 202
    assert post.last[0] == f"{BASE_URL}/vault-event"


def test_an_unreachable_hermes_raises_webhook_error(secret_file: Path, post: FakePost) -> None:
    post.error = webhook.WebhookError("connection refused")
    with pytest.raises(WebhookError):
        send_event(BASE_URL, "vault-event", {"kind": "note_created", "path": "a.md"}, secret_file, post=post)


# --- the real HTTP path ---------------------------------------------------------------------


def test_send_event_signs_a_real_post(secret_file: Path) -> None:
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    received: dict[str, Any] = {}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            length = int(self.headers["Content-Length"])
            received["path"] = self.path
            received["body"] = self.rfile.read(length)
            received["headers"] = dict(self.headers)
            self.send_response(202)
            self.end_headers()
            self.wfile.write(b'{"status": "accepted"}')

        def log_message(self, *args: Any) -> None:
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{server.server_port}/webhooks"
        status = send_event(
            base, "vault-event", {"kind": "note_changed", "path": "topics/note.md"}, secret_file
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    assert status == 202
    assert received["path"] == "/webhooks/vault-event"
    headers = received["headers"]
    assert headers[SIGNATURE_HEADER] == _v2(SECRET, received["body"], headers[TIMESTAMP_HEADER])
    assert json.loads(received["body"]) == {"kind": "note_changed", "path": "topics/note.md"}
