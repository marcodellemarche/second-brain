"""The vault schema and the two shipped templates: every zone mapped, no stray frontmatter key."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import pytest
import yaml

from second_brain.core.schema import SchemaError, VaultSchema, load_schema
from second_brain.core.types import FRONTMATTER_KEYS, PERSONAL_ZONES, Zone

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
    assert load_schema(TEMPLATES / "en").zones == {zone: zone.value for zone in PERSONAL_ZONES}


def test_every_zone_is_mapped(language: str) -> None:
    schema = load_schema(TEMPLATES / language)
    assert set(schema.zones) == set(PERSONAL_ZONES)
    assert {schema.folder(zone) for zone in PERSONAL_ZONES} == set(schema.zones.values())


def test_every_zone_has_a_folder(template: Path, language: str) -> None:
    schema = load_schema(template)
    for zone in PERSONAL_ZONES:
        assert (template / schema.folder(zone)).is_dir(), f"{language}: no folder for {zone}"


def test_raw_is_the_only_untrusted_zone(language: str) -> None:
    assert load_schema(TEMPLATES / language).untrusted_zones == {Zone.RAW}


def test_meta_and_brain_pages_are_present(template: Path, language: str) -> None:
    names = {
        "en": ["profile.md", "commitments.md", "positions.md", "preferences.md", "initiative.md"],
        "it": ["profilo.md", "impegni.md", "posizioni.md", "preferenze.md", "iniziativa.md"],
    }[language]
    assert not (template / "_meta" / "index.md").exists()
    assert not (template / "_meta" / "log.md").exists()
    for name in [*names, "policy.md"]:
        assert (template / "_brain" / name).is_file(), name


@pytest.mark.parametrize("zone", sorted(PERSONAL_ZONES))
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
    return sorted(path for path in template.rglob("*.md") if path.name != "SCHEMA.md")


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


def test_the_block_maps_every_zone_a_personal_vault_has(template: Path) -> None:
    # The four team zones are optional: a personal vault does not carry an empty `incidents/`.
    assert set(schema_block(template)["zones"]) == {zone.value for zone in PERSONAL_ZONES}


BRAIN_STEMS = {
    "en": {
        "commitments": "commitments",
        "initiative": "initiative",
        "policy": "policy",
        "positions": "positions",
        "preferences": "preferences",
        "profile": "profile",
        "lint": "lint",
    },
    "it": {
        "commitments": "impegni",
        "initiative": "iniziativa",
        "policy": "policy",
        "positions": "posizioni",
        "preferences": "preferenze",
        "profile": "profilo",
        "lint": "lint",
    },
}


def test_both_templates_name_the_brain_pages(language: str) -> None:
    assert load_schema(TEMPLATES / language).brain_pages == BRAIN_STEMS[language]


def test_a_brain_page_resolves_through_the_brain_folder(language: str) -> None:
    schema = load_schema(TEMPLATES / language)
    assert schema.page("commitments") == f"_brain/{BRAIN_STEMS[language]['commitments']}.md"
    assert schema.page("initiative") == f"_brain/{BRAIN_STEMS[language]['initiative']}.md"
    assert schema.page("lint") == "_brain/lint.md"


def test_every_named_brain_page_exists_in_the_template(template: Path) -> None:
    schema = load_schema(template)
    for name in schema.brain_pages:
        path = schema.page(name)
        if name == "lint":
            continue
        assert (template / path).is_file(), f"{name}: no {path}"


def test_an_unknown_brain_page_is_refused(vault: Path) -> None:
    with pytest.raises(SchemaError, match="nowhere"):
        load_schema(vault).page("nowhere")


@pytest.mark.parametrize("stem", ["", ".", "..", "a/b", "a\\b", "initiative.md", ".hidden"])
def test_an_invalid_brain_page_stem_is_rejected(stem: str) -> None:
    zones = {zone: zone.value for zone in Zone}
    with pytest.raises(ValueError):
        VaultSchema(language="en", zones=zones, types={"note": "a note"}, brain_pages={"initiative": stem})


@pytest.mark.parametrize(
    "name", ["", "Commitments", "commit-ments", "commitments2", "commitments/initiative"]
)
def test_a_brain_page_name_is_a_logical_name(name: str) -> None:
    zones = {zone: zone.value for zone in Zone}
    with pytest.raises(ValueError):
        VaultSchema(language="en", zones=zones, types={"note": "a note"}, brain_pages={name: "initiative"})


def test_two_brain_pages_cannot_share_a_stem() -> None:
    zones = {zone: zone.value for zone in Zone}
    with pytest.raises(ValueError, match="initiative"):
        VaultSchema(
            language="en",
            zones=zones,
            types={"note": "a note"},
            brain_pages={"initiative": "initiative", "policy": "initiative"},
        )


def test_a_schema_without_brain_pages_still_loads(vault: Path) -> None:
    block = schema_block(vault)
    del block["brain_pages"]
    write_schema(vault, block)
    schema = load_schema(vault)
    assert schema.brain_pages == {}
    with pytest.raises(SchemaError):
        schema.page("initiative")
