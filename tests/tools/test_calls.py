"""Call tools: the transcript is copied by code, and a team call leaves the vault only by hand-off."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from second_brain.tools.calls import (
    CallTools,
    HandoffConfig,
    HandoffParams,
    TranscriptParams,
    call_tools,
    split_note,
)
from second_brain.tools.vault import Provenance, Vault, VaultConfig, VaultError, is_empty_note

SCHEMA = Path(__file__).resolve().parents[2] / "templates" / "vault" / "en"
SOURCE = "inbox/calls/2026-10/call-2026-10-21 16.01.18 - design.md"
RAW = "raw/calls/2026/call-2026-10-21 16.01.18 - design.md"
CALL = "calls/2026/call-2026-10-21 16.01.18 - design.md"
BRAIN = Provenance(requested_by="brain", reason="test", source_integrity="untrusted", session="s1")

TRANSCRIPT = [f"**[{second}s] remote_1:** line number {second}" for second in range(400)]
SOURCE_NOTE = "\n".join(
    [
        "---",
        "date: 2026-10-21",
        "participants: [Alice Example, Bob Example]",
        "---",
        "",
        "# Design session - 2026-10-21 16:01",
        "",
        "## Summary",
        "",
        "We talked about a map with holes.",
        "",
        "## Full transcript",
        "",
        *TRANSCRIPT,
        "",
    ]
)


def _git(*args: str, root: Path) -> None:
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)


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
    _git("add", "-A", root=root)
    _git("-c", "user.name=t", "-c", "user.email=t@localhost", "commit", "-qm", "scaffold", root=root)
    vault = Vault(VaultConfig(root=root))
    vault.write(SOURCE, SOURCE_NOTE, BRAIN)
    return vault


def _params(**overrides: object) -> TranscriptParams:
    values: dict[str, object] = {
        "source": SOURCE,
        "path": RAW,
        "from_heading": "Full transcript",
        "date": "2026-10-21",
        "start": "16:01",
        "participants": ["Alice Example", "Bob Example"],
        "created": "2026-10-21",
    }
    values.update(overrides)
    return TranscriptParams.model_validate(values)


def test_the_transcript_lands_in_raw_word_for_word(vault: Vault) -> None:
    result = CallTools(vault).transcript(_params(), BRAIN)
    assert result["ok"] and result["lines"] == len(TRANSCRIPT) + 1
    content, _ = vault.read(RAW)
    frontmatter, body = split_note(content)
    assert frontmatter["type"] == "transcript"
    assert frontmatter["trust"] == "untrusted"
    assert frontmatter["sources"] == [SOURCE]
    assert frontmatter["start"] == "16:01"
    assert body[1] == "# Design session - 2026-10-21 16:01"
    assert [line for line in body if line.startswith("**[")] == TRANSCRIPT
    assert "We talked about a map with holes." not in content


def test_without_a_heading_the_whole_body_is_copied(vault: Vault) -> None:
    CallTools(vault).transcript(_params(from_heading=None), BRAIN)
    content, _ = vault.read(RAW)
    assert "We talked about a map with holes." in content
    assert TRANSCRIPT[-1] in content


def test_a_missing_heading_is_refused_and_nothing_is_written(vault: Vault) -> None:
    with pytest.raises(VaultError) as raised:
        CallTools(vault).transcript(_params(from_heading="Transcript nowhere"), BRAIN)
    assert raised.value.code == "not_found"
    assert not (vault.root / RAW).exists()


def test_a_transcript_already_in_raw_is_left_alone(vault: Vault) -> None:
    tools = CallTools(vault)
    tools.transcript(_params(), BRAIN)
    before, _ = vault.read(RAW)
    with pytest.raises(VaultError) as raised:
        tools.transcript(_params(from_heading=None), BRAIN)
    assert raised.value.code == "exists"
    assert vault.read(RAW)[0] == before


def test_a_revision_keeps_the_old_transcript_and_writes_the_new_one_next_to_it(vault: Vault) -> None:
    tools = CallTools(vault)
    tools.transcript(_params(), BRAIN)
    before, _ = vault.read(RAW)
    named = SOURCE_NOTE.replace("remote_1:", "Alice Example:")
    vault.write(SOURCE, named, BRAIN, vault.read(SOURCE)[1])
    result = tools.transcript(_params(revision=True), BRAIN)
    revised = RAW.replace(".md", " (rev 2).md")
    assert result["ok"] and result["path"] == revised
    assert vault.read(RAW)[0] == before
    assert "Alice Example:** line number 7" in vault.read(revised)[0]


def test_the_same_revision_twice_writes_nothing(vault: Vault) -> None:
    tools = CallTools(vault)
    tools.transcript(_params(), BRAIN)
    unchanged = tools.transcript(_params(revision=True, created="2026-10-23"), BRAIN)
    assert unchanged == {"ok": True, "unchanged": True, "path": RAW}
    vault.write(SOURCE, SOURCE_NOTE.replace("remote_1:", "Bob Example:"), BRAIN, vault.read(SOURCE)[1])
    first = tools.transcript(_params(revision=True), BRAIN)
    again = tools.transcript(_params(revision=True), BRAIN)
    assert again == {"ok": True, "unchanged": True, "path": first["path"]}
    assert not (vault.root / RAW.replace(".md", " (rev 3).md")).exists()


def test_an_empty_transcript_in_raw_is_completed(vault: Vault) -> None:
    stub = "---\ntype: transcript\ntrust: untrusted\n---\n\n# Design session - 2026-10-21 16:01\n"
    vault.write(RAW, stub, BRAIN)
    result = CallTools(vault).transcript(_params(), BRAIN)
    assert result["filled"] is True
    assert TRANSCRIPT[200] in vault.read(RAW)[0]


def test_the_transcript_only_goes_from_the_inbox_into_raw(vault: Vault) -> None:
    tools = CallTools(vault)
    with pytest.raises(VaultError) as raised:
        tools.transcript(_params(path="topics/design.md"), BRAIN)
    assert raised.value.code == "zone_denied"
    vault.write("topics/design.md", "# Design\n\nprivate words\n", BRAIN)
    with pytest.raises(VaultError) as raised:
        tools.transcript(_params(source="topics/design.md"), BRAIN)
    assert raised.value.code == "zone_denied"


def test_is_empty_note() -> None:
    assert is_empty_note("---\na: 1\n---\n\n# Title\n\n")
    assert is_empty_note("")
    assert not is_empty_note("---\na: 1\n---\n# Title\nwords\n")
    assert not is_empty_note("---\nnever closed\n")


def test_fill_never_rewrites_a_source_with_words(vault: Vault) -> None:
    vault.write(RAW, "---\ntype: transcript\n---\n# T\n\nsomeone said this\n", BRAIN)
    _, sha256 = vault.read(RAW)
    with pytest.raises(VaultError) as raised:
        vault.fill(RAW, "replaced\n", BRAIN, sha256)
    assert raised.value.code == "immutable"


# --- hand-off ------------------------------------------------------------------------------------


def _call_note(vault: Vault) -> None:
    vault.write(
        CALL,
        f"---\ntype: call\ntranscript: {RAW}\nsources:\n  - {SOURCE}\n---\n\n# Design session\n\nSummary.\n",
        BRAIN,
    )


def test_a_handed_off_call_leaves_the_vault_and_waits_in_the_folder(vault: Vault, tmp_path: Path) -> None:
    CallTools(vault).transcript(_params(), BRAIN)
    _call_note(vault)
    handoff = HandoffConfig(dir=tmp_path / "handoff", move_hint="rsync the folder")
    result = CallTools(vault, handoff).handoff(HandoffParams(path=CALL, reason="team work"), BRAIN)
    assert result["ok"]
    assert result["move_hint"] == "rsync the folder"
    assert result["transcript_kept"] == RAW
    assert len(result["commits"]) == 2
    written = tmp_path / "handoff" / Path(CALL).name
    assert "Summary." in written.read_text(encoding="utf-8")
    assert not (vault.root / CALL).exists()
    assert not (vault.root / SOURCE).exists()
    assert (vault.root / RAW).exists()


def test_a_second_hand_off_with_the_same_name_does_not_overwrite(vault: Vault, tmp_path: Path) -> None:
    folder = tmp_path / "handoff"
    folder.mkdir()
    (folder / Path(CALL).name).write_text("first\n", encoding="utf-8")
    _call_note(vault)
    result = CallTools(vault, HandoffConfig(dir=folder)).handoff(HandoffParams(path=CALL, reason="x"), BRAIN)
    assert result["handoff"].endswith("-2.md")
    assert (folder / Path(CALL).name).read_text(encoding="utf-8") == "first\n"


def test_only_calls_and_captures_can_be_handed_off(vault: Vault, tmp_path: Path) -> None:
    vault.write("people/alice.md", "# Alice\n\nprivate\n", BRAIN)
    tools = CallTools(vault, HandoffConfig(dir=tmp_path / "handoff"))
    for path in ("people/alice.md", RAW, "_brain/profile.md"):
        with pytest.raises(VaultError) as raised:
            tools.handoff(HandoffParams(path=path, reason="x"), BRAIN)
        assert raised.value.code == "zone_denied"
    assert not (tmp_path / "handoff").exists()


def test_the_hand_off_tool_asks_and_exists_only_when_configured(vault: Vault, tmp_path: Path) -> None:
    assert [tool.spec.name for tool in call_tools(vault)] == ["call_transcript"]
    tools = {tool.spec.name: tool for tool in call_tools(vault, HandoffConfig(dir=tmp_path))}
    assert tools["team_handoff"].spec.autonomy.value == "ask"
