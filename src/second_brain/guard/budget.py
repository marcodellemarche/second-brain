"""Per-session run budget: steps, wall clock, identical calls and empty searches.

Every tool call passes through here before it runs. Going over a limit blocks the call and tells
the model to stop and answer with what it has.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from second_brain.core.policy import PolicyConfig
from second_brain.guard.session import SessionStore


@dataclass(frozen=True)
class BudgetVerdict:
    ok: bool
    reason: str
    message_to_model: str = ""


def check_budget(
    store: SessionStore,
    sid: str,
    tool: str,
    args: dict[str, Any],
    policy: PolicyConfig,
    now: float,
    search_tools: frozenset[str],
) -> BudgetVerdict:
    """Count the call, then check every limit. The first one hit wins."""
    steps, started = store.next_step(sid)
    repeats = store.record_call(sid, tool, args)
    elapsed = now - started
    # A search still in flight counts as empty until it answers, or a batch of parallel searches
    # would all pass before the first result is seen.
    empty_searches = store.empty_searches(sid) + store.pending_searches(sid)

    if steps > policy.max_steps:
        return _stop(
            "max_steps",
            "The step budget for this task is used up. Do not call more tools: answer now with "
            "what you already have and say what is missing.",
        )
    if elapsed > policy.max_seconds:
        return _stop(
            "max_seconds",
            "The time budget for this task is used up. Do not call more tools: answer now with "
            "what you already have and say what is missing.",
        )
    if repeats > policy.max_repeat:
        return _stop(
            "max_repeat",
            "You already made this exact call. Do not repeat it: answer now with what you already have.",
        )
    if tool in search_tools and empty_searches >= policy.max_empty_searches:
        return _stop(
            "max_empty_searches",
            f"The last {empty_searches} searches found nothing new or have not answered yet. Stop "
            "searching: answer "
            "now with what you already have and say what you could not find.",
        )
    return BudgetVerdict(ok=True, reason="", message_to_model="")


def note_search_result(store: SessionStore, sid: str, new_items: int) -> None:
    """A search that found something new resets the streak, an empty one extends it."""
    store.note_search(sid, new_items)


def _stop(reason: str, message: str) -> BudgetVerdict:
    return BudgetVerdict(ok=False, reason=reason, message_to_model=message)
