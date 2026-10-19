"""Tests for the proactive notification budget, its deferred queue and the feedback block."""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
import yaml
from pydantic import ValidationError

from second_brain.core.types import Autonomy, Effect
from second_brain.tools.notify import (
    DEFAULT_DAILY,
    MAX_TEXT,
    Budget,
    Counts,
    DeferredParams,
    FeedbackParams,
    InitiativeFile,
    Notifier,
    NotifyConfig,
    NotifyParams,
    NotifyStore,
    decide,
    notify_tools,
    render_block,
)
from second_brain.tools.registry import Tool
from second_brain.tools.vault import Provenance

ROME = ZoneInfo("Europe/Rome")
INITIATIVE = "_brain/initiative.md"
# An ordinary morning in Rome, used as "now" by every test that does not move the clock.
MORNING = datetime(2024, 5, 1, 9, 0, tzinfo=ROME).timestamp()


class ManualClock:
    """A clock that only moves when the test moves it."""

    def __init__(self, now: float = MORNING) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _provenance(**overrides: object) -> Provenance:
    values: dict[str, object] = {
        "requested_by": "user-1",
        "reason": "test call",
        "source_integrity": "trusted",
        "session": "session-1",
    }
    values.update(overrides)
    return Provenance(**values)  # type: ignore[arg-type]


@pytest.fixture
def vault_root(tmp_path: Path) -> Path:
    root = tmp_path / "vault"
    (root / "_brain").mkdir(parents=True)
    return root


@pytest.fixture
def clock() -> ManualClock:
    return ManualClock()


def _config(vault_root: Path, tmp_path: Path, **overrides: Any) -> NotifyConfig:
    values: dict[str, Any] = {
        "store_path": tmp_path / "state" / "notify.sqlite3",
        "initiative_path": INITIATIVE,
        "timezone": "Europe/Rome",
    }
    values.update(overrides)
    return NotifyConfig(**values)


@pytest.fixture
def notifier(vault_root: Path, tmp_path: Path, clock: ManualClock) -> Notifier:
    return Notifier(_config(vault_root, tmp_path), vault_root, clock=clock)


@pytest.fixture
def tools(vault_root: Path, tmp_path: Path, clock: ManualClock) -> dict[str, Tool]:
    return {
        tool.spec.name: tool for tool in notify_tools(_config(vault_root, tmp_path), vault_root, clock=clock)
    }


def _write_initiative(vault_root: Path, total: int, categories: dict[str, int] | None = None) -> None:
    block = render_block(total, categories or {})
    note = f"# Initiative\n\nHow often I may speak first.\n\n{block}\n\nAnything else is a note to myself.\n"
    (vault_root / INITIATIVE).write_text(note, encoding="utf-8")


def _call(tools: dict[str, Tool], name: str, params: dict[str, Any], **prov: object) -> dict[str, Any]:
    tool = tools[name]
    parsed = tool.spec.params.model_validate(params)
    return tool.handler(parsed, _provenance(**prov))


def _notify(tools: dict[str, Tool], category: str = "ideas", urgent: bool = False) -> dict[str, Any]:
    return _call(tools, "notify", {"category": category, "text": "Something worth saying.", "urgent": urgent})


# --- the specs -----------------------------------------------------------------------------


def test_there_are_three_tools(tools: dict[str, Tool]) -> None:
    assert set(tools) == {"notify", "deferred_notifications", "notify_feedback"}


def test_notify_is_visible_and_auto(tools: dict[str, Tool]) -> None:
    spec = tools["notify"].spec
    assert (spec.effect, spec.autonomy) == (Effect.VISIBLE, Autonomy.AUTO)
    assert spec.hosts == ()
    assert spec.credentials == ()


def test_the_other_two_tools_only_read(tools: dict[str, Tool]) -> None:
    for name in ("deferred_notifications", "notify_feedback"):
        assert tools[name].spec.effect is Effect.READ


def test_notify_takes_a_category_a_text_and_urgency(tools: dict[str, Tool]) -> None:
    assert tools["notify"].spec.params is NotifyParams
    assert set(NotifyParams.model_fields) == {"category", "text", "urgent"}


def test_the_text_has_a_limit() -> None:
    NotifyParams(category="ideas", text="x" * MAX_TEXT)
    with pytest.raises(ValidationError):
        NotifyParams(category="ideas", text="x" * (MAX_TEXT + 1))


def test_an_empty_text_is_refused() -> None:
    with pytest.raises(ValidationError):
        NotifyParams(category="ideas", text="   ")


@pytest.mark.parametrize("category", ["", "  ", "two words", "con accento", "x" * 33, "-lead"])
def test_a_category_is_a_short_name(category: str) -> None:
    with pytest.raises(ValidationError):
        NotifyParams(category=category, text="Hello.")


def test_a_category_is_folded_to_lowercase() -> None:
    assert NotifyParams(category=" DeadLine ", text="Hello.").category == "deadline"


def test_a_zero_delta_is_refused() -> None:
    with pytest.raises(ValidationError):
        FeedbackParams(category="ideas", delta=0)


# --- the config ----------------------------------------------------------------------------


def test_the_default_daily_is_six() -> None:
    assert DEFAULT_DAILY == 6
    config = NotifyConfig(
        store_path=Path("/tmp/x.sqlite3"), initiative_path=INITIATIVE, timezone="Europe/Rome"
    )
    assert config.default_daily == 6


def test_the_initiative_path_stays_in_the_vault(tmp_path: Path) -> None:
    for path in ("../outside.md", "/etc/initiative.md", "_brain/../../x.md", "_brain//x.md", "x.txt"):
        with pytest.raises(ValidationError):
            NotifyConfig(store_path=tmp_path / "x.sqlite3", initiative_path=path, timezone="Europe/Rome")


def test_an_unknown_timezone_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ValidationError):
        NotifyConfig(store_path=tmp_path / "x.sqlite3", initiative_path=INITIATIVE, timezone="Mars/Olympus")


def test_an_unknown_key_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ValidationError):
        NotifyConfig(
            store_path=tmp_path / "x.sqlite3",
            initiative_path=INITIATIVE,
            timezone="Europe/Rome",
            quiet_hours="22-8",
        )


def test_a_negative_default_daily_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ValidationError):
        NotifyConfig(
            store_path=tmp_path / "x.sqlite3",
            initiative_path=INITIATIVE,
            timezone="Europe/Rome",
            default_daily=-1,
        )


# --- the budget note -----------------------------------------------------------------------


def test_the_budget_is_read_from_the_fenced_block(vault_root: Path) -> None:
    _write_initiative(vault_root, 4, {"deadline": 2, "ideas": 1})
    budget = InitiativeFile(vault_root / INITIATIVE, DEFAULT_DAILY).read()
    assert budget is not None
    assert budget.total == 4
    assert budget.categories == {"deadline": 2, "ideas": 1}


def test_a_budget_without_categories_is_allowed(vault_root: Path) -> None:
    (vault_root / INITIATIVE).write_text("```yaml\ntotal: 3\n```\n", encoding="utf-8")
    budget = InitiativeFile(vault_root / INITIATIVE, DEFAULT_DAILY).read()
    assert budget is not None
    assert (budget.total, budget.categories) == (3, {})


def test_a_missing_file_falls_back_to_the_default(vault_root: Path, caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING):
        budget = InitiativeFile(vault_root / INITIATIVE, DEFAULT_DAILY).budget()
    assert budget.total == DEFAULT_DAILY
    assert budget.categories == {}
    assert "cannot be read" in caplog.text


def test_a_file_without_a_block_falls_back_to_the_default(
    vault_root: Path, caplog: pytest.LogCaptureFixture
) -> None:
    (vault_root / INITIATIVE).write_text("# Initiative\n\nNo block here.\n", encoding="utf-8")
    with caplog.at_level(logging.WARNING):
        budget = InitiativeFile(vault_root / INITIATIVE, DEFAULT_DAILY).budget()
    assert budget.total == DEFAULT_DAILY
    assert "no fenced YAML block" in caplog.text


@pytest.mark.parametrize(
    "block",
    [
        "```yaml\ntotal: not a number\n```\n",
        "```yaml\ncategories:\n  ideas: 2\n```\n",
        "```yaml\ntotal: -1\n```\n",
        "```yaml\ntotal: 3\ncategories:\n  ideas: lots\n```\n",
        "```yaml\ntotal: 3\nextra: 1\n```\n",
        "```yaml\ntotal: [1, 2\n```\n",
        "```yaml\n- just\n- a list\n```\n",
    ],
)
def test_an_invalid_block_falls_back_to_the_default(
    vault_root: Path, caplog: pytest.LogCaptureFixture, block: str
) -> None:
    (vault_root / INITIATIVE).write_text(block, encoding="utf-8")
    with caplog.at_level(logging.WARNING):
        budget = InitiativeFile(vault_root / INITIATIVE, DEFAULT_DAILY).budget()
    assert budget.total == DEFAULT_DAILY
    assert budget.categories == {}
    assert "is not a valid budget" in caplog.text


def test_the_block_is_found_inside_a_longer_note(vault_root: Path) -> None:
    _write_initiative(vault_root, 5, {"ideas": 2})
    content = (vault_root / INITIATIVE).read_text(encoding="utf-8")
    assert content.startswith("# Initiative")
    assert InitiativeFile(vault_root / INITIATIVE, DEFAULT_DAILY).budget().total == 5


def test_an_unreadable_budget_is_not_a_crash(vault_root: Path) -> None:
    (vault_root / INITIATIVE).mkdir()
    assert InitiativeFile(vault_root / INITIATIVE, DEFAULT_DAILY).budget().total == DEFAULT_DAILY


# --- the decision --------------------------------------------------------------------------


def test_an_empty_day_sends_and_reports_the_rest() -> None:
    budget = Budget(total=6, categories={"ideas": 2})
    assert decide(False, "ideas", Counts(total=0, categories={}), budget) == ("send", 1)


def test_the_category_limit_stops_the_message() -> None:
    budget = Budget(total=6, categories={"ideas": 2})
    assert decide(False, "ideas", Counts(total=2, categories={"ideas": 2}), budget) == ("defer", 0)


def test_the_total_stops_a_category_with_no_limit() -> None:
    budget = Budget(total=2, categories={"ideas": 5})
    counts = Counts(total=2, categories={"deadline": 2})
    assert decide(False, "ideas", counts, budget) == ("defer", 0)


def test_the_total_wins_over_a_category_with_room() -> None:
    budget = Budget(total=3, categories={"ideas": 5})
    counts = Counts(total=3, categories={"ideas": 1, "deadline": 2})
    assert decide(False, "ideas", counts, budget) == ("defer", 0)


def test_the_tighter_of_the_two_limits_is_the_remaining_count() -> None:
    budget = Budget(total=10, categories={"ideas": 3})
    assert decide(False, "ideas", Counts(total=1, categories={"ideas": 1}), budget) == ("send", 1)


def test_an_urgent_message_always_goes_out() -> None:
    budget = Budget(total=1, categories={"deadline": 0})
    counts = Counts(total=5, categories={"deadline": 5})
    assert decide(True, "deadline", counts, budget) == ("send", 0)


# --- the store and the tool ----------------------------------------------------------------


def test_the_tool_sends_while_the_budget_lasts(vault_root: Path, tools: dict[str, Tool]) -> None:
    _write_initiative(vault_root, 2)
    assert _notify(tools) == {"ok": True, "decision": "send", "remaining_today": 1}
    assert _notify(tools) == {"ok": True, "decision": "send", "remaining_today": 0}


def test_the_tool_defers_when_the_total_is_used_up(vault_root: Path, tools: dict[str, Tool]) -> None:
    _write_initiative(vault_root, 1)
    _notify(tools)
    assert _notify(tools) == {"ok": True, "decision": "defer", "remaining_today": 0}


def test_the_category_limit_is_counted_on_its_own(vault_root: Path, tools: dict[str, Tool]) -> None:
    _write_initiative(vault_root, 10, {"ideas": 1, "deadline": 5})
    assert _notify(tools, "ideas")["decision"] == "send"
    assert _notify(tools, "ideas")["decision"] == "defer"
    assert _notify(tools, "deadline")["decision"] == "send"


def test_the_total_covers_every_category(vault_root: Path, tools: dict[str, Tool]) -> None:
    _write_initiative(vault_root, 2, {"ideas": 5, "deadline": 5})
    assert _notify(tools, "ideas")["decision"] == "send"
    assert _notify(tools, "deadline")["decision"] == "send"
    assert _notify(tools, "ideas")["decision"] == "defer"


def test_an_urgent_message_goes_out_past_the_budget(vault_root: Path, tools: dict[str, Tool]) -> None:
    _write_initiative(vault_root, 0)
    assert _notify(tools)["decision"] == "defer"
    assert _notify(tools, urgent=True)["decision"] == "send"


def test_an_urgent_message_does_not_use_the_budget(vault_root: Path, tools: dict[str, Tool]) -> None:
    _write_initiative(vault_root, 1)
    assert _notify(tools, urgent=True)["decision"] == "send"
    assert _notify(tools, "ideas") == {"ok": True, "decision": "send", "remaining_today": 0}


def test_a_missing_budget_note_uses_the_default(vault_root: Path, tools: dict[str, Tool]) -> None:
    for index in range(DEFAULT_DAILY):
        assert _notify(tools)["decision"] == "send", index
    assert _notify(tools)["decision"] == "defer"


def test_an_invalid_budget_note_uses_the_default(vault_root: Path, tools: dict[str, Tool]) -> None:
    (vault_root / INITIATIVE).write_text("```yaml\ntotal: nonsense\n```\n", encoding="utf-8")
    assert _notify(tools)["decision"] == "send"


def test_a_configured_default_daily_is_used(vault_root: Path, tmp_path: Path, clock: ManualClock) -> None:
    config = _config(vault_root, tmp_path, default_daily=1)
    tools = {tool.spec.name: tool for tool in notify_tools(config, vault_root, clock=clock)}
    assert _notify(tools)["decision"] == "send"
    assert _notify(tools)["decision"] == "defer"


def test_every_call_is_recorded(vault_root: Path, tools: dict[str, Tool], notifier: Notifier) -> None:
    _write_initiative(vault_root, 1)
    _notify(tools, "ideas")
    _notify(tools, "deadline")
    _notify(tools, "deadline", urgent=True)
    rows = notifier.store.list()
    assert [(row.category, row.decision, row.urgent) for row in rows] == [
        ("deadline", "send", True),
        ("deadline", "defer", False),
        ("ideas", "send", False),
    ]
    assert [row.created for row in rows] == [MORNING, MORNING, MORNING]


def test_a_deferred_message_keeps_its_text(
    vault_root: Path, tools: dict[str, Tool], notifier: Notifier
) -> None:
    _write_initiative(vault_root, 0)
    _call(tools, "notify", {"category": "ideas", "text": "The idea I had about the roof."})
    row = notifier.store.list()[0]
    assert (row.text, row.status) == ("The idea I had about the roof.", "deferred")


def test_two_stores_on_one_file_share_the_counters(vault_root: Path, tmp_path: Path) -> None:
    _write_initiative(vault_root, 1)
    config = _config(vault_root, tmp_path)
    first = Notifier(config, vault_root, clock=ManualClock())
    second = Notifier(config, vault_root, clock=ManualClock())
    assert first.notify("ideas", "One.", False)["decision"] == "send"
    assert second.notify("ideas", "Two.", False)["decision"] == "defer"


def test_the_store_reopens_the_same_file(tmp_path: Path, clock: ManualClock) -> None:
    first = NotifyStore(tmp_path / "notify.sqlite3", ROME)
    first.submit("ideas", "One.", False, Budget(total=5, categories={}), MORNING)
    assert [row.text for row in NotifyStore(tmp_path / "notify.sqlite3", ROME).list()] == ["One."]


# --- the day rollover ----------------------------------------------------------------------


def test_the_day_rolls_over_at_local_midnight(
    vault_root: Path, tools: dict[str, Tool], clock: ManualClock
) -> None:
    _write_initiative(vault_root, 1)
    clock.now = datetime(2024, 5, 1, 23, 59, tzinfo=ROME).timestamp()
    assert _notify(tools)["decision"] == "send"
    assert _notify(tools)["decision"] == "defer"
    clock.now = datetime(2024, 5, 2, 0, 1, tzinfo=ROME).timestamp()
    assert _notify(tools) == {"ok": True, "decision": "send", "remaining_today": 0}


def test_the_rollover_follows_the_configured_timezone(
    vault_root: Path, tmp_path: Path, clock: ManualClock
) -> None:
    config = _config(vault_root, tmp_path, timezone="Pacific/Auckland")
    tools = {tool.spec.name: tool for tool in notify_tools(config, vault_root, clock=clock)}
    # 22:00 UTC is already the next day in Auckland and still the evening in Rome.
    clock.now = datetime(2024, 5, 1, 22, 0, tzinfo=ZoneInfo("UTC")).timestamp()
    _notify(tools)
    assert NotifyStore(config.store_path, ZoneInfo("Pacific/Auckland")).counts("2024-05-02").total == 1
    assert NotifyStore(config.store_path, ZoneInfo("Pacific/Auckland")).counts("2024-05-01").total == 0


def test_a_message_from_the_previous_local_day_does_not_use_the_budget(
    vault_root: Path, tools: dict[str, Tool], clock: ManualClock
) -> None:
    _write_initiative(vault_root, 1)
    clock.now = datetime(2024, 5, 1, 23, 30, tzinfo=ROME).timestamp()
    assert _notify(tools)["decision"] == "send"
    clock.now = datetime(2024, 5, 2, 0, 30, tzinfo=ROME).timestamp()
    assert _notify(tools)["decision"] == "send"


# --- the deferred queue --------------------------------------------------------------------


def test_the_deferred_queue_is_empty_at_first(tools: dict[str, Tool]) -> None:
    result = _call(tools, "deferred_notifications", {"since": "2024-05-01T00:00:00+02:00"})
    assert result == {"ok": True, "since": "2024-05-01T00:00:00+02:00", "notifications": []}


def test_a_deferred_message_reaches_the_queue(vault_root: Path, tools: dict[str, Tool]) -> None:
    _write_initiative(vault_root, 0)
    _call(tools, "notify", {"category": "ideas", "text": "The idea about the roof."})
    result = _call(tools, "deferred_notifications", {"since": "2024-05-01T00:00:00+02:00"})
    assert [entry["text"] for entry in result["notifications"]] == ["The idea about the roof."]
    assert result["notifications"][0]["category"] == "ideas"
    assert result["notifications"][0]["urgent"] is False
    assert result["notifications"][0]["created"].startswith("2024-05-01T09:00:00")


def test_a_sent_message_is_not_in_the_queue(vault_root: Path, tools: dict[str, Tool]) -> None:
    _write_initiative(vault_root, 5)
    _notify(tools)
    result = _call(tools, "deferred_notifications", {"since": "2024-05-01T00:00:00+02:00"})
    assert result["notifications"] == []


def test_the_queue_only_covers_the_messages_since_the_last_digest(
    vault_root: Path, tools: dict[str, Tool], clock: ManualClock
) -> None:
    _write_initiative(vault_root, 0)
    _call(tools, "notify", {"category": "ideas", "text": "Before the digest."})
    clock.advance(3600)
    _call(tools, "notify", {"category": "ideas", "text": "After the digest."})
    result = _call(tools, "deferred_notifications", {"since": "2024-05-01T09:30:00+02:00"})
    assert [entry["text"] for entry in result["notifications"]] == ["After the digest."]


def test_the_queue_is_oldest_first(vault_root: Path, tools: dict[str, Tool], clock: ManualClock) -> None:
    _write_initiative(vault_root, 0)
    _call(tools, "notify", {"category": "ideas", "text": "First."})
    clock.advance(60)
    _call(tools, "notify", {"category": "deadline", "text": "Second."})
    result = _call(tools, "deferred_notifications", {"since": "2024-05-01T00:00:00+02:00"})
    assert [entry["text"] for entry in result["notifications"]] == ["First.", "Second."]


def test_a_naive_since_is_read_as_local_time(tools: dict[str, Tool], notifier: Notifier) -> None:
    notifier.store.submit("ideas", "One.", False, Budget(total=0, categories={}), MORNING)
    result = _call(tools, "deferred_notifications", {"since": "2024-05-01T08:00:00"})
    assert len(result["notifications"]) == 1


def test_an_invalid_since_is_refused(tools: dict[str, Tool]) -> None:
    result = _call(tools, "deferred_notifications", {"since": "yesterday morning"})
    assert (result["ok"], result["code"]) == (False, "invalid_since")


def test_deferred_messages_survive_a_restart(vault_root: Path, tmp_path: Path, clock: ManualClock) -> None:
    _write_initiative(vault_root, 0)
    config = _config(vault_root, tmp_path)
    Notifier(config, vault_root, clock=clock).notify("ideas", "Waiting.", False)
    restarted = Notifier(config, vault_root, clock=clock)
    assert [entry.text for entry in restarted.store.deferred(0.0)] == ["Waiting."]


# --- feedback ------------------------------------------------------------------------------


def test_feedback_lowers_a_category_limit(vault_root: Path, tools: dict[str, Tool]) -> None:
    _write_initiative(vault_root, 6, {"ideas": 3})
    result = _call(tools, "notify_feedback", {"category": "ideas", "delta": -1})
    assert result["ok"] is True
    assert (result["previous"], result["limit"], result["total"]) == (3, 2, 6)
    assert yaml.safe_load(_block(result["block"])) == {"total": 6, "categories": {"ideas": 2}}


def test_feedback_raises_a_category_limit(vault_root: Path, tools: dict[str, Tool]) -> None:
    _write_initiative(vault_root, 6, {"ideas": 1})
    result = _call(tools, "notify_feedback", {"category": "ideas", "delta": 2})
    assert result["limit"] == 3


def test_feedback_keeps_the_other_categories(vault_root: Path, tools: dict[str, Tool]) -> None:
    _write_initiative(vault_root, 6, {"ideas": 2, "deadline": 3})
    result = _call(tools, "notify_feedback", {"category": "ideas", "delta": 1})
    block = yaml.safe_load(_block(result["block"]))
    assert block["categories"] == {"deadline": 3, "ideas": 3}


def test_feedback_starts_a_category_at_the_total(vault_root: Path, tools: dict[str, Tool]) -> None:
    _write_initiative(vault_root, 6)
    result = _call(tools, "notify_feedback", {"category": "ideas", "delta": -1})
    assert (result["previous"], result["limit"]) == (None, 5)


def test_feedback_never_goes_negative_or_past_the_total(vault_root: Path, tools: dict[str, Tool]) -> None:
    _write_initiative(vault_root, 4, {"ideas": 0})
    assert _call(tools, "notify_feedback", {"category": "ideas", "delta": -3})["limit"] == 0
    _write_initiative(vault_root, 4, {"ideas": 4})
    assert _call(tools, "notify_feedback", {"category": "ideas", "delta": 3})["limit"] == 4


def test_feedback_does_not_write_the_note(vault_root: Path, tools: dict[str, Tool]) -> None:
    _write_initiative(vault_root, 6, {"ideas": 3})
    before = (vault_root / INITIATIVE).read_text(encoding="utf-8")
    _call(tools, "notify_feedback", {"category": "ideas", "delta": -1})
    assert (vault_root / INITIATIVE).read_text(encoding="utf-8") == before


def test_the_proposed_block_can_be_written_back(vault_root: Path, tools: dict[str, Tool]) -> None:
    _write_initiative(vault_root, 6, {"ideas": 3})
    block = _call(tools, "notify_feedback", {"category": "ideas", "delta": 1})["block"]
    note = (vault_root / INITIATIVE).read_text(encoding="utf-8")
    (vault_root / INITIATIVE).write_text(note.replace(render_block(6, {"ideas": 3}), block), encoding="utf-8")
    assert InitiativeFile(vault_root / INITIATIVE, DEFAULT_DAILY).budget().categories == {"ideas": 4}


def test_feedback_without_a_usable_budget_is_refused(vault_root: Path, tools: dict[str, Tool]) -> None:
    result = _call(tools, "notify_feedback", {"category": "ideas", "delta": -1})
    assert (result["ok"], result["code"]) == (False, "budget_unavailable")


def test_feedback_with_a_broken_budget_is_refused(vault_root: Path, tools: dict[str, Tool]) -> None:
    (vault_root / INITIATIVE).write_text("```yaml\ntotal: nope\n```\n", encoding="utf-8")
    result = _call(tools, "notify_feedback", {"category": "ideas", "delta": -1})
    assert (result["ok"], result["code"]) == (False, "budget_unavailable")


def test_feedback_names_the_note_to_write(vault_root: Path, tools: dict[str, Tool]) -> None:
    _write_initiative(vault_root, 6)
    result = _call(tools, "notify_feedback", {"category": "ideas", "delta": -1})
    assert result["path"] == INITIATIVE
    assert result["category"] == "ideas"


def test_the_rendered_block_is_fenced_yaml() -> None:
    block = render_block(3, {"ideas": 1})
    assert block == "```yaml\ntotal: 3\ncategories:\n  ideas: 1\n```"


def test_the_rendered_block_without_categories_is_still_valid() -> None:
    assert Budget.model_validate(yaml.safe_load(_block(render_block(2, {})))).categories == {}


def test_deferred_params_carry_the_digest_time() -> None:
    assert DeferredParams(since="2024-05-01T09:00:00+02:00").since == "2024-05-01T09:00:00+02:00"


def _block(block: str) -> str:
    """The YAML body of a rendered block, without the fences."""
    lines = block.splitlines()
    return "\n".join(lines[1:-1])
