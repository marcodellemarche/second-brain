"""The API an instance package builds on. Everything else in `second_brain` can change freely.

`API_VERSION` grows when a name here changes in a way an instance would notice. An instance calls
`require_api` with the version it was written for, so a brain built on a product it does not
match refuses to start instead of failing on the first call.
"""

from __future__ import annotations

from second_brain.core.schema import SchemaError, VaultSchema, load_schema
from second_brain.core.types import Autonomy, Effect, ToolSpec, Zone
from second_brain.sidecars.extension import SidecarContext, SidecarSpec
from second_brain.sidecars.webhook import HttpPost, WebhookError, send_event
from second_brain.tools import http
from second_brain.tools.registry import ExtensionError, ExtensionSettings, Tool
from second_brain.tools.secrets import redact, scan
from second_brain.tools.vault import Provenance, Vault, VaultConfig, VaultError

API_VERSION = 1

__all__ = [
    "API_VERSION",
    "Autonomy",
    "Effect",
    "ExtensionError",
    "ExtensionSettings",
    "HttpPost",
    "Provenance",
    "SchemaError",
    "SidecarContext",
    "SidecarSpec",
    "Tool",
    "ToolSpec",
    "Vault",
    "VaultConfig",
    "VaultError",
    "VaultSchema",
    "WebhookError",
    "Zone",
    "http",
    "load_schema",
    "redact",
    "require_api",
    "scan",
    "send_event",
]


def require_api(version: int) -> None:
    """Refuse to go on when this product does not offer the API the instance was written for."""
    if version != API_VERSION:
        raise ExtensionError(
            f"this instance needs version {version} of the second_brain.ext API, "
            f"the installed product offers version {API_VERSION}"
        )
