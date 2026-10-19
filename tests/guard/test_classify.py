from dataclasses import FrozenInstanceError

import pytest

from second_brain.core.types import Autonomy, Effect
from second_brain.guard.classify import BUILTIN, MCP_PREFIX, UNKNOWN, ToolClass, classify, path_taint

EXPECTED: dict[str, tuple[Effect, Autonomy, bool, bool]] = {
    "web_search": (Effect.EXTERNAL, Autonomy.AUTO, True, False),
    "web_extract": (Effect.EXTERNAL, Autonomy.AUTO, True, False),
    "browser_navigate": (Effect.EXTERNAL, Autonomy.AUTO, True, False),
    "browser_snapshot": (Effect.EXTERNAL, Autonomy.AUTO, True, False),
    "read_file": (Effect.READ, Autonomy.AUTO, False, False),
    "search_files": (Effect.READ, Autonomy.AUTO, False, False),
    "write_file": (Effect.INTERNAL, Autonomy.AUTO, False, False),
    "patch": (Effect.INTERNAL, Autonomy.AUTO, False, False),
    "terminal": (Effect.INTERNAL, Autonomy.AUTO, False, False),
    "memory": (Effect.INTERNAL, Autonomy.NOTIFY, False, False),
    "skill_manage": (Effect.INTERNAL, Autonomy.NOTIFY, False, False),
    "session_search": (Effect.READ, Autonomy.AUTO, False, False),
    "delegate_task": (Effect.INTERNAL, Autonomy.AUTO, False, False),
    "send_message": (Effect.VISIBLE, Autonomy.NEVER, False, False),
    "text_to_speech": (Effect.INTERNAL, Autonomy.AUTO, False, False),
    "execute_code": (Effect.INTERNAL, Autonomy.NEVER, False, False),
    "cronjob": (Effect.INTERNAL, Autonomy.NEVER, False, False),
    "cronjob_manage": (Effect.INTERNAL, Autonomy.NEVER, False, False),
    "manage_connections": (Effect.INTERNAL, Autonomy.NEVER, False, False),
    "manage_catalog": (Effect.INTERNAL, Autonomy.NEVER, False, False),
}


@pytest.mark.parametrize(("name", "expected"), EXPECTED.items())
def test_builtin_table(name: str, expected: tuple[Effect, Autonomy, bool, bool]) -> None:
    effect, autonomy, untrusted, private = expected
    assert BUILTIN[name] == ToolClass(effect, autonomy, untrusted, private)
    assert classify(name, {}) == ToolClass(effect, autonomy, untrusted, private)


def test_builtin_table_has_no_extra_tools() -> None:
    assert set(BUILTIN) == set(EXPECTED)


def test_tool_class_is_frozen() -> None:
    with pytest.raises(FrozenInstanceError):
        BUILTIN["web_search"].effect = Effect.READ  # type: ignore[misc]


def test_mcp_tool_is_found_without_prefix() -> None:
    spec = ToolClass(Effect.INTERNAL, Autonomy.AUTO)
    assert classify(MCP_PREFIX + "vault_write", {"vault_write": spec}) is spec


def test_mcp_tool_missing_from_specs_fails_closed() -> None:
    specs = {"vault_write": ToolClass(Effect.INTERNAL, Autonomy.AUTO)}
    assert classify(MCP_PREFIX + "vault_delete", specs) == UNKNOWN


def test_unknown_tool_fails_closed() -> None:
    assert classify("rm_rf", {}) == ToolClass(Effect.EXTERNAL, Autonomy.NEVER, True, True)


def test_other_mcp_server_fails_closed() -> None:
    specs = {"vault_write": ToolClass(Effect.INTERNAL, Autonomy.AUTO)}
    assert classify("mcp__other__vault_write", specs) == UNKNOWN


VAULT = "/srv/vault"
PRIVATE = ("_brain", "Personale")
UNTRUSTED = ("raw", "calls")


def taint(path: str) -> tuple[bool, bool]:
    return path_taint(path, VAULT, PRIVATE, UNTRUSTED)


def test_plain_vault_path_is_clean() -> None:
    assert taint("/srv/vault/Topics/note.md") == (False, False)


def test_relative_path_is_vault_relative() -> None:
    assert taint("Topics/note.md") == (False, False)
    assert taint("raw/note.md") == (True, False)


def test_untrusted_prefix() -> None:
    assert taint("/srv/vault/raw/mail/2024-01-01.md") == (True, False)
    assert taint("/srv/vault/calls/standup.md") == (True, False)


def test_private_prefix() -> None:
    assert taint("/srv/vault/_brain/user.md") == (False, True)
    assert taint("/srv/vault/Personale/taxes.md") == (False, True)


def test_prefix_directory_itself_taints() -> None:
    assert taint("/srv/vault/raw") == (True, False)
    assert taint("/srv/vault/_brain/") == (False, True)


def test_similar_prefix_does_not_match() -> None:
    assert taint("/srv/vault/raw_notes/note.md") == (False, False)
    assert taint("/srv/vault/_braindump/note.md") == (False, False)


def test_outside_the_vault_taints_nothing() -> None:
    assert taint("/etc/passwd") == (False, False)
    assert taint("/srv/vault-other/raw/note.md") == (False, False)
    assert taint("/tmp/note.md") == (False, False)


def test_dotdot_escaping_the_vault_taints_nothing() -> None:
    assert taint("/srv/vault/../etc/passwd") == (False, False)
    assert taint("raw/../../etc/passwd") == (False, False)


def test_dotdot_staying_inside_the_vault_is_resolved() -> None:
    assert taint("/srv/vault/Topics/../raw/note.md") == (True, False)
    assert taint("/srv/vault/raw/../Topics/note.md") == (False, False)


def test_both_prefixes_can_match() -> None:
    assert path_taint("/srv/vault/raw/_brain.md", VAULT, ("raw",), ("raw",)) == (True, True)


def test_empty_prefixes_taint_nothing() -> None:
    assert path_taint("/srv/vault/raw/note.md", VAULT, (), ()) == (False, False)
