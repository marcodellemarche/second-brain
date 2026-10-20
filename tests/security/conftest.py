"""Shared fixtures for the attack scenarios: a running guard and a way to read its audit log."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from second_brain.guard import plugin
from second_brain.guard.config import GuardConfig

CONFIG: dict[str, Any] = {
    "vault_root": "/vault",
    "guard_dir": "",
    "hermes_config_path": "",
    "media_dir": "/vault/_meta/media",
    "private_prefixes": ["_brain", "documents"],
    "untrusted_prefixes": ["raw"],
    "mcp_tools": {
        "vault_read": {"effect": "read", "autonomy": "auto", "taints_private": True},
        "vault_write": {"effect": "internal", "autonomy": "auto"},
        "vault_delete": {"effect": "internal", "autonomy": "auto"},
        "ask_team": {"effect": "external", "autonomy": "ask"},
        "share_expense": {"effect": "visible", "autonomy": "auto"},
    },
    "admin_senders": ["admin-1"],
}


@pytest.fixture
def guard(tmp_path: Path) -> plugin.Guard:
    hermes = tmp_path / "hermes" / "config.yaml"
    hermes.parent.mkdir()
    hermes.write_text("approvals:\n  mode: manual\n", encoding="utf-8")
    config = GuardConfig.model_validate(
        {
            **CONFIG,
            "guard_dir": str(tmp_path / "guard"),
            "hermes_config_path": str(hermes),
        }
    )
    return plugin.Guard(config)


def audit_rows(guard: plugin.Guard) -> list[dict[str, Any]]:
    """Every line of the audit log, oldest first."""
    path = guard.guard_dir / plugin.AUDIT_FILE
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


@pytest.fixture
def decisions(guard: plugin.Guard) -> Callable[[], list[dict[str, Any]]]:
    """Reads the decision rows at call time, so the test sees the calls it just made."""

    def read() -> list[dict[str, Any]]:
        return [row for row in audit_rows(guard) if row.get("event") == "decision"]

    return read
