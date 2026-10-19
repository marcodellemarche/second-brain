"""The tool table brain_guard decides on, exported from the tools themselves.

`mcp_tools` in the guard config is what `classify` looks a brain_tools call up in. Writing it by
hand means two places can disagree about a tool, so it is generated here from the registry the
server actually serves:

    python -m second_brain.tools.export tools.yaml > guard-tools.yaml

The output is the `mcp_tools` mapping, so it goes under that key of the guard config as it is.
"""

from __future__ import annotations

import sys
from typing import Any

import yaml

from second_brain.tools.config import ToolsConfig, build_registry
from second_brain.tools.registry import Registry


def export_guard_classes(registry: Registry) -> dict[str, dict[str, Any]]:
    """Effect, autonomy and taints of every registered tool, keyed by name without the prefix."""
    return {
        spec.name: {
            "effect": spec.effect.value,
            "autonomy": spec.autonomy.value,
            "taints_untrusted": spec.taints_untrusted,
            "taints_private": spec.taints_private,
        }
        for spec in registry.specs()
    }


def render(classes: dict[str, dict[str, Any]]) -> str:
    return yaml.safe_dump(classes, sort_keys=True, default_flow_style=False)


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        print("usage: python -m second_brain.tools.export <tools-config.yaml>", file=sys.stderr)
        return 2
    registry = build_registry(ToolsConfig.from_yaml(args[0]))
    sys.stdout.write(render(export_guard_classes(registry)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
