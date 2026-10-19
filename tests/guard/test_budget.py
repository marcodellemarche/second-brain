from dataclasses import FrozenInstanceError

import pytest

from second_brain.core.policy import PolicyConfig
from second_brain.guard.budget import BudgetVerdict, check_budget, note_search_result
from second_brain.guard.session import SessionStore

SEARCHES = frozenset({"web_search", "search_files", "session_search"})


def check(
    store: SessionStore,
    *,
    sid: str = "s1",
    tool: str = "read_file",
    args: dict | None = None,
    policy: PolicyConfig | None = None,
    now: float = 0.0,
) -> BudgetVerdict:
    return check_budget(store, sid, tool, args or {}, policy or PolicyConfig(), now, SEARCHES)


def test_verdict_is_frozen() -> None:
    verdict = BudgetVerdict(ok=True, reason="")
    assert verdict.message_to_model == ""
    with pytest.raises(FrozenInstanceError):
        verdict.ok = False  # type: ignore[misc]


def test_call_within_budget_is_ok_and_counted() -> None:
    store = SessionStore()
    verdict = check(store)
    assert verdict == BudgetVerdict(ok=True, reason="", message_to_model="")
    assert store.get("s1").steps == 1
    assert check(store).ok
    assert store.get("s1").steps == 2


def test_steps_over_the_limit() -> None:
    store = SessionStore()
    policy = PolicyConfig(max_steps=2)
    assert check(store, policy=policy).ok
    assert check(store, policy=policy).ok
    verdict = check(store, policy=policy)
    assert not verdict.ok
    assert verdict.reason == "max_steps"
    assert "answer now" in verdict.message_to_model
    assert store.get("s1").steps == 3


def test_steps_are_counted_even_when_blocked() -> None:
    store = SessionStore()
    policy = PolicyConfig(max_steps=1)
    assert check(store, policy=policy).ok
    assert not check(store, policy=policy).ok
    assert store.get("s1").steps == 2


def test_elapsed_over_the_limit() -> None:
    store = SessionStore()
    store.get("s1").started = 0.0
    policy = PolicyConfig(max_seconds=10)
    assert check(store, policy=policy, now=10.0).ok
    verdict = check(store, policy=policy, now=10.5)
    assert not verdict.ok
    assert verdict.reason == "max_seconds"
    assert "answer now" in verdict.message_to_model


def test_elapsed_uses_the_passed_now() -> None:
    store = SessionStore()
    store.get("s1").started = 100.0
    assert check(store, policy=PolicyConfig(max_seconds=10), now=105.0).ok


def test_identical_call_repeated_too_often() -> None:
    store = SessionStore()
    policy = PolicyConfig(max_repeat=2)
    args = {"path": "Topics/note.md"}
    assert check(store, args=args, policy=policy).ok
    assert check(store, args=args, policy=policy).ok
    verdict = check(store, args=args, policy=policy)
    assert not verdict.ok
    assert verdict.reason == "max_repeat"
    assert "already have" in verdict.message_to_model


def test_repeat_counts_per_arguments_and_tool() -> None:
    store = SessionStore()
    policy = PolicyConfig(max_repeat=1)
    assert check(store, args={"q": "a"}, policy=policy).ok
    assert check(store, args={"q": "b"}, policy=policy).ok
    assert check(store, tool="search_files", args={"q": "a"}, policy=policy).ok
    assert not check(store, args={"q": "a"}, policy=policy).ok


def test_repeat_ignores_argument_key_order() -> None:
    store = SessionStore()
    policy = PolicyConfig(max_repeat=1)
    assert check(store, args={"a": 1, "b": 2}, policy=policy).ok
    assert not check(store, args={"b": 2, "a": 1}, policy=policy).ok


def test_empty_searches_over_the_limit_on_a_search_tool() -> None:
    store = SessionStore()
    policy = PolicyConfig(max_empty_searches=2)
    store.get("s1").empty_searches = 2
    verdict = check(store, tool="web_search", policy=policy)
    assert not verdict.ok
    assert verdict.reason == "max_empty_searches"
    assert "Stop searching" in verdict.message_to_model


def test_empty_searches_do_not_block_a_non_search_tool() -> None:
    store = SessionStore()
    store.get("s1").empty_searches = 99
    assert check(store, tool="read_file").ok


def test_search_still_allowed_below_the_limit() -> None:
    store = SessionStore()
    store.get("s1").empty_searches = 2
    assert check(store, tool="web_search", policy=PolicyConfig(max_empty_searches=3)).ok


def test_note_search_result_resets_on_new_items() -> None:
    store = SessionStore()
    store.get("s1").empty_searches = 2
    note_search_result(store, "s1", 3)
    assert store.get("s1").empty_searches == 0


def test_note_search_result_increments_on_empty() -> None:
    store = SessionStore()
    note_search_result(store, "s1", 0)
    assert store.get("s1").empty_searches == 1
    note_search_result(store, "s1", 0)
    assert store.get("s1").empty_searches == 2


def test_budget_is_per_session() -> None:
    store = SessionStore()
    policy = PolicyConfig(max_steps=1)
    assert check(store, sid="a", policy=policy).ok
    assert not check(store, sid="a", policy=policy).ok
    assert check(store, sid="b", policy=policy).ok
