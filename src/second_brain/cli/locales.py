"""Hermes' own approval strings, overridden for a brain without touching Hermes.

Hermes reads `$HERMES_HOME/locales/<language>.yaml` on top of its bundled catalog, so this file
changes the header, the labels and the outcome lines of the approval card. brain_guard binds
every approval to one tool call, so the session and the permanent buttons approve exactly what
the first one does: they cannot be hidden from here, so their labels say so.
"""

from __future__ import annotations

from typing import Any

import yaml

LOCALES_DIR = "config/hermes/locales"

_TEXTS: dict[str, dict[str, str]] = {
    "en": {
        "header": "Approval needed",
        "reason": "What",
        "approve": "Approve",
        "same": "Same as Approve",
        "deny": "Deny",
        "deadline": "If nobody answers within {window}, it does not run.",
        "timed_out": "⌛ Nobody answered within {window}, so it did not run. Ask again if you still want it.",
        "approved_by": "✅ Approved by {user}",
        "denied_by": "❌ Denied by {user}",
        "approved": "✅ Approved",
        "denied": "❌ Denied",
    },
    "it": {
        "header": "Serve la tua approvazione",
        "reason": "Cosa",
        "approve": "Approva",
        "same": "Come Approva",
        "deny": "Nega",
        "deadline": "Se nessuno risponde entro {window}, non parte.",
        "timed_out": (
            "⌛ Nessuna risposta entro {window}, quindi non è partito. Chiedimelo di nuovo se lo vuoi ancora."
        ),
        "approved_by": "✅ Approvato da {user}",
        "denied_by": "❌ Negato da {user}",
        "approved": "✅ Approvato",
        "denied": "❌ Negato",
    },
}


def locale_overlay(language: str) -> dict[str, Any]:
    """The catalog keys this brain overrides, nested the way Hermes' catalogs are."""
    text = _TEXTS.get(language, _TEXTS["en"])
    return {
        "gateway": {
            "exec_approval": {
                "header": text["header"],
                "reason_label": text["reason"],
                "action_once": text["approve"],
                "action_session": text["same"],
                "action_always": text["same"],
                "action_deny": text["deny"],
                "deadline": text["deadline"],
                "timed_out_notice": text["timed_out"],
            }
        },
        "platform": {
            "slack": {
                "approval": {
                    "resolved_once": text["approved_by"],
                    "resolved_session": text["approved_by"],
                    "resolved_always": text["approved_by"],
                    "resolved_deny": text["denied_by"],
                }
            },
            "telegram": {
                "approval": {
                    "action_once": f"✅ {text['approve']}",
                    "action_session": text["same"],
                    "action_always": text["same"],
                    "action_deny": f"❌ {text['deny']}",
                    "resolved_once": text["approved"],
                    "resolved_session": text["approved"],
                    "resolved_always": text["approved"],
                    "resolved_deny": text["denied"],
                }
            },
        },
    }


def render_locale(language: str) -> dict[str, str]:
    """`{instance path: content}` for the overlay of the brain's language."""
    code = language if language in _TEXTS else "en"
    content = yaml.safe_dump(
        locale_overlay(code), sort_keys=True, allow_unicode=True, default_flow_style=False
    )
    return {f"{LOCALES_DIR}/{code}.yaml": content}
