import threading

import pytest

from second_brain.guard.session import SessionState, SessionStore


def test_get_creates_state_once() -> None:
    store = SessionStore()
    state = store.get("s1")
    assert isinstance(state, SessionState)
    assert state.sid == "s1"
    assert state.sender == ""
    assert state.platform == ""
    assert state.parent is None
    assert state.untrusted_seen is False
    assert state.private_seen is False
    assert state.known_urls == set()
    assert state.steps == 0
    assert state.call_counts == {}
    assert state.empty_searches == 0
    assert store.get("s1") is state


def test_root_follows_parent_chain() -> None:
    store = SessionStore()
    store.link_child("child", "middle")
    store.link_child("middle", "root")
    assert store.root("child").sid == "root"
    assert store.root("root").sid == "root"


def test_flag_set_on_parent_after_child_creation_is_visible_in_child() -> None:
    store = SessionStore()
    store.link_child("child", "parent")
    assert store.flags("child") == (False, False)

    store.mark_untrusted("parent")
    store.mark_private("parent")

    assert store.flags("child") == (True, True)
    assert store.get("child").untrusted_seen is False


def test_flags_or_over_the_whole_chain() -> None:
    store = SessionStore()
    store.link_child("child", "parent")
    store.mark_untrusted("child")
    store.mark_private("parent")
    assert store.flags("child") == (True, True)
    assert store.flags("parent") == (False, True)


def test_cycle_is_safe() -> None:
    store = SessionStore()
    store.link_child("a", "b")
    store.link_child("b", "a")
    store.mark_untrusted("b")

    assert store.root("a").sid in {"a", "b"}
    assert store.flags("a") == (True, False)
    assert store.sender("a") == ""


def test_self_cycle_is_safe() -> None:
    store = SessionStore()
    store.link_child("a", "a")
    assert store.root("a").sid == "a"
    assert store.flags("a") == (False, False)


def test_sender_is_inherited_from_the_chain() -> None:
    store = SessionStore()
    store.link_child("child", "parent")
    store.set_identity("parent", sender="owner", platform="telegram")

    assert store.sender("child") == "owner"

    store.set_identity("child", sender="someone-else")
    assert store.sender("child") == "someone-else"
    assert store.sender("parent") == "owner"


def test_set_identity_records_sender_and_platform() -> None:
    store = SessionStore()
    store.set_identity("s1", sender="owner", platform="telegram")
    state = store.get("s1")
    assert state.sender == "owner"
    assert state.platform == "telegram"


def test_set_identity_does_not_clear_what_is_known() -> None:
    store = SessionStore()
    store.set_identity("s1", sender="owner", platform="telegram")
    store.set_identity("s1")
    store.set_identity("s1", sender="", platform="")
    state = store.get("s1")
    assert state.sender == "owner"
    assert state.platform == "telegram"


def test_set_identity_overwrites_one_field_at_a_time() -> None:
    store = SessionStore()
    store.set_identity("s1", sender="owner", platform="telegram")
    store.set_identity("s1", platform="slack")
    state = store.get("s1")
    assert state.sender == "owner"
    assert state.platform == "slack"


def test_set_identity_is_thread_safe() -> None:
    store = SessionStore()
    threads = [
        threading.Thread(target=store.set_identity, args=("s1", f"user-{index}", "telegram"))
        for index in range(8)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert store.get("s1").sender.startswith("user-")
    assert store.get("s1").platform == "telegram"


def test_sender_of_unknown_session_is_empty() -> None:
    assert SessionStore().sender("nobody") == ""


def test_record_call_counts_identical_calls() -> None:
    store = SessionStore()
    assert store.record_call("s1", "weather", {"place": "Rome", "days": 2}) == 1
    assert store.record_call("s1", "weather", {"days": 2, "place": "Rome"}) == 2
    assert store.record_call("s1", "weather", {"place": "Rome", "days": 3}) == 1
    assert store.record_call("s1", "calendar", {"place": "Rome", "days": 2}) == 1
    assert store.record_call("s2", "weather", {"place": "Rome", "days": 2}) == 1


def test_record_call_is_per_session_not_per_chain() -> None:
    store = SessionStore()
    store.link_child("child", "parent")
    assert store.record_call("parent", "weather", {"place": "Rome"}) == 1
    assert store.record_call("child", "weather", {"place": "Rome"}) == 1


def test_known_urls_are_inherited() -> None:
    store = SessionStore()
    store.link_child("child", "parent")
    store.add_known_urls("parent", {"https://example.com/a"})

    assert store.knows_url("parent", "https://example.com/a") is True
    assert store.knows_url("child", "https://example.com/a") is True
    assert store.knows_url("child", "https://example.com/b") is False

    store.add_known_urls("child", ["https://example.com/b"])
    assert store.knows_url("parent", "https://example.com/b") is False


def test_forget_drops_only_that_session() -> None:
    store = SessionStore()
    store.link_child("child", "parent")
    store.mark_untrusted("parent")
    store.forget("parent")

    # The child keeps the parent id; reading it again creates a clean state.
    assert store.flags("parent") == (False, False)
    assert store.root("child").sid == "parent"
    assert store.flags("child") == (False, False)
    assert store.sender("child") == ""


def test_thread_safety_smoke() -> None:
    store = SessionStore()
    store.link_child("child", "parent")
    threads_count = 16
    iterations = 200

    def worker() -> None:
        for _ in range(iterations):
            store.mark_untrusted("parent")
            store.mark_private("child")
            store.get("parent")
            store.record_call("parent", "weather", {"place": "Rome"})
            store.flags("child")

    threads = [threading.Thread(target=worker) for _ in range(threads_count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert store.flags("child") == (True, True)
    assert store.record_call("parent", "weather", {"place": "Rome"}) == threads_count * iterations + 1


@pytest.mark.parametrize("sid", ["", "?"])
def test_odd_session_ids_are_accepted(sid: str) -> None:
    store = SessionStore()
    assert store.get(sid).sid == sid
