"""What a tool does, how autonomous it is by default, and what its result taints.

brain_guard sees every Hermes tool call, built-in or MCP. This module is the single table that
answers those three questions for each one. Anything not in the table fails closed.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from second_brain.core.types import Autonomy, Effect

MCP_PREFIX = "mcp__brain__"


@dataclass(frozen=True)
class ToolClass:
    effect: Effect
    autonomy: Autonomy
    taints_untrusted: bool = False
    taints_private: bool = False


UNKNOWN = ToolClass(Effect.EXTERNAL, Autonomy.NEVER, True, True)

BUILTIN: dict[str, ToolClass] = {
    "web_search": ToolClass(Effect.EXTERNAL, Autonomy.AUTO, taints_untrusted=True),
    "web_extract": ToolClass(Effect.EXTERNAL, Autonomy.AUTO, taints_untrusted=True),
    "browser_navigate": ToolClass(Effect.EXTERNAL, Autonomy.AUTO, taints_untrusted=True),
    "browser_snapshot": ToolClass(Effect.EXTERNAL, Autonomy.AUTO, taints_untrusted=True),
    # Reading a vault path taints only if the path says so: see path_taint.
    "read_file": ToolClass(Effect.READ, Autonomy.AUTO),
    "search_files": ToolClass(Effect.READ, Autonomy.AUTO),
    # The vault is read-only at OS level, so these only reach Hermes' own work dirs.
    "write_file": ToolClass(Effect.INTERNAL, Autonomy.AUTO),
    "patch": ToolClass(Effect.INTERNAL, Autonomy.AUTO),
    "terminal": ToolClass(Effect.INTERNAL, Autonomy.AUTO),
    "memory": ToolClass(Effect.INTERNAL, Autonomy.NOTIFY),
    "skill_manage": ToolClass(Effect.INTERNAL, Autonomy.NOTIFY),
    "session_search": ToolClass(Effect.READ, Autonomy.AUTO),
    "delegate_task": ToolClass(Effect.INTERNAL, Autonomy.AUTO),
    "send_message": ToolClass(Effect.VISIBLE, Autonomy.NEVER),
    "text_to_speech": ToolClass(Effect.INTERNAL, Autonomy.AUTO),
    "execute_code": ToolClass(Effect.INTERNAL, Autonomy.NEVER),
    "cronjob": ToolClass(Effect.INTERNAL, Autonomy.NEVER),
    "cronjob_manage": ToolClass(Effect.INTERNAL, Autonomy.NEVER),
    "manage_connections": ToolClass(Effect.INTERNAL, Autonomy.NEVER),
    "manage_catalog": ToolClass(Effect.INTERNAL, Autonomy.NEVER),
}


def classify(tool_name: str, mcp_specs: dict[str, ToolClass]) -> ToolClass:
    """Class of a tool call. MCP tools from brain_tools arrive as `mcp__brain__<name>`.

    An unknown tool, or a tool from any other MCP server, is external and never allowed.
    """
    if tool_name.startswith(MCP_PREFIX):
        return mcp_specs.get(tool_name.removeprefix(MCP_PREFIX), UNKNOWN)
    return BUILTIN.get(tool_name, UNKNOWN)


def path_taint(
    path: str,
    vault_root: str,
    private_prefixes: tuple[str, ...],
    untrusted_prefixes: tuple[str, ...],
) -> tuple[bool, bool]:
    """(untrusted, private) for a path the model is about to read.

    Prefixes are vault-relative. A relative path is taken as vault-relative too. Anything that
    resolves outside the vault taints nothing, because the vault is all we classify.
    """
    root = Path(vault_root).resolve()
    candidate = Path(path)
    if not candidate.is_absolute():
        candidate = root / candidate
    try:
        relative = candidate.resolve().relative_to(root)
    except (OSError, ValueError):
        return (False, False)
    relative_path = relative.as_posix()
    return (
        _under(relative_path, untrusted_prefixes),
        _under(relative_path, private_prefixes),
    )


def _under(relative_path: str, prefixes: tuple[str, ...]) -> bool:
    """True when the path is the prefix directory itself or something inside it."""
    for prefix in prefixes:
        normalized = prefix.strip("/")
        if normalized and (relative_path == normalized or relative_path.startswith(normalized + "/")):
            return True
    return False
