"""Render the `{{ name }}` placeholders in a template.

Jinja is not a dependency here: the templates need substitution and one `{% for %}` block, and
nothing else. An unknown name, or a placeholder that survives rendering, is an error: a config
with `{{ owner_name }}` still in it would start and fail somewhere far from the answers file.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

_PLACEHOLDER = re.compile(r"\{\{\s*(\w+)\s*\}\}")
_LOOP = re.compile(r"\{%\s*for\s+(\w+)\s+in\s+(\w+)\s*%\}(.*?)\{%\s*endfor\s*%\}", re.DOTALL)
_LEFTOVER = re.compile(r"\{\{|\{%|\{#")


class TemplateError(RuntimeError):
    """A template asked for a value nobody provided, or left a placeholder behind."""


def render_text(text: str, values: Mapping[str, Any], *, source: str = "template") -> str:
    """Substitute every placeholder, expanding the loops first, and refuse anything left over."""

    def loop(match: re.Match[str]) -> str:
        item, collection, body = match.groups()
        entries = values.get(collection)
        if not isinstance(entries, Iterable) or isinstance(entries, (str, bytes)):
            raise TemplateError(f"{source}: {collection!r} is not a list to loop over")
        pattern = re.compile(r"\{\{\s*" + item + r"\s*\}\}")
        return "".join(pattern.sub(str(entry), body) for entry in entries)

    rendered = _LOOP.sub(loop, text)

    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in values:
            raise TemplateError(f"{source}: no value for {{{{{name}}}}}")
        return _as_text(values[name])

    rendered = _PLACEHOLDER.sub(replace, rendered)
    leftover = _LEFTOVER.search(rendered)
    if leftover is not None:
        raise TemplateError(f"{source}: a placeholder was left in the output at offset {leftover.start()}")
    return rendered


def _as_text(value: Any) -> str:
    if isinstance(value, (list, tuple)):
        return ", ".join(str(item) for item in value)
    return str(value)


def render_template(path: Path, values: Mapping[str, Any]) -> str:
    return render_text(path.read_text(encoding="utf-8"), values, source=str(path))
