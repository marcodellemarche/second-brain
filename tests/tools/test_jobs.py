"""Tests for the job queue and the two enqueue tools."""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from second_brain.core.types import Autonomy, Effect
from second_brain.tools.jobs import (
    DEFAULT_STALE_AFTER,
    MAX_ATTEMPTS,
    STATUSES,
    Job,
    JobEnqueueParams,
    JobKind,
    JobPayload,
    JobStore,
    job_tools,
)
from second_brain.tools.registry import Tool
from second_brain.tools.vault import Provenance

KINDS = {
    "research": JobKind(route="background-research", description="Research a question on the web.", web=True),
    "develop_idea": JobKind(route="background-idea", description="Develop an idea into a note.", web=False),
}


class FakeClock:
    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        self.now += 1.0
        return self.now


class ManualClock:
    """A clock that only moves when the test moves it."""

    def __init__(self, now: float = 1000.0) -> None:
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
def store(tmp_path: Path) -> JobStore:
    return JobStore(tmp_path / "jobs.sqlite3")


@pytest.fixture
def tools(store: JobStore) -> dict[str, Tool]:
    return {tool.spec.name: tool for tool in job_tools(store, KINDS)}


def _payload(**overrides: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "title": "How do heat pumps behave below zero",
        "question": "Collect what is known about heat pump efficiency at -15 C and summarize it.",
    }
    values.update(overrides)
    return values


def _call(tools: dict[str, Tool], name: str, params: dict[str, Any], **prov: object) -> dict[str, Any]:
    tool = tools[name]
    parsed = tool.spec.params.model_validate(params)
    return tool.handler(parsed, _provenance(**prov))


def _claimed(store: JobStore) -> Job:
    job = store.claim()
    assert job is not None
    return job


# --- specs ---------------------------------------------------------------------------------


def test_there_is_one_tool_per_effect_class(tools: dict[str, Tool]) -> None:
    assert set(tools) == {"research_enqueue", "job_enqueue"}


def test_research_enqueue_is_external_and_auto(tools: dict[str, Tool]) -> None:
    spec = tools["research_enqueue"].spec
    assert (spec.effect, spec.autonomy) == (Effect.EXTERNAL, Autonomy.AUTO)
    assert spec.hosts == ()
    assert spec.credentials == ()


def test_job_enqueue_is_internal_and_notifies(tools: dict[str, Tool]) -> None:
    spec = tools["job_enqueue"].spec
    assert (spec.effect, spec.autonomy) == (Effect.INTERNAL, Autonomy.NOTIFY)
    assert spec.hosts == ()
    assert spec.credentials == ()


def test_the_two_tools_take_the_same_params(tools: dict[str, Tool]) -> None:
    assert tools["research_enqueue"].spec.params is JobEnqueueParams
    assert tools["job_enqueue"].spec.params is JobEnqueueParams


def test_a_web_kind_is_required_to_say_so() -> None:
    with pytest.raises(ValidationError):
        JobKind.model_validate({"route": "background-research", "description": "Research."})


def test_the_payload_has_no_field_for_vault_content() -> None:
    assert set(JobPayload.model_fields) == {"title", "question", "note_path"}
    assert set(JobEnqueueParams.model_fields) == {"kind", "title", "question", "note_path"}


def test_a_brain_with_only_web_kinds_gets_only_the_research_tool(store: JobStore) -> None:
    kinds = {"research": KINDS["research"]}
    assert {tool.spec.name for tool in job_tools(store, kinds)} == {"research_enqueue"}


def test_a_brain_with_only_local_kinds_gets_only_the_job_tool(store: JobStore) -> None:
    kinds = {"develop_idea": KINDS["develop_idea"]}
    assert {tool.spec.name for tool in job_tools(store, kinds)} == {"job_enqueue"}


def test_no_kinds_means_no_tools(store: JobStore) -> None:
    assert job_tools(store, {}) == []


# --- enqueue -------------------------------------------------------------------------------


def test_enqueue_returns_a_job_that_is_queued(store: JobStore) -> None:
    job_id = store.enqueue("research", _payload(), "user-1", "session-1")
    assert job_id
    jobs = store.list()
    assert len(jobs) == 1
    job = jobs[0]
    assert (job.id, job.kind, job.status) == (job_id, "research", "queued")
    assert (job.requested_by, job.session) == ("user-1", "session-1")
    assert job.attempts == 0
    assert (job.started, job.dispatched, job.finished, job.error) == (None, None, None, None)


def test_enqueue_keeps_the_payload(store: JobStore) -> None:
    payload = _payload(note_path="ideas/heat-pumps.md")
    store.enqueue("develop_idea", payload, "user-1", "session-1")
    job = store.list()[0]
    assert job.payload.title == payload["title"]
    assert job.payload.question == payload["question"]
    assert job.payload.note_path == "ideas/heat-pumps.md"


def test_enqueue_is_idempotent_for_the_same_payload(store: JobStore) -> None:
    first = store.enqueue("research", _payload(), "user-1", "session-1")
    second = store.enqueue("research", _payload(), "user-2", "session-2")
    assert first == second
    assert len(store.list()) == 1


def test_enqueue_is_idempotent_for_the_same_payload_while_running(store: JobStore) -> None:
    first = store.enqueue("research", _payload(), "user-1", "session-1")
    assert store.claim() is not None
    assert store.enqueue("research", _payload(), "user-1", "session-1") == first


def test_enqueue_is_idempotent_for_the_same_payload_while_dispatched(store: JobStore) -> None:
    first = store.enqueue("research", _payload(), "user-1", "session-1")
    store.claim()
    store.dispatch(first)
    assert store.enqueue("research", _payload(), "user-1", "session-1") == first


def test_enqueue_is_idempotent_across_field_order(store: JobStore) -> None:
    first = store.enqueue("research", _payload(), "user-1", "session-1")
    reordered = {"question": _payload()["question"], "title": _payload()["title"], "note_path": None}
    assert store.enqueue("research", reordered, "user-1", "session-1") == first


def test_enqueue_treats_a_different_kind_as_a_different_job(store: JobStore) -> None:
    first = store.enqueue("research", _payload(), "user-1", "session-1")
    second = store.enqueue("develop_idea", _payload(), "user-1", "session-1")
    assert first != second
    assert len(store.list()) == 2


def test_enqueue_treats_a_different_question_as_a_different_job(store: JobStore) -> None:
    first = store.enqueue("research", _payload(), "user-1", "session-1")
    second = store.enqueue("research", _payload(question="Something else entirely."), "user-1", "session-1")
    assert first != second


def test_a_finished_job_does_not_block_a_new_one(store: JobStore) -> None:
    first = store.enqueue("research", _payload(), "user-1", "session-1")
    store.claim()
    store.mark_finished(first)
    second = store.enqueue("research", _payload(), "user-1", "session-1")
    assert second != first


def test_enqueue_refuses_a_payload_that_is_not_a_job_payload(store: JobStore) -> None:
    with pytest.raises(ValidationError):
        store.enqueue("research", {"title": "only a title"}, "user-1", "session-1")


def test_enqueue_refuses_an_empty_identity(store: JobStore) -> None:
    with pytest.raises(ValueError):
        store.enqueue("research", _payload(), "  ", "session-1")
    with pytest.raises(ValueError):
        store.enqueue("research", _payload(), "user-1", "")


def test_enqueue_refuses_vault_content_in_the_payload(store: JobStore) -> None:
    with pytest.raises(ValidationError):
        store.enqueue("research", {**_payload(), "content": "a whole note"}, "user-1", "session-1")


def test_the_payload_is_stored_without_vault_content(store: JobStore) -> None:
    store.enqueue("research", _payload(), "user-1", "session-1")
    with sqlite3.connect(store.path) as connection:
        rows = connection.execute("SELECT payload FROM jobs").fetchall()
    assert [sorted(json.loads(row[0])) for row in rows] == [["note_path", "question", "title"]]


# --- claim ---------------------------------------------------------------------------------


def test_claim_takes_the_oldest_job_first(store: JobStore) -> None:
    clock = FakeClock()
    store = JobStore(store.path, clock=clock)
    first = store.enqueue("research", _payload(question="First question."), "user-1", "session-1")
    second = store.enqueue("research", _payload(question="Second question."), "user-1", "session-1")
    assert _claimed(store).id == first
    assert _claimed(store).id == second
    assert store.claim() is None


def test_claim_moves_the_job_to_running_and_counts_the_attempt(store: JobStore) -> None:
    job_id = store.enqueue("research", _payload(), "user-1", "session-1")
    job = _claimed(store)
    assert (job.id, job.status, job.attempts) == (job_id, "running", 1)
    assert job.started is not None
    assert store.list("queued") == []
    assert [entry.id for entry in store.list("running")] == [job_id]


def test_claim_on_an_empty_queue_returns_none(store: JobStore) -> None:
    assert store.claim() is None


def test_two_threads_never_claim_the_same_job(store: JobStore) -> None:
    total = 40
    for index in range(total):
        store.enqueue("research", _payload(question=f"Question {index}."), "user-1", "session-1")
    claimed: list[str] = []
    errors: list[BaseException] = []
    lock = threading.Lock()
    start = threading.Barrier(4)

    def worker() -> None:
        try:
            start.wait()
            while True:
                job = store.claim()
                if job is None:
                    return
                with lock:
                    claimed.append(job.id)
        except BaseException as exc:  # pragma: no cover - only on a broken claim
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    assert len(claimed) == total
    assert len(set(claimed)) == total


def test_claim_serializes_two_stores_on_one_file(tmp_path: Path) -> None:
    path = tmp_path / "jobs.sqlite3"
    first = JobStore(path)
    second = JobStore(path)
    job_id = first.enqueue("research", _payload(), "user-1", "session-1")
    claimed = [first.claim(), second.claim()]
    assert [job.id for job in claimed if job is not None] == [job_id]


def test_two_threads_enqueueing_the_same_payload_get_one_job(tmp_path: Path) -> None:
    first = JobStore(tmp_path / "jobs.sqlite3")
    second = JobStore(tmp_path / "jobs.sqlite3")
    ids: list[str] = []
    errors: list[BaseException] = []
    lock = threading.Lock()
    start = threading.Barrier(8)

    def worker(store: JobStore) -> None:
        try:
            start.wait()
            job_id = store.enqueue("research", _payload(), "user-1", "session-1")
            with lock:
                ids.append(job_id)
        except BaseException as exc:  # pragma: no cover - only on a broken enqueue
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(first if index % 2 else second,)) for index in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    assert len(set(ids)) == 1
    assert len(first.list()) == 1


# --- dispatch and finish -------------------------------------------------------------------


def test_dispatch_marks_the_job_dispatched(store: JobStore) -> None:
    job_id = store.enqueue("research", _payload(), "user-1", "session-1")
    store.claim()
    store.dispatch(job_id)
    job = store.list()[0]
    assert (job.status, job.error) == ("dispatched", None)
    assert job.dispatched is not None
    assert job.finished is None


def test_dispatch_leaves_a_queued_job_alone(store: JobStore) -> None:
    job_id = store.enqueue("research", _payload(), "user-1", "session-1")
    store.dispatch(job_id)
    assert store.list()[0].status == "queued"


def test_mark_finished_finishes_a_dispatched_job(store: JobStore) -> None:
    job_id = store.enqueue("research", _payload(), "user-1", "session-1")
    store.claim()
    store.dispatch(job_id)
    store.mark_finished(job_id)
    job = store.list()[0]
    assert (job.status, job.error) == ("finished", None)
    assert job.finished is not None


def test_mark_finished_finishes_a_running_job(store: JobStore) -> None:
    job_id = store.enqueue("research", _payload(), "user-1", "session-1")
    store.claim()
    store.mark_finished(job_id)
    assert store.list()[0].status == "finished"


def test_mark_finished_leaves_a_queued_job_alone(store: JobStore) -> None:
    job_id = store.enqueue("research", _payload(), "user-1", "session-1")
    store.mark_finished(job_id)
    assert store.list()[0].status == "queued"


def test_a_dispatched_job_is_not_claimed_again(store: JobStore) -> None:
    job_id = store.enqueue("research", _payload(), "user-1", "session-1")
    store.claim()
    store.dispatch(job_id)
    assert store.claim() is None


# --- fail ----------------------------------------------------------------------------------


def test_fail_puts_the_job_back_in_the_queue(store: JobStore) -> None:
    job_id = store.enqueue("research", _payload(), "user-1", "session-1")
    store.claim()
    store.fail(job_id, "webhook answered 500")
    job = store.list()[0]
    assert (job.status, job.attempts, job.started) == ("queued", 1, None)
    assert job.error == "webhook answered 500"


def test_a_failed_job_can_be_claimed_again(store: JobStore) -> None:
    job_id = store.enqueue("research", _payload(), "user-1", "session-1")
    store.claim()
    store.fail(job_id, "boom")
    again = _claimed(store)
    assert (again.id, again.attempts) == (job_id, 2)


def test_a_job_fails_after_three_attempts(store: JobStore) -> None:
    job_id = store.enqueue("research", _payload(), "user-1", "session-1")
    for _ in range(MAX_ATTEMPTS):
        assert _claimed(store).id == job_id
        store.fail(job_id, "webhook answered 502")
    job = store.list()[0]
    assert (job.status, job.attempts) == ("failed", MAX_ATTEMPTS)
    assert job.error == "webhook answered 502"
    assert job.finished is not None
    assert store.claim() is None


def test_a_failed_job_can_be_failed_without_a_retry(store: JobStore) -> None:
    job_id = store.enqueue("research", _payload(), "user-1", "session-1")
    store.claim()
    store.dispatch(job_id)
    store.fail(job_id, "no result note", retry=False)
    job = store.list()[0]
    assert (job.status, job.attempts) == ("failed", 1)
    assert job.error == "no result note"


def test_a_dispatched_job_can_go_back_to_the_queue(store: JobStore) -> None:
    job_id = store.enqueue("research", _payload(), "user-1", "session-1")
    store.claim()
    store.dispatch(job_id)
    store.fail(job_id, "boom")
    job = store.list()[0]
    assert (job.status, job.dispatched) == ("queued", None)


def test_a_job_that_failed_is_not_retried_by_enqueue(store: JobStore) -> None:
    job_id = store.enqueue("research", _payload(), "user-1", "session-1")
    for _ in range(MAX_ATTEMPTS):
        store.claim()
        store.fail(job_id, "boom")
    assert store.enqueue("research", _payload(), "user-1", "session-1") != job_id


def test_fail_shortens_and_flattens_the_error(store: JobStore) -> None:
    job_id = store.enqueue("research", _payload(), "user-1", "session-1")
    store.claim()
    store.fail(job_id, "line one\nline two " + "x" * 2000)
    error = store.list()[0].error
    assert error is not None
    assert "\n" not in error
    assert len(error) <= 500


def test_fail_on_an_unknown_job_does_nothing(store: JobStore) -> None:
    store.fail("no-such-job", "boom")
    assert store.list() == []


def test_fail_leaves_a_finished_job_alone(store: JobStore) -> None:
    job_id = store.enqueue("research", _payload(), "user-1", "session-1")
    store.claim()
    store.mark_finished(job_id)
    store.fail(job_id, "boom")
    assert store.list()[0].status == "finished"


# --- stale jobs ----------------------------------------------------------------------------


def test_the_stale_window_is_fifteen_minutes() -> None:
    assert DEFAULT_STALE_AFTER == 900.0


def test_a_running_job_goes_back_to_the_queue_when_it_is_stale(tmp_path: Path) -> None:
    clock = ManualClock()
    store = JobStore(tmp_path / "jobs.sqlite3", clock=clock)
    job_id = store.enqueue("research", _payload(), "user-1", "session-1")
    store.claim()
    clock.advance(DEFAULT_STALE_AFTER + 1)
    assert store.reclaim_stale() == [job_id]
    job = store.list()[0]
    assert (job.status, job.attempts, job.started) == ("queued", 1, None)
    assert job.error is not None
    assert store.reclaim_stale() == []


def test_a_reclaimed_job_can_be_claimed_again_and_counts_the_attempt(tmp_path: Path) -> None:
    clock = ManualClock()
    store = JobStore(tmp_path / "jobs.sqlite3", clock=clock)
    job_id = store.enqueue("research", _payload(), "user-1", "session-1")
    store.claim()
    clock.advance(DEFAULT_STALE_AFTER + 1)
    store.reclaim_stale()
    again = _claimed(store)
    assert (again.id, again.attempts) == (job_id, 2)


def test_a_fresh_running_job_is_left_alone(tmp_path: Path) -> None:
    clock = ManualClock()
    store = JobStore(tmp_path / "jobs.sqlite3", clock=clock)
    store.enqueue("research", _payload(), "user-1", "session-1")
    store.claim()
    clock.advance(DEFAULT_STALE_AFTER - 1)
    assert store.reclaim_stale() == []
    assert len(store.list("running")) == 1


def test_a_stale_job_that_used_every_attempt_fails(tmp_path: Path) -> None:
    clock = ManualClock()
    store = JobStore(tmp_path / "jobs.sqlite3", clock=clock)
    store.enqueue("research", _payload(), "user-1", "session-1")
    for _ in range(MAX_ATTEMPTS):
        store.claim()
        clock.advance(DEFAULT_STALE_AFTER + 1)
        store.reclaim_stale()
    job = store.list()[0]
    assert (job.status, job.attempts) == ("failed", MAX_ATTEMPTS)
    assert store.claim() is None


def test_a_dispatched_job_is_not_stale(tmp_path: Path) -> None:
    clock = ManualClock()
    store = JobStore(tmp_path / "jobs.sqlite3", clock=clock)
    job_id = store.enqueue("research", _payload(), "user-1", "session-1")
    store.claim()
    store.dispatch(job_id)
    clock.advance(DEFAULT_STALE_AFTER * 4)
    assert store.reclaim_stale() == []
    assert store.list()[0].status == "dispatched"


def test_the_stale_window_is_configurable(tmp_path: Path) -> None:
    clock = ManualClock()
    store = JobStore(tmp_path / "jobs.sqlite3", clock=clock, stale_after=10)
    store.enqueue("research", _payload(), "user-1", "session-1")
    store.claim()
    clock.advance(11)
    assert len(store.reclaim_stale()) == 1


def test_a_useless_stale_window_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        JobStore(tmp_path / "jobs.sqlite3", stale_after=0)


# --- list ----------------------------------------------------------------------------------


def test_list_returns_the_newest_first(store: JobStore) -> None:
    clock = FakeClock()
    store = JobStore(store.path, clock=clock)
    ids = [
        store.enqueue("research", _payload(question=f"Question {index}."), "user-1", "session-1")
        for index in range(3)
    ]
    assert [job.id for job in store.list()] == list(reversed(ids))


def test_list_filters_by_status_and_limit(store: JobStore) -> None:
    done = store.enqueue("research", _payload(question="Done one."), "user-1", "session-1")
    store.enqueue("research", _payload(question="Queued one."), "user-1", "session-1")
    store.claim()
    store.mark_finished(done)
    assert [job.id for job in store.list("finished")] == [done]
    assert len(store.list("queued")) == 1
    assert len(store.list(limit=1)) == 1


def test_every_status_can_be_listed(store: JobStore) -> None:
    for status in STATUSES:
        assert store.list(status) == []


def test_list_refuses_an_unknown_status(store: JobStore) -> None:
    with pytest.raises(ValueError):
        store.list("done")


def test_the_store_reopens_the_same_file(tmp_path: Path) -> None:
    path = tmp_path / "jobs.sqlite3"
    job_id = JobStore(path).enqueue("research", _payload(), "user-1", "session-1")
    assert [job.id for job in JobStore(path).list()] == [job_id]


def test_a_queue_file_from_before_the_dispatch_column_still_opens(tmp_path: Path) -> None:
    path = tmp_path / "jobs.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.executescript(
            "CREATE TABLE jobs (id TEXT PRIMARY KEY, kind TEXT NOT NULL, payload TEXT NOT NULL,"
            " status TEXT NOT NULL, requested_by TEXT NOT NULL, session TEXT NOT NULL,"
            " created REAL NOT NULL, started REAL, finished REAL,"
            " attempts INTEGER NOT NULL DEFAULT 0, error TEXT);"
        )
    job_id = JobStore(path).enqueue("research", _payload(), "user-1", "session-1")
    job = JobStore(path).list()[0]
    assert (job.id, job.dispatched) == (job_id, None)


# --- the tools -----------------------------------------------------------------------------


def test_research_enqueue_queues_a_web_job(tools: dict[str, Tool], store: JobStore) -> None:
    result = _call(tools, "research_enqueue", {"kind": "research", **_payload()})
    assert result == {"ok": True, "job_id": store.list()[0].id, "kind": "research", "status": "queued"}
    job = store.list()[0]
    assert (job.requested_by, job.session) == ("user-1", "session-1")


def test_job_enqueue_queues_a_local_job(tools: dict[str, Tool], store: JobStore) -> None:
    result = _call(tools, "job_enqueue", {"kind": "develop_idea", **_payload()})
    assert result["ok"] is True
    assert store.list()[0].kind == "develop_idea"


def test_job_enqueue_uses_the_provenance_as_the_requester(tools: dict[str, Tool], store: JobStore) -> None:
    _call(
        tools,
        "job_enqueue",
        {"kind": "develop_idea", **_payload()},
        requested_by="user-7",
        session="session-9",
    )
    job = store.list()[0]
    assert (job.kind, job.requested_by, job.session) == ("develop_idea", "user-7", "session-9")


def test_research_enqueue_refuses_a_local_kind(tools: dict[str, Tool], store: JobStore) -> None:
    result = _call(tools, "research_enqueue", {"kind": "develop_idea", **_payload()})
    assert (result["ok"], result["code"]) == (False, "wrong_tool")
    assert "job_enqueue" in result["message"]
    assert store.list() == []


def test_job_enqueue_refuses_a_web_kind(tools: dict[str, Tool], store: JobStore) -> None:
    result = _call(tools, "job_enqueue", {"kind": "research", **_payload()})
    assert (result["ok"], result["code"]) == (False, "wrong_tool")
    assert "research_enqueue" in result["message"]
    assert store.list() == []


def test_each_tool_returns_the_same_id_twice(tools: dict[str, Tool], store: JobStore) -> None:
    first = _call(tools, "research_enqueue", {"kind": "research", **_payload()})
    second = _call(tools, "research_enqueue", {"kind": "research", **_payload()})
    assert first["job_id"] == second["job_id"]
    assert len(store.list()) == 1


def test_research_enqueue_refuses_an_unknown_kind(tools: dict[str, Tool], store: JobStore) -> None:
    result = _call(tools, "research_enqueue", {"kind": "delete_the_vault", **_payload()})
    assert result["ok"] is False
    assert result["code"] == "unknown_kind"
    assert "research" in result["message"]
    assert store.list() == []


def test_job_enqueue_refuses_an_unknown_kind(tools: dict[str, Tool], store: JobStore) -> None:
    result = _call(tools, "job_enqueue", {"kind": "delete_the_vault", **_payload()})
    assert result["ok"] is False
    assert result["code"] == "unknown_kind"
    assert "develop_idea" in result["message"]
    assert "research" not in result["message"]
    assert store.list() == []


def test_the_unknown_kind_message_of_a_single_tool_lists_its_own_kinds(store: JobStore) -> None:
    tools = {tool.spec.name: tool for tool in job_tools(store, {"develop_idea": KINDS["develop_idea"]})}
    result = _call(tools, "job_enqueue", {"kind": "research", **_payload()})
    assert (result["ok"], result["code"]) == (False, "unknown_kind")


def test_a_question_over_the_limit_is_refused(tools: dict[str, Tool], store: JobStore) -> None:
    with pytest.raises(ValidationError):
        _call(tools, "research_enqueue", {"kind": "research", **_payload(question="x" * 2001)})
    assert store.list() == []


def test_a_question_at_the_limit_is_accepted(tools: dict[str, Tool]) -> None:
    result = _call(tools, "research_enqueue", {"kind": "research", **_payload(question="x" * 2000)})
    assert result["ok"] is True


def test_an_empty_question_is_refused(tools: dict[str, Tool]) -> None:
    with pytest.raises(ValidationError):
        _call(tools, "research_enqueue", {"kind": "research", **_payload(question="   ")})


def test_the_title_is_flattened(tools: dict[str, Tool], store: JobStore) -> None:
    _call(tools, "research_enqueue", {"kind": "research", **_payload(title="  Heat   pumps\nbelow zero  ")})
    assert store.list()[0].payload.title == "Heat pumps below zero"


def test_a_note_path_outside_the_vault_is_refused(tools: dict[str, Tool]) -> None:
    for path in ("../outside.md", "/etc/notes.md", "ideas/../../x.md", "ideas//x.md", "ideas/x.txt"):
        with pytest.raises(ValidationError):
            _call(tools, "research_enqueue", {"kind": "research", **_payload(note_path=path)})


def test_a_note_path_inside_the_vault_is_kept(tools: dict[str, Tool], store: JobStore) -> None:
    _call(tools, "job_enqueue", {"kind": "develop_idea", **_payload(note_path="ideas/heat-pumps.md")})
    assert store.list()[0].payload.note_path == "ideas/heat-pumps.md"
