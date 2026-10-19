"""The decision function: effect, autonomy and session flags in, one action out."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from second_brain.core.types import Autonomy, Effect

Action = Literal["allow", "ask", "block"]


@dataclass(frozen=True)
class Decision:
    action: Action
    reason: str


def decide(
    effect: Effect,
    autonomy: Autonomy,
    *,
    untrusted: bool,
    private: bool,
    user_literal: bool,
) -> Decision:
    """First matching rule wins. `notify` and `auto` both allow here; the digest is elsewhere."""
    if autonomy is Autonomy.NEVER:
        return Decision("block", "tool is never allowed")
    if autonomy is Autonomy.ASK:
        return Decision("ask", "tool requires approval")
    if effect is Effect.EXTERNAL and untrusted and private and not user_literal:
        return Decision("ask", "rule of two")
    if effect is Effect.VISIBLE and untrusted:
        return Decision("ask", "visible action in a session that read untrusted content")
    if effect is Effect.VISIBLE and not user_literal:
        return Decision("ask", "visible action the user did not ask for")
    return Decision("allow", "no rule applies")
