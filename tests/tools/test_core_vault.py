"""Tests for the core vault tools: registration, and the log the digest reads."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from pydantic import ValidationError

from second_brain.core.types import Autonomy, Effect
from second_brain.tools.core_vault import LogParams, vault_tools
from second_brain.tools.registry import Tool
from second_brain.tools.vault import Provenance, Vault, VaultConfig


def _provenance() -> Provenance:
    return Provenance(
        requested_by="job:nightly",
        reason="test write",
        source_integrity="trusted",
        session="session-1",
    )


@pytest.fixture
def root(tmp_path: Path) -> Path:
    repo = tmp_path / "vault"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "tester")
    _git(repo, "config", "user.email", "tester@localhost")
    _git(repo, "commit", "-q", "--allow-empty", "-m", "init")
    return repo


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, check=True)
    return result.stdout.strip()


def _tool(root: Path, name: str) -> Tool:
    return {tool.spec.name: tool for tool in vault_tools(Vault(VaultConfig(root=root)))}[name]


def test_the_log_tool_is_a_private_read_with_the_documented_params(root: Path) -> None:
    spec = _tool(root, "vault_log").spec

    assert spec.effect is Effect.READ
    assert spec.autonomy is Autonomy.AUTO
    assert spec.taints_private is True
    assert spec.params is LogParams
    assert spec.description.startswith("List recent vault commits, newest first")


def test_the_log_tool_returns_the_entries(root: Path) -> None:
    Vault(VaultConfig(root=root)).write("topics/note.md", "hello\n", _provenance())

    result = _tool(root, "vault_log").handler(LogParams(limit=5), _provenance())

    assert result["ok"] is True
    assert result["commits"][0]["paths"] == ["topics/note.md"]
    assert result["commits"][0]["author"] == "brain"
    assert result["commits"][0]["trailers"]["Requested-By"] == "job:nightly"


def test_the_log_tool_turns_a_bad_since_into_a_refusal(root: Path) -> None:
    result = _tool(root, "vault_log").handler(LogParams(since="yesterday"), _provenance())

    assert result["ok"] is False
    assert result["code"] == "bad_since"
    assert "commits" not in result


@pytest.mark.parametrize("limit", [0, 201])
def test_the_log_tool_bounds_the_limit(root: Path, limit: int) -> None:
    with pytest.raises(ValidationError):
        LogParams(limit=limit)
