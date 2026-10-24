"""A prompt-injected model names Hermes' own state in `archive_store`, hoping the guard copies it out.

The guard copies only a bare file name found inside the cache folders, never through a link that
leaves them, so Hermes' session database and secrets stay where they are.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from second_brain.guard import plugin
from second_brain.guard.config import GuardConfig

CONFIG: dict[str, Any] = {
    "vault_root": "/vault",
    "mcp_tools": {"archive_store": {"effect": "internal", "autonomy": "notify"}},
    "admin_senders": ["admin-1"],
}


@pytest.fixture
def home(tmp_path: Path) -> Path:
    hermes_home = tmp_path / "opt-data"
    (hermes_home / "cache" / "documents").mkdir(parents=True)
    (hermes_home / "state.db").write_bytes(b"session history\n")
    (hermes_home / ".env").write_bytes(b"TOKEN=fake\n")
    return hermes_home


@pytest.fixture
def staging_guard(tmp_path: Path, home: Path) -> plugin.Guard:
    hermes = tmp_path / "hermes" / "config.yaml"
    hermes.parent.mkdir()
    hermes.write_text("approvals:\n  mode: manual\n", encoding="utf-8")
    (tmp_path / "attachments").mkdir()
    config = GuardConfig.model_validate(
        {
            **CONFIG,
            "guard_dir": str(tmp_path / "guard"),
            "hermes_config_path": str(hermes),
            "attachment_inbox": str(tmp_path / "attachments"),
            "attachment_sources": [str(home / "cache" / "documents")],
        }
    )
    return plugin.Guard(config)


def _store(guard: plugin.Guard, source: str) -> None:
    args = {"source": source, "category_code": "11", "name": "Lease"}
    guard.pre_llm_call(session_id="s1", sender_id="admin-1")
    guard.pre_tool_call(tool_name="mcp__brain__archive_store", args=args, session_id="s1", tool_call_id="c1")
    guard.stamp_guard(tool_name="mcp__brain__archive_store", args=args, session_id="s1", tool_call_id="c1")


@pytest.mark.parametrize("source", ["../../state.db", "../../.env", "/opt/data/state.db", "..\\..\\state.db"])
def test_a_path_out_of_the_cache_is_never_copied(
    staging_guard: plugin.Guard, tmp_path: Path, source: str
) -> None:
    _store(staging_guard, source)
    assert list((tmp_path / "attachments").iterdir()) == []


def test_a_link_planted_in_the_cache_is_never_followed(
    staging_guard: plugin.Guard, tmp_path: Path, home: Path
) -> None:
    (home / "cache" / "documents" / "lease.pdf").symlink_to(home / "state.db")
    _store(staging_guard, "lease.pdf")
    assert list((tmp_path / "attachments").iterdir()) == []
