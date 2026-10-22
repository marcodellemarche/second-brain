"""The HTTP layer an instance tool can share: one error type, one JSON call, injectable transport.

The transport is a parameter so tests never reach the network, and the proxy comes from the
environment like for every other tool. A refusal or a failure becomes a `ToolError` with a stable
`code`; nothing here decides whether the call was allowed, that is the guard's job.
"""

from __future__ import annotations

import base64
import json
import ssl
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from typing import Any, cast

from second_brain.tools.registry import ExtensionError

TIMEOUT = 20.0
MAX_DETAIL = 1200

# method, url, headers, body, timeout -> the response body
Fetch = Callable[[str, str, dict[str, str], bytes | None, float], bytes]


class ToolError(Exception):
    """A refused or failed remote call. `code` is stable and machine-readable.

    `status` is the HTTP status when the service answered and `detail` what it said (or why it
    could not be reached), so a tool can word its own message and keep the code.
    """

    def __init__(self, code: str, message: str, status: int | None = None, detail: str = "") -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.status = status
        self.detail = detail


def detail(raw: bytes) -> str:
    """A short, single-line excerpt of an error body: never the request, so never a token."""
    text = raw[:MAX_DETAIL].decode(errors="replace")
    try:
        parsed = json.loads(text)
    except ValueError:
        return " ".join(text.split())
    if isinstance(parsed, Mapping):
        reasons = _error_list(parsed.get("errors"))
        for key in ("message", "error_description", "error", "detail"):
            value = parsed.get(key)
            if isinstance(value, str) and value.strip():
                head = " ".join(value.split())
                return f"{head}: {reasons}" if reasons else head
        if reasons:
            return reasons
    return " ".join(text.split())


def _error_list(errors: Any) -> str:
    """Some APIs put the real reason of a 422 in an `errors` list, next to a generic message."""
    if isinstance(errors, str):
        return " ".join(errors.split())
    if not isinstance(errors, list):
        return ""
    parts = []
    for item in errors[:3]:
        if isinstance(item, Mapping):
            text = item.get("message") or " ".join(str(item.get(k)) for k in ("field", "code") if item.get(k))
        else:
            text = str(item)
        if text:
            parts.append(" ".join(str(text).split()))
    return "; ".join(parts)


def status_error(exc: urllib.error.HTTPError, service: str) -> ToolError:
    """The `ToolError` for an HTTP error status, with an excerpt of what the service said."""
    said = detail(exc.read() or b"")
    if exc.code in (401, 403):
        message = f"{service} refused the token ({exc.code}): check the secret of this tool. {said}"
        return ToolError("unauthorized", message.strip(), exc.code, said)
    if exc.code == 404:
        return ToolError("not_found", f"{service} has nothing at that path (404). {said}".strip(), 404, said)
    if exc.code == 422:
        return ToolError("bad_params", f"{service} rejected the request (422). {said}".strip(), 422, said)
    return ToolError("backend_error", f"{service} answered {exc.code}. {said}".strip(), exc.code, said)


def http_fetch(
    method: str,
    url: str,
    headers: dict[str, str],
    data: bytes | None,
    timeout: float,
    *,
    context: ssl.SSLContext | None = None,
    service: str | None = None,
) -> bytes:
    """The real transport. `context` is for a service with its own CA, such as a cluster API."""
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    name = service or urllib.parse.urlsplit(url).hostname or "the service"
    try:
        if context is None:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return cast(bytes, response.read())
        opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=context))
        with opener.open(request, timeout=timeout) as response:
            return cast(bytes, response.read())
    except urllib.error.HTTPError as exc:
        raise status_error(exc, name) from None
    except OSError as exc:  # URLError, a timeout, a refused connection
        reason = getattr(exc, "reason", exc)
        raise ToolError("unreachable", f"{name} is not reachable: {reason}", detail=str(reason)) from None


def call(
    fetch: Fetch,
    method: str,
    url: str,
    *,
    headers: Mapping[str, str] | None = None,
    body: Any | None = None,
    timeout: float = TIMEOUT,
) -> Any:
    """One JSON call. An empty body answers None, anything that is not JSON is a `bad_response`."""
    sent = dict(headers or {})
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        sent.setdefault("Content-Type", "application/json")
    sent.setdefault("Accept", "application/json")
    raw = fetch(method, url, sent, data, timeout)
    if not raw:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        raise ToolError(
            "bad_response", f"the answer of {urllib.parse.urlsplit(url).hostname} is not JSON"
        ) from None


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def basic(user: str, token: str) -> dict[str, str]:
    joined = f"{user}:{token}".encode()
    return {"Authorization": "Basic " + base64.b64encode(joined).decode("ascii")}


def quote(value: str) -> str:
    """One path segment or query value, encoded so a name with a slash cannot change the path."""
    return urllib.parse.quote(value, safe="")


def error(code: str, message: str) -> dict[str, Any]:
    return {"error": message, "code": code}


def guarded(run: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    """Run a call, turning a refused or failed one into a result instead of an exception.

    A missing secret file is a result like any other: the brain tells the owner which file is
    empty instead of failing the turn.
    """
    try:
        return run()
    except ToolError as exc:
        return error(exc.code, exc.message)
    except ExtensionError as exc:
        return error("missing_secret", str(exc))
