"""Tests for the MCP server: bearer auth, the guard object and the vault tools behind them."""

from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from second_brain.tools.core_vault import vault_tools
from second_brain.tools.registry import Registry
from second_brain.tools.server import BearerAuth, build_server, dispatch, tool_callable
from second_brain.tools.vault import Vault, VaultConfig

TOKEN = "test-token-9f3a"


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, check=True)
    return result.stdout.strip()


@pytest.fixture
def vault_root(tmp_path: Path) -> Path:
    root = tmp_path / "vault"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.name", "tester")
    _git(root, "config", "user.email", "tester@localhost")
    _git(root, "commit", "-q", "--allow-empty", "-m", "init")
    return root


@pytest.fixture
def registry(vault_root: Path) -> Registry:
    vault = Vault(VaultConfig(root=vault_root))
    registry = Registry()
    for tool in vault_tools(vault):
        registry.add(tool)
    return registry


def _guard(**overrides: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "requested_by": "user-1",
        "session": "session-1",
        "source_integrity": "trusted",
        "approval": "none",
    }
    values.update(overrides)
    return values


def _call(registry: Registry, name: str, authorization: str | None, **arguments: Any) -> dict[str, Any]:
    return dispatch(registry, TOKEN, authorization, name, arguments)


def _write(registry: Registry, **overrides: Any) -> dict[str, Any]:
    arguments: dict[str, Any] = {
        "path": "inbox/note.md",
        "content": "# note\n",
        "reason": "capture a thought",
        "guard": _guard(),
    }
    arguments.update(overrides)
    return dispatch(registry, TOKEN, f"Bearer {TOKEN}", "vault_write", arguments)


def test_dispatch_refuses_without_token(registry: Registry, vault_root: Path) -> None:
    result = _write(registry, guard=_guard())
    assert result["ok"] is True

    result = _call(registry, "vault_write", None, path="inbox/a.md", content="x", reason="r")
    assert result["ok"] is False
    assert result["code"] == "unauthorized"
    assert not (vault_root / "inbox" / "a.md").exists()


def test_dispatch_refuses_a_wrong_token(registry: Registry) -> None:
    result = _call(registry, "vault_write", "Bearer wrong", path="a.md", content="x", reason="r")
    assert result["ok"] is False
    assert result["code"] == "unauthorized"


def test_dispatch_refuses_a_missing_guard(registry: Registry, vault_root: Path) -> None:
    result = _call(registry, "vault_write", f"Bearer {TOKEN}", path="inbox/a.md", content="x", reason="r")
    assert result["ok"] is False
    assert result["code"] == "no_guard"
    assert not (vault_root / "inbox" / "a.md").exists()


@pytest.mark.parametrize(
    "guard",
    [
        "not an object",
        {},
        {"requested_by": "user-1", "session": "session-1"},
        {"requested_by": "", "session": "session-1", "source_integrity": "trusted"},
        {"requested_by": "user-1", "session": "session-1", "source_integrity": "maybe"},
        {"requested_by": "user-1", "session": "session-1", "source_integrity": "trusted", "approval": 7},
    ],
)
def test_dispatch_refuses_a_malformed_guard(registry: Registry, vault_root: Path, guard: Any) -> None:
    result = _call(
        registry, "vault_write", f"Bearer {TOKEN}", path="inbox/a.md", content="x", reason="r", guard=guard
    )
    assert result["ok"] is False
    assert result["code"] == "no_guard"
    assert not (vault_root / "inbox" / "a.md").exists()


def test_dispatch_reports_an_unknown_tool(registry: Registry) -> None:
    result = _call(registry, "vault_nope", f"Bearer {TOKEN}", guard=_guard())
    assert result["ok"] is False
    assert result["code"] == "unknown_tool"


def test_vault_write_commit_trailers_come_from_the_guard(registry: Registry, vault_root: Path) -> None:
    result = _write(
        registry,
        reason="file the invoice",
        guard=_guard(
            requested_by="user-42",
            session="session-7",
            source_integrity="untrusted",
            approval="approved",
        ),
    )
    assert result["ok"] is True
    message = _git(vault_root, "log", "-1", "--format=%B")
    assert result["commit"] == _git(vault_root, "rev-parse", "HEAD")
    assert "Requested-By: user-42" in message
    assert "Reason: file the invoice" in message
    assert "Source-Integrity: untrusted" in message
    assert "Session: session-7" in message
    assert "Approval: approved" in message


def test_vault_errors_are_results(registry: Registry) -> None:
    result = _write(registry, path="../outside.md")
    assert result["ok"] is False
    assert result["code"] == "outside_vault"

    _write(registry)
    result = _write(registry, content="# rewritten\n")
    assert result["ok"] is False
    assert result["code"] == "quiet_window"


def test_vault_read_returns_content_and_sha(registry: Registry) -> None:
    written = _write(registry)
    result = _call(registry, "vault_read", f"Bearer {TOKEN}", path="inbox/note.md", guard=_guard())
    assert result["ok"] is True
    assert result["content"] == "# note\n"
    assert len(result["sha256"]) == 64
    assert written["ok"] is True


def test_server_exposes_every_tool_with_a_guard_argument(registry: Registry) -> None:
    server = build_server(registry, TOKEN)
    tools = {tool.name: tool for tool in asyncio.run(server.list_tools())}
    assert set(tools) == {"vault_read", "vault_write", "vault_move", "vault_delete", "vault_undo"}
    schema = tools["vault_write"].inputSchema
    assert schema["required"] == ["path", "content", "reason"]
    assert set(schema["properties"]) == {"path", "content", "reason", "expected_sha256", "guard"}
    assert schema["properties"]["guard"]["type"] == "object"


def _context(authorization: str | None) -> Any:
    """The slice of a FastMCP Context that the tool callable reads: the request headers."""
    headers = {} if authorization is None else {"authorization": authorization}
    return SimpleNamespace(request_context=SimpleNamespace(request=SimpleNamespace(headers=headers)))


def test_tool_call_without_guard_is_refused_through_the_server(registry: Registry, vault_root: Path) -> None:
    call = tool_callable(registry, TOKEN, "vault_write")
    result = call(ctx=_context(f"Bearer {TOKEN}"), path="inbox/a.md", content="x", reason="r")
    assert result["ok"] is False
    assert result["code"] == "no_guard"
    assert not (vault_root / "inbox" / "a.md").exists()


def test_tool_call_through_the_server_writes(registry: Registry, vault_root: Path) -> None:
    call = tool_callable(registry, TOKEN, "vault_write")
    result = call(
        ctx=_context(f"Bearer {TOKEN}"),
        path="inbox/a.md",
        content="x",
        reason="r",
        guard=_guard(requested_by="user-9"),
    )
    assert result["ok"] is True
    assert (vault_root / "inbox" / "a.md").read_text() == "x"
    assert "Requested-By: user-9" in _git(vault_root, "log", "-1", "--format=%B")


def test_tool_call_with_a_wrong_token_is_refused_through_the_server(registry: Registry) -> None:
    call = tool_callable(registry, TOKEN, "vault_write")
    result = call(ctx=_context("Bearer wrong"), path="inbox/a.md", content="x", reason="r", guard=_guard())
    assert result["ok"] is False
    assert result["code"] == "unauthorized"


def test_requests_without_a_bearer_token_get_401() -> None:
    async def app(scope: dict[str, Any], receive: Any, send: Any) -> None:
        raise AssertionError("the request must not reach the app")

    sent: list[dict[str, Any]] = []

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    async def receive() -> dict[str, Any]:  # pragma: no cover - never awaited
        return {"type": "http.request"}

    guarded = BearerAuth(app, TOKEN)
    asyncio.run(guarded({"type": "http", "headers": []}, receive, send))
    assert sent[0]["status"] == 401
    assert json.loads(sent[1]["body"])["code"] == "unauthorized"

    sent.clear()
    headers = [(b"authorization", f"Bearer {TOKEN}".encode())]
    with pytest.raises(AssertionError, match="must not reach the app"):
        asyncio.run(guarded({"type": "http", "headers": headers}, receive, send))
