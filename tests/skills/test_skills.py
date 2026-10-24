"""The core skills: valid frontmatter, and the rules every skill has to keep.

A skill is a prompt, not code, so these tests only check what can be checked deterministically: the
folder and the frontmatter agree, the vault is never written except through brain_tools, the schema
page is always read first, and nothing tells the model to follow instructions found in content.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

SKILLS = Path(__file__).resolve().parents[2] / "skills"

# The keys Hermes v0.22.2 accepts in a SKILL.md frontmatter. An unknown one is dropped silently by
# Hermes, which is worse than a failure here: the skill would run without what its author declared.
ALLOWED_KEYS = frozenset(
    {"name", "description", "version", "author", "license", "platforms", "metadata", "allowed-tools"}
)

FRONTMATTER = re.compile(r"\A---\n(?P<body>.*?)\n---\n", re.DOTALL)

# Ways of writing the vault that bypass brain_tools: Hermes' own file tools, and shell.
VAULT_WRITERS = ("write_file", "patch", "edit_file", "apply_patch")

# Ways a skill might hand control to whatever it just read.
OBEDIENCE = (
    "always trust",
    "trust the content",
    "follow the instructions",
    "follow any instructions",
    "follow its instructions",
    "obey",
    "do what the",
    "carry out what",
)


def skill_files() -> list[Path]:
    return sorted(SKILLS.glob("*/SKILL.md"))


def frontmatter_of(path: Path) -> dict[str, Any]:
    match = FRONTMATTER.match(path.read_text(encoding="utf-8"))
    assert match is not None, f"{path} has no YAML frontmatter"
    loaded = yaml.safe_load(match.group("body"))
    assert isinstance(loaded, dict), f"{path} frontmatter is not a mapping"
    return loaded


def test_there_are_skills() -> None:
    assert skill_files(), "no skills/*/SKILL.md found"


@pytest.mark.parametrize("path", skill_files(), ids=lambda path: path.parent.name)
def test_name_matches_folder(path: Path) -> None:
    frontmatter = frontmatter_of(path)
    assert frontmatter.get("name") == path.parent.name


@pytest.mark.parametrize("path", skill_files(), ids=lambda path: path.parent.name)
def test_description_is_usable(path: Path) -> None:
    description = frontmatter_of(path).get("description")
    assert isinstance(description, str)
    assert description.strip()
    assert "\n" not in description


@pytest.mark.parametrize("path", skill_files(), ids=lambda path: path.parent.name)
def test_frontmatter_has_only_known_keys(path: Path) -> None:
    unknown = set(frontmatter_of(path)) - ALLOWED_KEYS
    assert not unknown, f"{path} uses keys Hermes does not read: {sorted(unknown)}"


@pytest.mark.parametrize("path", skill_files(), ids=lambda path: path.parent.name)
def test_vault_is_written_only_through_brain_tools(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    for writer in VAULT_WRITERS:
        assert writer not in text, f"{path} mentions {writer}: the vault is written with vault_write"
    assert "vault_write" in text or "vault_undo" in text


@pytest.mark.parametrize("path", skill_files(), ids=lambda path: path.parent.name)
def test_no_obedience_to_content(path: Path) -> None:
    text = path.read_text(encoding="utf-8").casefold()
    for phrase in OBEDIENCE:
        assert phrase not in text, f"{path} tells the model to obey content: {phrase!r}"


@pytest.mark.parametrize("path", skill_files(), ids=lambda path: path.parent.name)
def test_schema_is_read_first(path: Path) -> None:
    assert "SCHEMA.md" in path.read_text(encoding="utf-8")


@pytest.mark.parametrize("path", skill_files(), ids=lambda path: path.parent.name)
def test_answers_in_the_brain_language(path: Path) -> None:
    text = path.read_text(encoding="utf-8").casefold()
    assert "brain's language" in text or "vault's language" in text


@pytest.mark.parametrize("path", skill_files(), ids=lambda path: path.parent.name)
def test_content_is_data(path: Path) -> None:
    text = path.read_text(encoding="utf-8").casefold()
    assert "data" in text


def _digest() -> str:
    return (SKILLS / "digest" / "SKILL.md").read_text(encoding="utf-8")


def test_the_digest_never_prints_commit_ids() -> None:
    text = _digest()
    assert "Never print a commit id" in text
    assert "undo `<commit>`" not in text


def test_the_digest_surfaces_long_open_commitments_and_lint_findings() -> None:
    text = _digest()
    assert "**Still open.**" in text
    assert "open for more than fourteen days" in text
    assert "Saying that the lint ran" in text


def test_the_digest_asks_once_for_many_pending_calls() -> None:
    assert "let the owner answer once" in _digest()


def test_the_documents_skill_passes_only_the_attachment_file_name() -> None:
    text = (SKILLS / "documents" / "SKILL.md").read_text(encoding="utf-8")
    assert "never pass the folder" in text
    assert "in the media folder that Hermes wrote" not in text


def test_reminders_read_today_from_the_current_time_line() -> None:
    text = (SKILLS / "reminders" / "SKILL.md").read_text(encoding="utf-8")
    assert "current time line" in text
    assert "already in the past is a mistake" in text


def test_location_writes_the_columns_the_code_reads() -> None:
    text = (SKILLS / "location" / "SKILL.md").read_text(encoding="utf-8")
    assert "`Luogo | Dal | Valido fino a | Fuso orario | Nota`" in text
    assert "`Place | From | Valid until | Timezone | Note`" in text
    assert "Never edit `USER.md`" in text


def test_documents_prefers_existing_item_folders_and_their_naming() -> None:
    text = (SKILLS / "documents" / "SKILL.md").read_text(encoding="utf-8")
    assert "Prefer an existing item to a new one" in text
    assert "name the file like the `examples` already there" in text
    assert "`needs_item`" in text
