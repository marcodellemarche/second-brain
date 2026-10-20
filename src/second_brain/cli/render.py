"""Render the `{{ name }}` placeholders in a template.

Jinja is not a dependency here: the templates need substitution, one `{% for %}` block and one
`{% if %}` block, and nothing else. An unknown name, or a placeholder that survives rendering, is an
error: a config with `{{ owner_name }}` still in it would start and fail somewhere far from the
answers file.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

_PLACEHOLDER = re.compile(r"\{\{\s*(\w+)\s*\}\}")
_LOOP = re.compile(r"\{%\s*for\s+(\w+)\s+in\s+(\w+)\s*%\}(.*?)\{%\s*endfor\s*%\}", re.DOTALL)
# `{% if name %}...{% endif %}`, and `{% if name %}...{% else %}...{% endif %}`. One name, no
# operators: the templates decide between two blocks, they do not compute anything.
_CONDITION = re.compile(
    r"\{%\s*if\s+(\w+)\s*%\}(?P<yes>.*?)(?:\{%\s*else\s*%\}(?P<no>.*?))?\{%\s*endif\s*%\}",
    re.DOTALL,
)
_LEFTOVER = re.compile(r"\{\{|\{%|\{#")


class TemplateError(RuntimeError):
    """A template asked for a value nobody provided, or left a placeholder behind."""


def _truthy(value: Any) -> bool:
    """A name is true when it is there and not empty: an empty list or string is false."""
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, tuple, dict, set)):
        return bool(value)
    return bool(value)


def render_text(text: str, values: Mapping[str, Any], *, source: str = "template") -> str:
    """Substitute every placeholder, expanding the blocks first, and refuse anything left over."""

    def loop(match: re.Match[str]) -> str:
        item, collection, body = match.groups()
        entries = values.get(collection)
        if not isinstance(entries, Iterable) or isinstance(entries, (str, bytes)):
            raise TemplateError(f"{source}: {collection!r} is not a list to loop over")
        pattern = re.compile(r"\{\{\s*" + item + r"\s*\}\}")
        return "".join(pattern.sub(str(entry), body) for entry in entries)

    def condition(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in values:
            raise TemplateError(f"{source}: no value for {{% if {name} %}}")
        chosen = match.group("yes") if _truthy(values[name]) else (match.group("no") or "")
        return chosen

    # The conditions are expanded first and repeatedly, so a block can hold another one: the inner
    # text of a false branch is dropped whole, placeholders and loops included.
    for _ in range(10):
        rendered = _CONDITION.sub(condition, text)
        if rendered == text:
            break
        text = rendered
    else:  # pragma: no cover - a template nested deeper than ten levels is a mistake
        raise TemplateError(f"{source}: the conditional blocks nest too deeply")

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
