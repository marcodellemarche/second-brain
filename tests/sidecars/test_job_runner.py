"""Tests for the job runner sidecar. The POST is a fake, so nothing here touches the network."""

from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from second_brain.sidecars.job_runner import (
    DEFAULT_TIMEOUT,
    SIGNATURE_HEADER,
    JobRunner,
    JobRunnerError,
    job_body,
    sign_body,
)
from second_brain.tools.jobs import MAX_ATTEMPTS, Job, JobKind, JobStore

KINDS = {
    "research": JobKind(route="background-research", description="Research a question on the web."),
    "develop_idea": JobKind(route="background-idea", description="Develop an idea into a note."),
}
BASE_URL = "http://hermes:8644/webhooks"
SECRET = "test-secret-not-a-real-one"


class FakePost:
    """Records every call and answers with a queued status, or raises."""

    def __init__(self, status: int = 202, body: bytes = b'{"status": "accepted"}') -> None:
        self.status = status
        self.body = body
        self.calls: list[tuple[str, bytes, Mapping[str, str], float]] = []
        self.error: BaseException | None = None

    def __call__(
        self, url: str, body: bytes, headers: Mapping[str, str], timeout: float
    ) -> tuple[int, bytes]:
        self.calls.append((url, body, headers, timeout))
        if self.error is not None:
            raise self.error
        return self.status, self.body

    @property
    def last(self) -> tuple[str, bytes, Mapping[str, str], float]:
        return self.calls[-1]


@pytest.fixture
def store(tmp_path: Path) -> JobStore:
    return JobStore(tmp_path / "jobs.sqlite3")


@pytest.fixture
def secret_file(tmp_path: Path) -> Path:
    path = tmp_path / "webhook.secret"
    path.write_text(SECRET + "\n", encoding="utf-8")
    return path


@pytest.fixture
def post() -> FakePost:
    return FakePost()


@pytest.fixture
def runner(store: JobStore, secret_file: Path, post: FakePost) -> JobRunner:
    return JobRunner(store, KINDS, BASE_URL, secret_file, post=post)


def _enqueue(store: JobStore, kind: str = "research", **payload: Any) -> str:
    values: dict[str, Any] = {
        "title": "How do heat pumps behave below zero",
        "question": "Collect what is known about heat pump efficiency at -15 C and summarize it.",
    }
    values.update(payload)
    return store.enqueue(kind, values, "user-1", "session-1")


def _github_signature(secret: str, body: bytes) -> str:
    """The signature the test expects, computed the way Hermes v0.22.2 does."""
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


# --- signing -------------------------------------------------------------------------------


def test_sign_body_is_the_github_style_header() -> None:
    body = b'{"job_id": "abc"}'
    assert sign_body(SECRET.encode(), body) == _github_signature(SECRET, body)
    assert sign_body(SECRET.encode(), body).startswith("sha256=")


def test_sign_body_changes_with_the_body_and_the_secret() -> None:
    body = b'{"job_id": "abc"}'
    assert sign_body(b"one", body) != sign_body(b"two", body)
    assert sign_body(b"one", body) != sign_body(b"one", body + b" ")


def test_job_body_carries_only_the_payload(store: JobStore) -> None:
    job_id = _enqueue(store, note_path="ideas/heat-pumps.md")
    job = store.claim()
    assert job is not None
    body = json.loads(job_body(job))
    assert body == {
        "job_id": job_id,
        "kind": "research",
        "title": "How do heat pumps behave below zero",
        "question": "Collect what is known about heat pump efficiency at -15 C and summarize it.",
        "note_path": "ideas/heat-pumps.md",
    }
    assert sorted(body) == ["job_id", "kind", "note_path", "question", "title"]


def test_job_body_keeps_non_ascii_text_readable(store: JobStore) -> None:
    _enqueue(store, title="Pompe di calore sotto zero", question="Cosa succede a -15 °C?")
    job = store.claim()
    assert job is not None
    assert "°C" in job_body(job).decode("utf-8")


# --- sending -------------------------------------------------------------------------------


def test_run_job_posts_to_the_route_of_the_kind(store: JobStore, runner: JobRunner, post: FakePost) -> None:
    _enqueue(store, kind="develop_idea")
    assert runner.run_once() is True
    assert post.last[0] == f"{BASE_URL}/background-idea"


def test_run_job_signs_the_raw_body(store: JobStore, runner: JobRunner, post: FakePost) -> None:
    _enqueue(store)
    runner.run_once()
    _, body, headers, _ = post.last
    assert headers[SIGNATURE_HEADER] == _github_signature(SECRET, body)
    assert headers["Content-Type"] == "application/json"


def test_the_signature_does_not_match_a_tampered_body(
    store: JobStore, runner: JobRunner, post: FakePost
) -> None:
    _enqueue(store)
    runner.run_once()
    _, body, headers, _ = post.last
    assert headers[SIGNATURE_HEADER] != _github_signature(SECRET, body + b" ")


def test_run_job_sends_the_default_timeout(store: JobStore, runner: JobRunner, post: FakePost) -> None:
    _enqueue(store)
    runner.run_once()
    assert post.last[3] == DEFAULT_TIMEOUT


def test_the_timeout_is_configurable(store: JobStore, secret_file: Path, post: FakePost) -> None:
    runner = JobRunner(store, KINDS, BASE_URL, secret_file, post=post, timeout=2.5)
    _enqueue(store)
    runner.run_once()
    assert post.last[3] == 2.5


def test_the_base_url_loses_its_trailing_slash(store: JobStore, secret_file: Path, post: FakePost) -> None:
    runner = JobRunner(store, KINDS, BASE_URL + "//", secret_file, post=post)
    _enqueue(store)
    runner.run_once()
    assert post.last[0] == f"{BASE_URL}/background-research"


def test_the_secret_is_read_at_send_time(store: JobStore, secret_file: Path, post: FakePost) -> None:
    runner = JobRunner(store, KINDS, BASE_URL, secret_file, post=post)
    _enqueue(store)
    runner.run_once()
    rotated = "rotated-secret-not-a-real-one"
    secret_file.write_text(rotated, encoding="utf-8")
    _enqueue(store, question="A second, different question.")
    runner.run_once()
    _, body, headers, _ = post.last
    assert headers[SIGNATURE_HEADER] == _github_signature(rotated, body)


def test_url_refuses_an_unknown_kind(runner: JobRunner) -> None:
    with pytest.raises(JobRunnerError):
        runner.url("delete_the_vault")


# --- outcomes ------------------------------------------------------------------------------


def test_a_2xx_answer_marks_the_job_finished(store: JobStore, runner: JobRunner) -> None:
    job_id = _enqueue(store)
    runner.run_once()
    job = store.list()[0]
    assert (job.id, job.status) == (job_id, "finished")
    assert job.finished is not None
    assert job.error is None


def test_every_2xx_marks_the_job_finished(store: JobStore, secret_file: Path) -> None:
    for status in (200, 202, 204):
        store = JobStore(secret_file.parent / f"jobs-{status}.sqlite3")
        post = FakePost(status=status, body=b"")
        runner = JobRunner(store, KINDS, BASE_URL, secret_file, post=post)
        _enqueue(store)
        assert runner.run_once() is True
        assert store.list()[0].status == "finished"


def test_a_non_2xx_answer_sends_the_job_back_to_the_queue(
    store: JobStore, secret_file: Path, post: FakePost
) -> None:
    post.status = 500
    post.body = b'{"error": "boom"}'
    runner = JobRunner(store, KINDS, BASE_URL, secret_file, post=post)
    job_id = _enqueue(store)
    assert runner.run_once() is True
    job = store.list()[0]
    assert (job.id, job.status, job.attempts) == (job_id, "queued", 1)
    assert job.error is not None
    assert "500" in job.error
    assert "boom" in job.error


def test_a_job_fails_after_three_refused_attempts(store: JobStore, secret_file: Path, post: FakePost) -> None:
    post.status = 502
    runner = JobRunner(store, KINDS, BASE_URL, secret_file, post=post)
    job_id = _enqueue(store)
    for _ in range(MAX_ATTEMPTS):
        runner.run_once()
    job = store.list()[0]
    assert (job.id, job.status, job.attempts) == (job_id, "failed", MAX_ATTEMPTS)
    assert len(post.calls) == MAX_ATTEMPTS
    assert store.claim() is None


def test_a_401_from_the_webhook_is_recorded(store: JobStore, secret_file: Path, post: FakePost) -> None:
    post.status = 401
    post.body = b'{"error": "Invalid signature"}'
    runner = JobRunner(store, KINDS, BASE_URL, secret_file, post=post)
    _enqueue(store)
    runner.run_once()
    error = store.list()[0].error
    assert error is not None
    assert "401" in error and "Invalid signature" in error


def test_a_long_response_body_is_truncated_in_the_error(
    store: JobStore, secret_file: Path, post: FakePost
) -> None:
    post.status = 500
    post.body = b"x" * 5000
    runner = JobRunner(store, KINDS, BASE_URL, secret_file, post=post)
    _enqueue(store)
    runner.run_once()
    error = store.list()[0].error
    assert error is not None
    assert len(error) <= 500


def test_an_unreachable_webhook_fails_the_job_without_raising(
    store: JobStore, secret_file: Path, post: FakePost
) -> None:
    post.error = OSError("connection refused")
    runner = JobRunner(store, KINDS, BASE_URL, secret_file, post=post)
    _enqueue(store)
    assert runner.run_once() is True
    job = store.list()[0]
    assert (job.status, job.attempts) == ("queued", 1)
    assert job.error is not None
    assert "connection refused" in job.error


def test_an_unknown_kind_in_the_queue_fails_the_job(
    store: JobStore, secret_file: Path, post: FakePost
) -> None:
    runner = JobRunner(store, KINDS, BASE_URL, secret_file, post=post)
    _enqueue(store, kind="retired_kind")
    runner.run_once()
    assert post.calls == []
    job = store.list()[0]
    assert job.status == "queued"
    assert job.error is not None
    assert "retired_kind" in job.error


def test_a_missing_secret_file_fails_the_job(store: JobStore, tmp_path: Path, post: FakePost) -> None:
    runner = JobRunner(store, KINDS, BASE_URL, tmp_path / "missing.secret", post=post)
    _enqueue(store)
    runner.run_once()
    assert post.calls == []
    job = store.list()[0]
    assert job.status == "queued"
    assert job.error is not None
    assert "secret" in job.error


def test_an_empty_secret_file_fails_the_job(store: JobStore, tmp_path: Path, post: FakePost) -> None:
    path = tmp_path / "empty.secret"
    path.write_text("  \n", encoding="utf-8")
    runner = JobRunner(store, KINDS, BASE_URL, path, post=post)
    _enqueue(store)
    runner.run_once()
    assert post.calls == []
    assert store.list()[0].error is not None


# --- the loop ------------------------------------------------------------------------------


def test_run_once_returns_false_on_an_empty_queue(runner: JobRunner, post: FakePost) -> None:
    assert runner.run_once() is False
    assert post.calls == []


def test_run_once_runs_a_single_job(store: JobStore, runner: JobRunner, post: FakePost) -> None:
    _enqueue(store, question="First question.")
    _enqueue(store, question="Second question.")
    runner.run_once()
    assert len(post.calls) == 1
    assert len(store.list("queued")) == 1
    assert len(store.list("finished")) == 1


def test_one_job_at_a_time_by_default(runner: JobRunner) -> None:
    assert runner.max_concurrent == 1


def test_max_concurrent_must_be_positive(store: JobStore, secret_file: Path) -> None:
    with pytest.raises(ValueError):
        JobRunner(store, KINDS, BASE_URL, secret_file, max_concurrent=0)


def test_an_empty_base_url_is_refused(store: JobStore, secret_file: Path) -> None:
    with pytest.raises(ValueError):
        JobRunner(store, KINDS, "  ", secret_file)


class Stopper:
    """A sleep that records its argument and stops the runner after a few ticks."""

    def __init__(self, after: int = 2) -> None:
        self.after = after
        self.calls: list[float] = []
        self.runner: JobRunner | None = None

    def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)
        if len(self.calls) >= self.after and self.runner is not None:
            self.runner.stop()


def test_run_forever_drains_the_queue_then_sleeps(store: JobStore, secret_file: Path, post: FakePost) -> None:
    sleep = Stopper()
    runner = JobRunner(store, KINDS, BASE_URL, secret_file, post=post, sleep=sleep)
    sleep.runner = runner
    _enqueue(store, question="First question.")
    _enqueue(store, question="Second question.")
    runner.run_forever(poll_seconds=3.0)
    assert len(post.calls) == 2
    assert len(store.list("finished")) == 2
    assert sleep.calls == [3.0, 3.0]


def test_run_forever_stops_without_touching_the_queue(
    store: JobStore, secret_file: Path, post: FakePost
) -> None:
    sleep = Stopper(after=1)
    runner = JobRunner(store, KINDS, BASE_URL, secret_file, post=post, sleep=sleep)
    sleep.runner = runner
    runner.run_forever(poll_seconds=1.0)
    assert post.calls == []
    assert sleep.calls == [1.0]


def test_run_forever_refuses_a_useless_poll_interval(runner: JobRunner) -> None:
    with pytest.raises(ValueError):
        runner.run_forever(poll_seconds=0)


def test_run_forever_survives_a_broken_store(secret_file: Path, post: FakePost) -> None:
    class BrokenStore(JobStore):
        def claim(self) -> Job | None:
            raise RuntimeError("database is gone")

    store = BrokenStore(secret_file.parent / "broken.sqlite3")
    sleep = Stopper(after=2)
    runner = JobRunner(store, KINDS, BASE_URL, secret_file, post=post, sleep=sleep)
    sleep.runner = runner
    runner.run_forever(poll_seconds=1.0)
    assert len(sleep.calls) == 2
