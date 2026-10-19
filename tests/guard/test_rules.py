from dataclasses import FrozenInstanceError

import pytest

from second_brain.core.types import Autonomy, Effect
from second_brain.guard.rules import Decision, decide

VISIBLE_TAINTED = "visible action in a session that read untrusted content"

# (effect, autonomy, untrusted, private, user_literal) -> (action, reason)
CASES: list[tuple[Effect, Autonomy, bool, bool, bool, str, str]] = [
    # never wins over everything
    (Effect.READ, Autonomy.NEVER, False, False, True, "block", "tool is never allowed"),
    (Effect.EXTERNAL, Autonomy.NEVER, True, True, True, "block", "tool is never allowed"),
    # ask wins over the rule of two
    (Effect.READ, Autonomy.ASK, False, False, True, "ask", "tool requires approval"),
    (Effect.EXTERNAL, Autonomy.ASK, True, True, False, "ask", "tool requires approval"),
    # rule of two: external, untrusted and private, not literal from the user
    (Effect.EXTERNAL, Autonomy.NOTIFY, True, True, False, "ask", "rule of two"),
    (Effect.EXTERNAL, Autonomy.AUTO, True, True, False, "ask", "rule of two"),
    # the literal URL or question from the user is the exception
    (Effect.EXTERNAL, Autonomy.AUTO, True, True, True, "allow", "no rule applies"),
    # rule of two needs both flags
    (Effect.EXTERNAL, Autonomy.AUTO, True, False, False, "allow", "no rule applies"),
    (Effect.EXTERNAL, Autonomy.AUTO, False, True, False, "allow", "no rule applies"),
    (Effect.EXTERNAL, Autonomy.AUTO, False, False, False, "allow", "no rule applies"),
    # visible: untrusted always asks, even when the user asked for it
    (Effect.VISIBLE, Autonomy.AUTO, True, False, True, "ask", VISIBLE_TAINTED),
    (Effect.VISIBLE, Autonomy.AUTO, True, True, False, "ask", VISIBLE_TAINTED),
    # visible: not asked for by the user
    (Effect.VISIBLE, Autonomy.NOTIFY, False, False, False, "ask", "visible action the user did not ask for"),
    (Effect.VISIBLE, Autonomy.AUTO, False, True, False, "ask", "visible action the user did not ask for"),
    # visible, clean session, asked by the user
    (Effect.VISIBLE, Autonomy.AUTO, False, False, True, "allow", "no rule applies"),
    (Effect.VISIBLE, Autonomy.NOTIFY, False, True, True, "allow", "no rule applies"),
    # read and internal are never affected by the flags
    (Effect.READ, Autonomy.AUTO, True, True, False, "allow", "no rule applies"),
    (Effect.INTERNAL, Autonomy.AUTO, True, True, False, "allow", "no rule applies"),
    (Effect.INTERNAL, Autonomy.NOTIFY, False, False, False, "allow", "no rule applies"),
]


@pytest.mark.parametrize(
    ("effect", "autonomy", "untrusted", "private", "user_literal", "action", "reason"),
    CASES,
)
def test_decide(
    effect: Effect,
    autonomy: Autonomy,
    untrusted: bool,
    private: bool,
    user_literal: bool,
    action: str,
    reason: str,
) -> None:
    decision = decide(
        effect,
        autonomy,
        untrusted=untrusted,
        private=private,
        user_literal=user_literal,
    )
    assert decision == Decision(action=action, reason=reason)


def test_decision_is_frozen() -> None:
    decision = decide(Effect.READ, Autonomy.AUTO, untrusted=False, private=False, user_literal=False)
    with pytest.raises(FrozenInstanceError):
        decision.action = "block"  # type: ignore[misc]


@pytest.mark.parametrize("effect", list(Effect))
@pytest.mark.parametrize("autonomy", list(Autonomy))
def test_every_combination_returns_a_decision(effect: Effect, autonomy: Autonomy) -> None:
    for untrusted in (False, True):
        for private in (False, True):
            for user_literal in (False, True):
                decision = decide(
                    effect,
                    autonomy,
                    untrusted=untrusted,
                    private=private,
                    user_literal=user_literal,
                )
                assert decision.action in {"allow", "ask", "block"}
                assert decision.reason
