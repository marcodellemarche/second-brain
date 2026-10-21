"""Tests for the MCP server: bearer auth, the guard object and the vault tools behind them."""

from __future__ import annotations

import asyncio
import json
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from second_brain.tools.core_vault import vault_tools
from second_brain.tools.outbox import ImportResult
from second_brain.tools.registry import Registry
from second_brain.tools.remote import RemoteError, SyncResult
from second_brain.tools.server import (
    BearerAuth,
    build_server,
    commit_human_changes_once,
    dispatch,
    import_outbox_once,
    start_human_commit_timer,
    start_outbox_timer,
    start_remote_timer,
    sync_remote_once,
    tool_callable,
)
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
    assert set(tools) == {
        "vault_read",
        "vault_log",
        "vault_write",
        "vault_move",
        "vault_delete",
        "vault_undo",
    }
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


def test_main_reads_only_the_tools_config(
    vault_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import yaml

    from second_brain.tools import config as config_module
    from second_brain.tools import registry as registry_module
    from second_brain.tools import server as server_module

    token_file = tmp_path / "token"
    token_file.write_text(TOKEN, encoding="utf-8")
    config_file = tmp_path / "tools.yaml"
    config_file.write_text(
        yaml.safe_dump(
            {
                "vault": {"root": str(vault_root)},
                "token_file": str(token_file),
                "listen": "127.0.0.1:9999",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv(config_module.CONFIG_ENV, str(config_file))
    monkeypatch.setattr(registry_module, "entry_points", lambda group: [])

    served: dict[str, Any] = {}

    def fake_run(app: Any, **kwargs: Any) -> None:
        served["app"] = app
        served.update(kwargs)

    monkeypatch.setattr(server_module.uvicorn, "run", fake_run)
    server_module.main()

    assert served["host"] == "127.0.0.1"
    assert served["port"] == 9999
    assert served["app"] is not None


def test_the_timer_commits_the_changes_made_by_hand(vault_root: Path) -> None:
    vault = Vault(VaultConfig(root=vault_root))
    (vault_root / "note.md").write_text("edited by hand\n")

    thread = start_human_commit_timer(vault, 0.01)
    deadline = time.monotonic() + 5
    while _git(vault_root, "status", "--porcelain") and time.monotonic() < deadline:
        time.sleep(0.01)

    assert thread.daemon is True
    assert _git(vault_root, "log", "-1", "--format=%s") == "human: 1 files"
    assert _git(vault_root, "status", "--porcelain") == ""


def test_one_tick_of_the_timer_commits_pending_changes(vault_root: Path) -> None:
    vault = Vault(VaultConfig(root=vault_root))
    (vault_root / "note.md").write_text("edited by hand\n")

    sha = commit_human_changes_once(vault)

    assert sha == _git(vault_root, "rev-parse", "HEAD")
    assert _git(vault_root, "log", "-1", "--format=%s") == "human: 1 files"
    assert commit_human_changes_once(vault) is None


class _FakeImporter:
    def __init__(self, results: list[ImportResult] | None = None, error: Exception | None = None) -> None:
        self._results = results or []
        self._error = error
        self.sweeps = 0

    def sweep(self) -> list[ImportResult]:
        self.sweeps += 1
        if self._error is not None:
            raise self._error
        return self._results


def test_import_outbox_once_counts_the_imported_results() -> None:
    importer = _FakeImporter(
        [
            ImportResult("a.json", "job-1", "raw/web/a.md", None),
            ImportResult("b.json", "job-2", None, "the job is unknown"),
            ImportResult("c.json", "job-3", "raw/web/c.md", None),
        ]
    )

    assert import_outbox_once(importer) == 2  # type: ignore[arg-type]
    assert importer.sweeps == 1


def test_import_outbox_once_never_raises() -> None:
    importer = _FakeImporter(error=RuntimeError("the outbox is on fire"))

    assert import_outbox_once(importer) == 0  # type: ignore[arg-type]


def test_the_outbox_timer_is_a_daemon_thread_with_its_own_name() -> None:
    thread = start_outbox_timer(_FakeImporter(), 3600.0)  # type: ignore[arg-type]

    assert thread.daemon is True
    assert thread.name == "brain_tools-outbox-import"
    assert thread.is_alive() is True


class _FakeRemote:
    def __init__(self, result: Any = None, error: Exception | None = None) -> None:
        self._result = result or SyncResult(pushed=None, pulled=[], conflicts=[], refused=[])
        self._error = error
        self.syncs = 0

    def sync_once(self) -> SyncResult:
        self.syncs += 1
        if self._error is not None:
            raise self._error
        return self._result


def test_sync_remote_once_reports_what_it_pushed() -> None:
    remote = _FakeRemote(
        SyncResult(
            pushed="a" * 40,
            pulled=["notes/one.md"],
            conflicts=["notes/two.md"],
            refused=["notes/three.md: not_found"],
        )
    )

    assert sync_remote_once(remote) == "a" * 40  # type: ignore[arg-type]
    assert remote.syncs == 1


def test_sync_remote_once_survives_a_remote_error() -> None:
    remote = _FakeRemote(error=RemoteError("git push failed: rejected"))

    assert sync_remote_once(remote) is None  # type: ignore[arg-type]
    assert remote.syncs == 1


def test_sync_remote_once_survives_any_other_error() -> None:
    remote = _FakeRemote(error=RuntimeError("the clone is on fire"))

    assert sync_remote_once(remote) is None  # type: ignore[arg-type]
    assert remote.syncs == 1


def test_the_remote_timer_calls_one_sync_per_tick() -> None:
    remote = _FakeRemote()

    thread = start_remote_timer(remote, 0.01)  # type: ignore[arg-type]
    deadline = time.monotonic() + 5
    while remote.syncs < 2 and time.monotonic() < deadline:
        time.sleep(0.01)

    assert thread.daemon is True
    assert thread.name == "brain_tools-remote-sync"
    assert remote.syncs >= 2


def test_the_remote_timer_keeps_going_after_a_remote_error() -> None:
    remote = _FakeRemote(error=RemoteError("the token file is empty"))

    thread = start_remote_timer(remote, 0.01)  # type: ignore[arg-type]
    deadline = time.monotonic() + 5
    while remote.syncs < 2 and time.monotonic() < deadline:
        time.sleep(0.01)

    assert thread.is_alive() is True
    assert remote.syncs >= 2
