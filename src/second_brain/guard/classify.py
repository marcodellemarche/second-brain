"""What a tool does, how autonomous it is by default, what its result taints, and which shell
commands never run.

brain_guard sees every Hermes tool call, built-in or MCP. This module is the single table that
answers those questions for each one. Anything not in the table fails closed, and the terminal
rules are an allowlist of nothing: a command that installs software or pipes a download into a
shell is refused before Hermes ever looks at it.
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass
from pathlib import Path

from second_brain.core.types import Autonomy, Effect

MCP_PREFIX = "mcp__brain__"

TERMINAL_DENIED_MESSAGE = (
    "Blocked: {reason}. Installing software and running remote scripts are not allowed here. "
    "Use the tools you already have."
)


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


# --- terminal commands ---------------------------------------------------------------------

_CHAIN_SEPARATORS = frozenset({";", "&&", "||", "&", "|&"})
_PIPE = "|"
_FETCHERS = frozenset({"curl", "wget"})
_SHELLS = frozenset({"sh", "bash", "zsh", "dash", "ksh"})
_WRAPPERS = frozenset({"sudo", "env", "command", "nohup", "exec"})
# Wrapper options that take the next token as their value, so `sudo -u bob pip` still sees pip.
_WRAPPER_VALUES = frozenset({"-u", "--user", "-g", "--group", "-C", "--chdir", "-S", "--split-string"})
_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_PIP = re.compile(r"^pip[0-9.]*$")
_PYTHON = re.compile(r"^python[0-9.]*$")
_MAX_SHELL_DEPTH = 3

_INSTALL_VERBS: dict[str, frozenset[str]] = {
    "npm": frozenset({"install", "i", "add", "ci"}),
    "pnpm": frozenset({"add", "install", "i"}),
    "yarn": frozenset({"add", "install"}),
    "apt": frozenset({"install"}),
    "apt-get": frozenset({"install"}),
    "apk": frozenset({"add"}),
    "gem": frozenset({"install"}),
    "cargo": frozenset({"install"}),
}


def terminal_denied(command: str) -> str | None:
    """Why this terminal command must not run, or None when the guard has nothing against it.

    The command is normalized first, then read as the shell would read it: chains are split on
    `;`, `&&` and `&`, pipelines on `|`, and each part is checked on its own, so `cd /tmp &&
    sudo pip install x` is the same as `pip install x`.
    """
    if not isinstance(command, str):
        return None
    return _denied(_tokens(_normalize(command)), 0)


def _denied(tokens: list[str], depth: int) -> str | None:
    for command in _commands(tokens):
        reason = _command_denied(command, depth)
        if reason is not None:
            return reason
    return None


def _command_denied(tokens: list[str], depth: int) -> str | None:
    stages = _stages(tokens)
    if not stages:
        return None
    fetcher = _head(stages[0])
    if fetcher in _FETCHERS and len(stages) > 1:
        for stage in stages[1:]:
            shell = _head(stage)
            if shell in _SHELLS:
                return f"remote script execution ({fetcher} | {shell})"
    for stage in stages:
        words = _strip_prefix(stage)
        if not words:
            continue
        name = _basename(words[0])
        reason = _install_denied(name, words[1:])
        if reason is not None:
            return reason
        if name in _SHELLS and depth < _MAX_SHELL_DEPTH:
            script = _shell_script(words[1:])
            if script:
                reason = _denied(_tokens(_normalize(script)), depth + 1)
                if reason is not None:
                    return reason
    return None


def _head(tokens: list[str]) -> str:
    """The command name a stage starts with, once the wrappers are gone."""
    words = _strip_prefix(tokens)
    return _basename(words[0]) if words else ""


def _install_denied(name: str, args: list[str]) -> str | None:
    """The reason a command installs software, or None when it does not."""
    verb = _first_word(args)
    if _PIP.match(name):
        return _install(f"{name} install") if verb == "install" else None
    if _PYTHON.match(name):
        # Only `python -m pip` is a package install; the rest of python is not the guard's business.
        if len(args) >= 2 and args[0] == "-m" and _PIP.match(args[1]):
            return _install(f"{name} -m pip")
        return None
    if name == "uv":
        if verb in {"pip", "add"}:
            return _install(f"uv {verb}")
        return None
    verbs = _INSTALL_VERBS.get(name)
    if verbs is not None and verb is not None and verb in verbs:
        return _install(f"{name} {verb}")
    return None


def _install(label: str) -> str:
    return f"package install ({label})"


def _normalize(command: str) -> str:
    """One line with single spaces: newlines become `;`, which is what they are to a shell."""
    return " ".join(re.sub(r"\n+", " ; ", command.replace("\r", "\n")).split())


def _tokens(text: str) -> list[str]:
    """Shell words, with the operators kept as words. Unbalanced quotes fall back to plain split."""
    lexer = shlex.shlex(text, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    try:
        return list(lexer)
    except ValueError:
        return text.split()


def _commands(tokens: list[str]) -> list[list[str]]:
    """The commands of a chain, each pipeline whole."""
    commands: list[list[str]] = []
    current: list[str] = []
    for token in tokens:
        if token in _CHAIN_SEPARATORS:
            if current:
                commands.append(current)
            current = []
        else:
            current.append(token)
    if current:
        commands.append(current)
    return commands


def _stages(tokens: list[str]) -> list[list[str]]:
    """The stages of a pipeline."""
    stages: list[list[str]] = []
    current: list[str] = []
    for token in tokens:
        if token == _PIPE:
            if current:
                stages.append(current)
            current = []
        else:
            current.append(token)
    if current:
        stages.append(current)
    return stages


def _strip_prefix(tokens: list[str]) -> list[str]:
    """Drop `sudo`, `env VAR=x` and the like, so the command itself is what is left."""
    rest = list(tokens)
    while rest:
        if _ASSIGNMENT.match(rest[0]):
            rest.pop(0)
            continue
        if _basename(rest[0]) not in _WRAPPERS:
            break
        rest.pop(0)
        while rest and rest[0].startswith("-"):
            takes_value = rest[0] in _WRAPPER_VALUES
            rest = rest[2:] if takes_value else rest[1:]
    return rest


def _shell_script(args: list[str]) -> str | None:
    """The script a shell was told to run with -c, including combined flags like -lc."""
    for index, token in enumerate(args):
        if not token.startswith("-") or token == "--":
            return None
        if not token.startswith("--") and token.endswith("c"):
            return args[index + 1] if index + 1 < len(args) else None
    return None


def _first_word(args: list[str]) -> str | None:
    """The first argument that is not an option."""
    for word in args:
        if not word.startswith("-"):
            return word
    return None


def _basename(token: str) -> str:
    """A command name without the path it was called with."""
    return token.rsplit("/", 1)[-1]


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
