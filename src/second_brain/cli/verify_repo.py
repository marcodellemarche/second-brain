"""`sb verify-repo`: the brain only ever writes inside its vault folder.

A brain's repository holds the instance code and, in one folder, the vault. The brain pushes with
its own author email, so any commit by that email outside the vault folder is a write that escaped
the tools. Run this before building and deploying: an offending commit stops the deploy.

The vault arrives through a subtree merge, and its own commits keep their trees without the
`vault/` prefix. Only the main line is walked: there the merge commit shows, against its first
parent, what it brought, already under `vault/`.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

FULL_SHA = re.compile(r"^[0-9a-f]{40}$")


def verify_repo(path: Path, prefix: str = "vault", author_email: str = "brain@localhost") -> list[str]:
    """Return "<short sha> <file>" for every file a commit by `author_email` touched outside `prefix/`."""
    if _git(path, "rev-parse", "--git-dir").returncode != 0:
        raise ValueError(f"{path} is not a git repository")
    result = _git(
        path,
        "log",
        f"--author={author_email}",
        "--first-parent",
        "--format=%H",
        "--name-only",
        "--no-renames",
        "--diff-merges=first-parent",
    )
    if result.returncode != 0:
        raise ValueError(f"cannot read the history of {path}: {result.stderr.strip()}")

    root = prefix.strip().strip("/")
    found: list[str] = []
    sha = ""
    for raw in result.stdout.splitlines():
        entry = raw.strip()
        if not entry:
            continue
        if FULL_SHA.match(entry):
            sha = entry[:8]
            continue
        if not _inside(entry, root):
            found.append(f"{sha} {entry}")
    return found


def _inside(entry: str, prefix: str) -> bool:
    return prefix in ("", ".") or entry.startswith(f"{prefix}/")


def _git(path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    # Unquoted paths: git otherwise escapes `lunedì.md` as `"luned\303\254.md"`, and a quoted
    # path never starts with the prefix.
    command = ["git", "-c", "core.quotePath=false", "-C", str(path), *args]
    return subprocess.run(command, capture_output=True, text=True, check=False)
