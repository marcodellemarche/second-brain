"""A prompt injection that wants a private note in the team's hands gets nowhere with team_handoff.

The tool moves a note out of the personal vault towards other people, so it only takes call notes
and captures, never a path that climbs out of them, never a note that looks like it holds a
secret, and the guard asks the owner every time, even in a session that read nothing untrusted.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from second_brain.core.types import Autonomy, Effect
from second_brain.guard import plugin
from second_brain.guard.classify import ToolClass
from second_brain.tools.calls import CallTools, HandoffConfig, HandoffParams
from second_brain.tools.vault import Provenance, Vault, VaultConfig, VaultError

SCHEMA = Path(__file__).resolve().parents[2] / "templates" / "vault" / "en"
BRAIN = Provenance(requested_by="brain", reason="test", source_integrity="untrusted", session="s1")


@pytest.fixture()
def vault(tmp_path: Path) -> Vault:
    root = tmp_path / "vault"
    root.mkdir()
    for path in sorted(SCHEMA.rglob("*")):
        if path.is_file():
            target = root / path.relative_to(SCHEMA)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(path.read_bytes())
    subprocess.run(["git", "init", "-q", str(root)], check=True, capture_output=True)
    vault = Vault(VaultConfig(root=root))
    vault.write("people/alice.md", "# Alice\n\nprivate words\n", BRAIN)
    return vault


@pytest.mark.parametrize(
    "path",
    [
        "calls/../people/alice.md",
        "calls/2026/../../people/alice.md",
        "people/alice.md",
        "/vault/people/alice.md",
        "_brain/preferences.md",
    ],
)
def test_a_private_note_cannot_be_handed_off(vault: Vault, tmp_path: Path, path: str) -> None:
    folder = tmp_path / "handoff"
    with pytest.raises(VaultError):
        CallTools(vault, HandoffConfig(dir=folder)).handoff(HandoffParams(path=path, reason="team"), BRAIN)
    assert not folder.exists() or not any(folder.iterdir())
    assert (vault.root / "people/alice.md").exists()


def test_a_call_that_looks_like_it_holds_a_secret_stays_home(vault: Vault, tmp_path: Path) -> None:
    note = "calls/2026/call.md"
    (vault.root / note).parent.mkdir(parents=True, exist_ok=True)
    (vault.root / note).write_text(
        "---\ntype: call\n---\n\nAWS key AKIAIOSFODNN7EXAMPLE and secret "
        "aws_secret_access_key = wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY\n",
        encoding="utf-8",
    )
    folder = tmp_path / "handoff"
    with pytest.raises(VaultError) as raised:
        CallTools(vault, HandoffConfig(dir=folder)).handoff(HandoffParams(path=note, reason="team"), BRAIN)
    assert raised.value.code == "secret_detected"
    assert not folder.exists()


def test_the_guard_asks_before_every_hand_off(guard: plugin.Guard) -> None:
    guard.tool_classes["team_handoff"] = ToolClass(Effect.INTERNAL, Autonomy.ASK)
    decision = guard.pre_tool_call(
        tool_name="mcp__brain__team_handoff",
        args={"path": "calls/2026/call.md", "reason": "team"},
        session_id="s1",
        tool_call_id="c1",
    )
    assert decision is not None and decision["action"] == "approve"
    assert "calls/2026/call.md" in decision["message"]
