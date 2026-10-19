"""One signed POST to a Hermes webhook route, shared by every sidecar.

Hermes v0.22.2 validates the replay-safe signature as `X-Webhook-Signature-V2`, the hex HMAC-SHA256
of `"<timestamp>.<body>"`, with the same timestamp in `X-Webhook-Timestamp` and at most five
minutes of skew (`gateway/platforms/webhook.py:800-808`). The body-only `X-Hub-Signature-256` is
the legacy path: it has no timestamp, so a captured request replays forever. Every sidecar signs
the same way, so this module holds the one implementation.

The secret is read at every send, so rotating it does not need a restart. A sidecar never retries
inside this module: the status goes back to the caller, which decides whether to try again.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

SIGNATURE_HEADER = "X-Webhook-Signature-V2"
TIMESTAMP_HEADER = "X-Webhook-Timestamp"
DEFAULT_TIMEOUT = 30.0
DEFAULT_USER_AGENT = "second-brain-sidecar"

# What one POST attempt answers: the HTTP status and the response body, for the error message.
HttpPost = Callable[[str, bytes, Mapping[str, str], float], tuple[int, bytes]]
# What a sidecar hands to a sender: the event body, nothing from the vault but a path.
EventPayload = Mapping[str, Any]
# A sender bound to one base URL, route and secret.
SendEvent = Callable[[EventPayload], int]


class WebhookError(RuntimeError):
    """The event cannot be sent: an unreadable secret, or a Hermes that does not answer."""


def sign_body(secret: bytes, body: bytes, timestamp: str) -> str:
    """The header value Hermes expects: the hex HMAC-SHA256 of `"<timestamp>.<body>"`."""
    signed = timestamp.encode("ascii") + b"." + body
    return hmac.new(secret, signed, hashlib.sha256).hexdigest()


def read_secret(secret_file: Path | str) -> bytes:
    """The HMAC secret, read at every send so a rotation does not need a restart."""
    path = Path(secret_file)
    try:
        secret = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise WebhookError(f"cannot read the HMAC secret: {exc}") from exc
    if not secret:
        raise WebhookError(f"the HMAC secret in {path} is empty")
    return secret.encode("utf-8")


def signed_headers(
    secret: bytes,
    body: bytes,
    timestamp: str,
    *,
    user_agent: str = DEFAULT_USER_AGENT,
) -> dict[str, str]:
    return {
        SIGNATURE_HEADER: sign_body(secret, body, timestamp),
        TIMESTAMP_HEADER: timestamp,
        "Content-Type": "application/json",
        "User-Agent": user_agent,
    }


def http_post(url: str, body: bytes, headers: Mapping[str, str], timeout: float) -> tuple[int, bytes]:
    """POST one signed body. An HTTP error status is an answer, not an exception."""
    request = urllib.request.Request(url, data=body, headers=dict(headers), method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()
    except (OSError, ValueError) as exc:
        raise WebhookError(f"{url} unreachable: {exc}") from exc


def event_body(payload: EventPayload) -> bytes:
    """The JSON body of one event. Compact, and non-ASCII text stays readable."""
    return json.dumps(dict(payload), ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def send_event(
    base_url: str,
    route: str,
    payload: EventPayload,
    secret_file: Path | str,
    post: HttpPost | None = None,
    *,
    timeout: float = DEFAULT_TIMEOUT,
    clock: Callable[[], float] = time.time,
    user_agent: str = DEFAULT_USER_AGENT,
) -> int:
    """Sign one event and POST it to `base_url/route`. Returns the HTTP status it answered with."""
    base = base_url.strip().rstrip("/")
    if not base:
        raise ValueError("base_url must not be empty")
    name = route.strip().strip("/")
    if not name:
        raise ValueError("route must not be empty")
    body = event_body(payload)
    timestamp = str(int(clock()))
    headers = signed_headers(read_secret(secret_file), body, timestamp, user_agent=user_agent)
    status, _ = (post or http_post)(f"{base}/{name}", body, headers, timeout)
    return status


def sender_for(
    base_url: str,
    route: str,
    secret_file: Path | str,
    post: HttpPost | None = None,
    *,
    timeout: float = DEFAULT_TIMEOUT,
    user_agent: str = DEFAULT_USER_AGENT,
) -> SendEvent:
    """A `send(payload) -> status` bound to one route, which is what the sidecars take."""

    def send(payload: EventPayload) -> int:
        return send_event(
            base_url,
            route,
            payload,
            secret_file,
            post,
            timeout=timeout,
            user_agent=user_agent,
        )

    return send


__all__ = [
    "DEFAULT_TIMEOUT",
    "SIGNATURE_HEADER",
    "TIMESTAMP_HEADER",
    "EventPayload",
    "HttpPost",
    "SendEvent",
    "WebhookError",
    "event_body",
    "http_post",
    "read_secret",
    "send_event",
    "sender_for",
    "sign_body",
    "signed_headers",
]
