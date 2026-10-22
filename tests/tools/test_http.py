"""The HTTP layer: the error mapping and the JSON call, with a fake transport."""

from __future__ import annotations

import io
import json
import ssl
import urllib.error
import urllib.request

import pytest

from second_brain.tools import http


def _http_error(code: int, body: bytes) -> urllib.error.HTTPError:
    return urllib.error.HTTPError("https://api.example.test/x", code, "nope", {}, io.BytesIO(body))


def test_call_returns_the_parsed_body() -> None:
    seen: dict[str, object] = {}

    def fetch(method, url, headers, data, timeout):
        seen.update(method=method, url=url, headers=headers, data=data)
        return b'{"ok": true}'

    assert http.call(fetch, "POST", "https://api.example.test/x", body={"a": 1}) == {"ok": True}
    assert seen["method"] == "POST"
    assert json.loads(seen["data"]) == {"a": 1}  # type: ignore[arg-type]
    headers = seen["headers"]
    assert isinstance(headers, dict)
    assert headers["Content-Type"] == "application/json"


def test_call_answers_none_for_an_empty_body() -> None:
    assert http.call(lambda *a: b"", "GET", "https://api.example.test/x") is None


def test_call_refuses_a_body_that_is_not_json() -> None:
    with pytest.raises(http.ToolError) as exc:
        http.call(lambda *a: b"<html>nope</html>", "GET", "https://api.example.test/x")
    assert exc.value.code == "bad_response"


@pytest.mark.parametrize(
    ("status", "code"),
    [
        (401, "unauthorized"),
        (403, "unauthorized"),
        (404, "not_found"),
        (422, "bad_params"),
        (500, "backend_error"),
    ],
)
def test_http_errors_become_stable_codes(status: int, code: str) -> None:
    error = http.status_error(_http_error(status, b'{"message": "details here"}'), "the service")
    assert error.code == code
    assert "details here" in error.message


def test_http_fetch_maps_a_refused_connection(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*args, **kwargs):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(urllib.request, "urlopen", boom)
    with pytest.raises(http.ToolError) as exc:
        http.http_fetch("GET", "https://api.example.test/x", {}, None, 1.0)
    assert exc.value.code == "unreachable"
    assert "api.example.test" in exc.value.message


def test_http_fetch_maps_an_http_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*args, **kwargs):
        raise _http_error(401, b'{"message": "bad credentials"}')

    monkeypatch.setattr(urllib.request, "urlopen", boom)
    with pytest.raises(http.ToolError) as exc:
        http.http_fetch("GET", "https://api.example.test/x", {}, None, 1.0)
    assert exc.value.code == "unauthorized"


def test_an_error_body_is_clipped_and_never_carries_the_request() -> None:
    error = http.status_error(_http_error(500, b"x" * 5000), "the service")
    assert len(error.message) < http.MAX_DETAIL + 200


def test_basic_and_bearer_headers() -> None:
    assert http.bearer("t")["Authorization"] == "Bearer t"
    assert http.basic("u", "t")["Authorization"].startswith("Basic ")


def test_quote_encodes_a_slash_so_a_name_cannot_change_the_path() -> None:
    assert http.quote("a/b") == "a%2Fb"


def test_guarded_turns_a_tool_error_into_a_result() -> None:
    def boom() -> dict[str, object]:
        raise http.ToolError("not_found", "nothing there")

    assert http.guarded(boom) == {"error": "nothing there", "code": "not_found"}


def test_a_422_with_an_errors_list_carries_the_real_reason() -> None:
    body = json.dumps(
        {
            "message": "Validation Failed",
            "errors": [
                {
                    "resource": "PullRequest",
                    "code": "custom",
                    "message": "No commits between develop and fix/login-redirect",
                }
            ],
        }
    ).encode()
    assert http.detail(body) == "Validation Failed: No commits between develop and fix/login-redirect"


def test_an_error_list_without_messages_names_field_and_code() -> None:
    body = json.dumps(
        {"message": "Validation Failed", "errors": [{"field": "head", "code": "invalid"}]}
    ).encode()
    assert http.detail(body) == "Validation Failed: head invalid"


def test_an_http_error_keeps_its_status() -> None:
    error = http.status_error(_http_error(409, b"{}"), "the service")
    assert error.code == "backend_error"
    assert error.status == 409
    assert error.detail == "{}"


def test_http_fetch_with_a_context_uses_its_own_opener(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*args, **kwargs):
        raise AssertionError("the default opener must not be used")

    class Opener:
        def open(self, request, timeout):
            raise urllib.error.URLError("refused")

    monkeypatch.setattr(urllib.request, "urlopen", boom)
    monkeypatch.setattr(urllib.request, "build_opener", lambda *handlers: Opener())
    with pytest.raises(http.ToolError) as exc:
        http.http_fetch(
            "GET",
            "https://api.example.test/x",
            {},
            None,
            1.0,
            context=ssl.create_default_context(),
            service="the API",
        )
    assert exc.value.message.startswith("the API is not reachable")
