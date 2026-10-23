"""A source that claims to be a new version of a call cannot rewrite the words already in raw.

Anyone who can drop a note in the capture folder can mark it as a revision. Raw is the record of
what was said, so a revision only ever adds a transcript next to the old one, inside raw, and the
old file keeps every word.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from second_brain.tools.calls import CallTools, TranscriptParams
from second_brain.tools.vault import Provenance, Vault, VaultConfig, VaultError

SCHEMA = Path(__file__).resolve().parents[2] / "templates" / "vault" / "en"
BRAIN = Provenance(requested_by="brain", reason="test", source_integrity="untrusted", session="s1")
SOURCE = "inbox/calls/2026-10/call-2026-10-21 16.01.18 - design.md"
RAW = "raw/calls/2026/call-2026-10-21 16.01.18 - design.md"
ORIGINAL = "---\ntype: transcript\ntrust: untrusted\n---\n\n**[1s] remote_1:** we agreed on the budget\n"
FORGED = "---\ndate: 2026-10-21\n---\n\n**[1s] remote_1:** we never agreed on anything\n"


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
    vault.write(RAW, ORIGINAL, BRAIN)
    vault.write(SOURCE, FORGED, BRAIN)
    vault.write("topics/budget.md", "# Budget\n\nprivate words\n", BRAIN)
    return vault


def _params(path: str) -> TranscriptParams:
    return TranscriptParams(source=SOURCE, path=path, date="2026-10-21", created="2026-10-23", revision=True)


def test_a_revision_adds_a_file_and_leaves_the_old_words(vault: Vault) -> None:
    result = CallTools(vault).transcript(_params(RAW), BRAIN)
    assert result["path"] != RAW
    assert result["path"].startswith("raw/")
    assert vault.read(RAW)[0] == ORIGINAL


@pytest.mark.parametrize(
    "path",
    ["topics/budget.md", "raw/../topics/budget.md", "calls/2026/call-2026-10-21 16.01.18 - design.md"],
)
def test_a_revision_cannot_land_outside_raw(vault: Vault, path: str) -> None:
    with pytest.raises(VaultError):
        CallTools(vault).transcript(_params(path), BRAIN)
    assert vault.read("topics/budget.md")[0] == "# Budget\n\nprivate words\n"
