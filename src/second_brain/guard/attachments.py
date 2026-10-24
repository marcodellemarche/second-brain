"""Hand a chat attachment over to brain_tools before `archive_store` runs.

Hermes keeps what arrives in the chat in its own cache, owner-only, and brain_tools runs as another
user: it cannot read that cache. The guard runs inside Hermes, so it copies the one file the call
names into the attachment inbox, a volume Hermes writes and brain_tools mounts read-only. The name
is a bare file name, the copy never follows a link out of the cache, and old copies are dropped.
"""

from __future__ import annotations

import os
import shutil
import time
from pathlib import Path

INBOX_MAX_AGE_SECONDS = 7 * 24 * 3600


def stage_attachment(
    name: str, sources: tuple[str, ...], inbox: str, now: float | None = None
) -> Path | None:
    """Copy `name` from the first source folder that has it into `inbox`; `None` when none has it.

    A copy already in the inbox is left as it is. Nothing here raises for a bad or missing name:
    brain_tools answers that call with its own error.
    """
    if not _is_bare_name(name):
        return None
    target_dir = Path(inbox)
    if not target_dir.is_dir():
        return None
    _prune(target_dir, time.time() if now is None else now)
    target = target_dir / name
    if target.exists():
        return target
    for folder in sources:
        origin = _inside(Path(folder), name)
        if origin is None:
            continue
        partial = target_dir / f".{name}.partial"
        try:
            shutil.copyfile(origin, partial)
            os.chmod(partial, 0o644)
            os.replace(partial, target)
        except OSError:
            partial.unlink(missing_ok=True)
            return None
        return target
    return None


def _is_bare_name(name: str) -> bool:
    if not name or name != name.strip() or name in (".", ".."):
        return False
    return "/" not in name and "\\" not in name


def _inside(folder: Path, name: str) -> Path | None:
    try:
        root = folder.resolve(strict=True)
        candidate = (folder / name).resolve(strict=True)
    except OSError:
        return None
    if root not in candidate.parents or not candidate.is_file():
        return None
    return candidate


def _prune(inbox: Path, now: float) -> None:
    for entry in inbox.iterdir():
        try:
            if entry.is_file() and now - entry.stat().st_mtime > INBOX_MAX_AGE_SECONDS:
                entry.unlink()
        except OSError:
            continue
