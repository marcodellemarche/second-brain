"""Strip attachment tags and filesystem paths from a reply before it reaches the chat.

Hermes uploads a file whenever the reply contains a `MEDIA:` tag or a bare absolute path.
Nothing in the reply needs either one to be useful, so both go, except for files inside the
media directory the tools are allowed to share and for chat commands like `/new`.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

_PATH_PART = r"""[^\s"'<>()\[\]{}`,;:|]+"""
# A media path may be quoted (it can contain spaces), a Hermes marker like [[as_document]],
# or a plain token.
_MEDIA_PART = r"""(?:"[^"]*"|'[^']*'|\[\[[^\]\s]*\]\]|""" + _PATH_PART + r""")"""

_REPLY_RE = re.compile(
    r"(?P<media>[ \t]*MEDIA:[ \t]*" + _MEDIA_PART + r")"
    r"|(?P<path>(?<![\w:/.\-])(?:/(?![\s/])|~/)" + _PATH_PART + r")"
)

_TRAILING = ".,;:!?"
_MEDIA_PREFIX = "MEDIA:"
PATH_PLACEHOLDER = "[path removed]"


def sanitize_reply(text: str, allowed_media_dir: str | None) -> tuple[str, list[str]]:
    """Return the reply with attachment tags and paths removed, plus what was removed."""
    root = _resolve(allowed_media_dir) if allowed_media_dir else None
    removed: list[str] = []

    def replace(match: re.Match[str]) -> str:
        tag = match.group("media")
        if tag is not None:
            if _inside(_media_path(tag), root):
                return match.group(0)
            removed.append(tag.strip())
            return ""

        raw = match.group("path")
        path = raw.rstrip(_TRAILING)
        if _inside(path, root) or _is_command(path):
            return match.group(0)
        removed.append(path)
        return PATH_PLACEHOLDER + raw[len(path) :]

    return _REPLY_RE.sub(replace, text), removed


def _is_command(path: str) -> bool:
    """A single segment without a dot (`/new`, `/undo`) is a chat command, not a path."""
    if not path.startswith("/"):
        return False
    segment = path[1:]
    return "/" not in segment and "." not in segment


def _media_path(tag: str) -> str:
    value = tag.strip()[len(_MEDIA_PREFIX) :].strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def _resolve(value: str | Path) -> Path | None:
    try:
        return Path(os.path.realpath(Path(value).expanduser()))
    except (OSError, RuntimeError, ValueError):
        return None


def _inside(path: str, root: Path | None) -> bool:
    """True when `path`, with `..` and symlinks resolved, stays under `root`."""
    if root is None or not path:
        return False
    # A relative path would be resolved against the working directory, which means nothing here.
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        return False
    target = _resolve(candidate)
    return target is not None and (target == root or root in target.parents)
