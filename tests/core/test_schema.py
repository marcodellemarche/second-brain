"""The vault schema and the two shipped templates: every zone mapped, no stray frontmatter key."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import pytest
import yaml

from second_brain.core.schema import SchemaError, VaultSchema, load_schema
from second_brain.core.types import FRONTMATTER_KEYS, Zone

TEMPLATES = Path(__file__).resolve().parents[2] / "templates" / "vault"
LANGUAGES = ["en", "it"]
ITALIAN_FOLDERS = {
    Zone.INBOX: "inbox",
    Zone.RAW: "raw",
    Zone.CALLS: "call",
    Zone.TOPICS: "argomenti",
    Zone.KNOWLEDGE: "conoscenza",
    Zone.IDEAS: "idee",
    Zone.PROJECTS: "progetti",
    Zone.PEOPLE: "persone",
    Zone.WORK: "lavoro",
    Zone.DOCUMENTS: "documenti",
    Zone.ANSWERS: "risposte",
    Zone.DRAWINGS: "disegni",
    Zone.ARCHIVE: "archivio",
    Zone.BRAIN: "_brain",
    Zone.META: "_meta",
}


@pytest.fixture(params=LANGUAGES)
def language(request: pytest.FixtureRequest) -> str:
    return str(request.param)


@pytest.fixture
def template(language: str) -> Path:
    return TEMPLATES / language


@pytest.fixture
def vault(template: Path, tmp_path: Path) -> Path:
    root = tmp_path / "vault"
    shutil.copytree(template, root)
    return root


def schema_text(vault_root: Path) -> str:
    return (vault_root / "_meta" / "SCHEMA.md").read_text(encoding="utf-8")


def write_schema(vault_root: Path, block: dict[str, Any]) -> None:
    body = yaml.safe_dump(block, allow_unicode=True, sort_keys=False)
    (vault_root / "_meta" / "SCHEMA.md").write_text(f"# Schema\n\n```yaml\n{body}```\n", encoding="utf-8")


def schema_block(vault_root: Path) -> dict[str, Any]:
    text = schema_text(vault_root)
    return yaml.safe_load(text.split("```yaml", 1)[1].split("```", 1)[0])


def test_both_templates_load(language: str) -> None:
    schema = load_schema(TEMPLATES / language)
    assert schema.language == language


def test_italian_template_uses_the_planned_folders() -> None:
    assert load_schema(TEMPLATES / "it").zones == ITALIAN_FOLDERS


def test_english_template_uses_the_logical_names() -> None:
    assert load_schema(TEMPLATES / "en").zones == {zone: zone.value for zone in Zone}


def test_every_zone_is_mapped(language: str) -> None:
    schema = load_schema(TEMPLATES / language)
    assert set(schema.zones) == set(Zone)
    assert {schema.folder(zone) for zone in Zone} == set(schema.zones.values())


def test_every_zone_has_a_folder(template: Path, language: str) -> None:
    schema = load_schema(template)
    for zone in Zone:
        assert (template / schema.folder(zone)).is_dir(), f"{language}: no folder for {zone}"


def test_raw_is_the_only_untrusted_zone(language: str) -> None:
    assert load_schema(TEMPLATES / language).untrusted_zones == {Zone.RAW}


def test_meta_and_brain_pages_are_present(template: Path, language: str) -> None:
    names = {
        "en": ["profile.md", "commitments.md", "positions.md", "preferences.md", "initiative.md"],
        "it": ["profilo.md", "impegni.md", "posizioni.md", "preferenze.md", "iniziativa.md"],
    }[language]
    assert (template / "_meta" / "index.md").is_file()
    assert (template / "_meta" / "log.md").is_file()
    for name in [*names, "policy.md"]:
        assert (template / "_brain" / name).is_file(), name


def test_log_has_the_line_format(template: Path) -> None:
    assert "## [YYYY-MM-DD] <op> | <title>" in (template / "_meta" / "log.md").read_text(encoding="utf-8")


@pytest.mark.parametrize("zone", list(Zone))
def test_zone_of_nested_paths(vault: Path, zone: Zone) -> None:
    schema = load_schema(vault)
    folder = schema.folder(zone)
    assert schema.zone_of(f"{folder}/2026/a/b.md") is zone
    assert schema.zone_of(f"./{folder}/a.md") is zone
    assert schema.zone_of(f"{folder}/2026/a.md".replace("/", "\\")) is zone


def test_zone_of_reads_the_folder_not_the_logical_name(vault: Path, language: str) -> None:
    schema = load_schema(vault)
    other_language = "call" if language == "en" else "calls"
    assert schema.zone_of(f"{schema.folder(Zone.CALLS)}/2026/a.md") is Zone.CALLS
    assert schema.zone_of(f"{other_language}/2026/a.md") is None
    assert schema.zone_of(f"{schema.folder(Zone.RAW)}/{other_language}/2026/a.md") is Zone.RAW


@pytest.mark.parametrize("path", ["", ".", "notes.md", "_scarti/x.md", "../raw/a.md"])
def test_zone_of_outside_every_zone(vault: Path, path: str) -> None:
    assert load_schema(vault).zone_of(path) is None


def test_folder_refuses_an_unmapped_zone(vault: Path) -> None:
    schema = load_schema(vault)
    broken = schema.model_copy(update={"zones": {}})
    with pytest.raises(SchemaError):
        broken.folder(Zone.INBOX)


def test_a_missing_zone_is_rejected(vault: Path) -> None:
    block = schema_block(vault)
    del block["zones"]["drawings"]
    write_schema(vault, block)
    with pytest.raises(SchemaError, match="drawings"):
        load_schema(vault)


def test_a_missing_zone_is_rejected_at_validation() -> None:
    zones = {zone: zone.value for zone in Zone if zone is not Zone.ANSWERS}
    with pytest.raises(ValueError, match="answers"):
        VaultSchema(language="en", zones=zones, types={"note": "a note"})


def test_an_unknown_zone_is_rejected(vault: Path) -> None:
    block = schema_block(vault)
    block["zones"]["nowhere"] = "nowhere"
    write_schema(vault, block)
    with pytest.raises(SchemaError):
        load_schema(vault)


@pytest.mark.parametrize("folder", ["", ".", "..", "a/b", "a\\b"])
def test_an_invalid_folder_name_is_rejected(folder: str) -> None:
    zones = {zone: zone.value for zone in Zone} | {Zone.INBOX: folder}
    with pytest.raises(ValueError):
        VaultSchema(language="en", zones=zones, types={"note": "a note"})


def test_two_zones_cannot_share_a_folder(vault: Path) -> None:
    block = schema_block(vault)
    block["zones"]["ideas"] = block["zones"]["topics"]
    write_schema(vault, block)
    with pytest.raises(SchemaError):
        load_schema(vault)


def test_a_type_without_a_meaning_is_rejected() -> None:
    zones = {zone: zone.value for zone in Zone}
    with pytest.raises(ValueError):
        VaultSchema(language="en", zones=zones, types={"note": "  "})


@pytest.mark.parametrize("block", ["", "no yaml here", "```yaml\n- a\n- b\n```", "```yaml\n- [\n```"])
def test_a_broken_schema_file_is_rejected(vault: Path, block: str) -> None:
    (vault / "_meta" / "SCHEMA.md").write_text(block, encoding="utf-8")
    with pytest.raises(SchemaError):
        load_schema(vault)


def test_a_missing_schema_file_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(SchemaError):
        load_schema(tmp_path)


def test_the_first_yaml_block_wins(vault: Path) -> None:
    block = schema_block(vault)
    (vault / "_meta" / "SCHEMA.md").write_text(
        f"# Schema\n\n```yaml\n{yaml.safe_dump(block)}\n```\n\n```yaml\nlanguage: xx\n```\n",
        encoding="utf-8",
    )
    assert load_schema(vault).language == block["language"]


def template_pages(template: Path) -> list[Path]:
    return sorted(
        path for path in template.rglob("*.md") if path.name != "SCHEMA.md" and path.name != "log.md"
    )


def frontmatter(path: Path) -> dict[str, Any] | None:
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---\n"):
        return None
    head = text.split("---\n", 2)[1]
    return yaml.safe_load(head) or {}


def test_template_pages_have_frontmatter(template: Path) -> None:
    pages = [path for path in template_pages(template) if path.parent.name == "_brain"]
    assert pages
    for path in pages:
        data = frontmatter(path)
        assert data is not None, f"{path} has no frontmatter"
        assert data["type"] == "brain", path


def test_no_frontmatter_key_outside_the_known_set(template: Path) -> None:
    for path in template_pages(template):
        data = frontmatter(path)
        if data is None:
            continue
        unknown = sorted(set(data) - FRONTMATTER_KEYS)
        assert not unknown, f"{path.relative_to(template)}: {unknown}"


def test_the_block_maps_every_zone_by_its_logical_name(template: Path) -> None:
    assert set(schema_block(template)["zones"]) == {zone.value for zone in Zone}
