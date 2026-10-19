"""Tests for the job runner sidecar. The POST is a fake, so nothing here touches the network."""

from __future__ import annotations

import hashlib
import hmac
import json
import subprocess
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from second_brain.core.schema import load_schema
from second_brain.sidecars.job_runner import (
    DEFAULT_DISPATCH_TIMEOUT,
    DEFAULT_TIMEOUT,
    SIGNATURE_HEADER,
    TIMESTAMP_HEADER,
    JobRunner,
    JobRunnerError,
    job_body,
    sign_body,
)
from second_brain.tools.jobs import MAX_ATTEMPTS, Job, JobKind, JobStore
from second_brain.tools.outbox import OutboxImporter
from second_brain.tools.vault import Vault, VaultConfig

KINDS = {
    "research": JobKind(route="background-research", description="Research a question on the web.", web=True),
    "develop_idea": JobKind(route="background-idea", description="Develop an idea into a note.", web=False),
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


class ManualClock:
    def __init__(self, now: float = 1_700_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def vault_root(tmp_path: Path) -> Path:
    root = tmp_path / "vault"
    (root / "ideas").mkdir(parents=True)
    return root


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
def runner(store: JobStore, vault_root: Path, secret_file: Path, post: FakePost) -> JobRunner:
    return JobRunner(store, KINDS, BASE_URL, secret_file, vault_root=vault_root, post=post)


def _enqueue(store: JobStore, kind: str = "research", **payload: Any) -> str:
    values: dict[str, Any] = {
        "title": "How do heat pumps behave below zero",
        "question": "Collect what is known about heat pump efficiency at -15 C and summarize it.",
    }
    values.update(payload)
    return store.enqueue(kind, values, "user-1", "session-1")


def _store_with_clock(tmp_path: Path, clock: ManualClock) -> JobStore:
    """A queue and a runner that share one clock, so the timeouts are the test's to move."""
    return JobStore(tmp_path / "jobs.sqlite3", clock=clock)


def _v2_signature(secret: str, body: bytes, timestamp: str) -> str:
    """The signature the test expects, computed the way Hermes v0.22.2 does."""
    return hmac.new(secret.encode(), timestamp.encode() + b"." + body, hashlib.sha256).hexdigest()


# --- signing -------------------------------------------------------------------------------


def test_sign_body_is_the_v2_header() -> None:
    body = b'{"job_id": "abc"}'
    timestamp = "1700000000"
    assert sign_body(SECRET.encode(), body, timestamp) == _v2_signature(SECRET, body, timestamp)
    assert not sign_body(SECRET.encode(), body, timestamp).startswith("sha256=")


def test_the_signature_commits_to_the_timestamp() -> None:
    body = b'{"job_id": "abc"}'
    assert sign_body(b"one", body, "1700000000") != sign_body(b"one", body, "1700000001")


def test_sign_body_changes_with_the_body_and_the_secret() -> None:
    body = b'{"job_id": "abc"}'
    assert sign_body(b"one", body, "1") != sign_body(b"two", body, "1")
    assert sign_body(b"one", body, "1") != sign_body(b"one", body + b" ", "1")


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


def test_run_job_sends_the_v2_signature_and_its_timestamp(
    store: JobStore, runner: JobRunner, post: FakePost
) -> None:
    _enqueue(store)
    runner.run_once()
    _, body, headers, _ = post.last
    timestamp = headers[TIMESTAMP_HEADER]
    assert timestamp.isdigit()
    assert headers[SIGNATURE_HEADER] == _v2_signature(SECRET, body, timestamp)
    assert headers["Content-Type"] == "application/json"


def test_the_legacy_header_is_not_sent(store: JobStore, runner: JobRunner, post: FakePost) -> None:
    _enqueue(store)
    runner.run_once()
    _, _, headers, _ = post.last
    assert "X-Hub-Signature-256" not in headers


def test_the_timestamp_is_the_current_time(store: JobStore, vault_root: Path, secret_file: Path) -> None:
    clock = ManualClock()
    post = FakePost()
    runner = JobRunner(store, KINDS, BASE_URL, secret_file, vault_root=vault_root, post=post, clock=clock)
    _enqueue(store)
    runner.run_once()
    assert post.last[2][TIMESTAMP_HEADER] == "1700000000"


def test_the_signature_does_not_match_a_tampered_body(
    store: JobStore, runner: JobRunner, post: FakePost
) -> None:
    _enqueue(store)
    runner.run_once()
    _, body, headers, _ = post.last
    assert headers[SIGNATURE_HEADER] != _v2_signature(SECRET, body + b" ", headers[TIMESTAMP_HEADER])


def test_the_signature_does_not_match_a_tampered_timestamp(
    store: JobStore, runner: JobRunner, post: FakePost
) -> None:
    _enqueue(store)
    runner.run_once()
    _, body, headers, _ = post.last
    assert headers[SIGNATURE_HEADER] != _v2_signature(SECRET, body, "1700000001")


def test_run_job_sends_the_default_timeout(store: JobStore, runner: JobRunner, post: FakePost) -> None:
    _enqueue(store)
    runner.run_once()
    assert post.last[3] == DEFAULT_TIMEOUT


def test_the_timeout_is_configurable(
    store: JobStore, vault_root: Path, secret_file: Path, post: FakePost
) -> None:
    runner = JobRunner(store, KINDS, BASE_URL, secret_file, vault_root=vault_root, post=post, timeout=2.5)
    _enqueue(store)
    runner.run_once()
    assert post.last[3] == 2.5


def test_the_base_url_loses_its_trailing_slash(
    store: JobStore, vault_root: Path, secret_file: Path, post: FakePost
) -> None:
    runner = JobRunner(store, KINDS, BASE_URL + "//", secret_file, vault_root=vault_root, post=post)
    _enqueue(store)
    runner.run_once()
    assert post.last[0] == f"{BASE_URL}/background-research"


def test_the_secret_is_read_at_send_time(
    store: JobStore, vault_root: Path, secret_file: Path, post: FakePost
) -> None:
    runner = JobRunner(store, KINDS, BASE_URL, secret_file, vault_root=vault_root, post=post)
    _enqueue(store)
    runner.run_once()
    rotated = "rotated-secret-not-a-real-one"
    secret_file.write_text(rotated, encoding="utf-8")
    _enqueue(store, question="A second, different question.")
    runner.run_once()
    _, body, headers, _ = post.last
    assert headers[SIGNATURE_HEADER] == _v2_signature(rotated, body, headers[TIMESTAMP_HEADER])


def test_url_refuses_an_unknown_kind(runner: JobRunner) -> None:
    with pytest.raises(JobRunnerError):
        runner.url("delete_the_vault")


# --- outcomes ------------------------------------------------------------------------------


def test_a_2xx_answer_marks_the_job_dispatched(store: JobStore, runner: JobRunner) -> None:
    job_id = _enqueue(store)
    runner.run_once()
    job = store.list()[0]
    assert (job.id, job.status) == (job_id, "dispatched")
    assert job.dispatched is not None
    assert job.finished is None
    assert job.error is None


def test_every_2xx_marks_the_job_dispatched(store: JobStore, vault_root: Path, secret_file: Path) -> None:
    for status in (200, 202, 204):
        store = JobStore(secret_file.parent / f"jobs-{status}.sqlite3")
        post = FakePost(status=status, body=b"")
        runner = JobRunner(store, KINDS, BASE_URL, secret_file, vault_root=vault_root, post=post)
        _enqueue(store)
        assert runner.run_once() is True
        assert store.list()[0].status == "dispatched"


def test_a_non_2xx_answer_sends_the_job_back_to_the_queue(
    store: JobStore, vault_root: Path, secret_file: Path, post: FakePost
) -> None:
    post.status = 500
    post.body = b'{"error": "boom"}'
    runner = JobRunner(store, KINDS, BASE_URL, secret_file, vault_root=vault_root, post=post)
    job_id = _enqueue(store)
    assert runner.run_once() is True
    job = store.list()[0]
    assert (job.id, job.status, job.attempts) == (job_id, "queued", 1)
    assert job.error is not None
    assert "500" in job.error
    assert "boom" in job.error


def test_a_job_fails_after_three_refused_attempts(
    store: JobStore, vault_root: Path, secret_file: Path, post: FakePost
) -> None:
    post.status = 502
    runner = JobRunner(store, KINDS, BASE_URL, secret_file, vault_root=vault_root, post=post)
    job_id = _enqueue(store)
    for _ in range(MAX_ATTEMPTS):
        runner.run_once()
    job = store.list()[0]
    assert (job.id, job.status, job.attempts) == (job_id, "failed", MAX_ATTEMPTS)
    assert len(post.calls) == MAX_ATTEMPTS
    assert store.claim() is None


def test_a_401_from_the_webhook_is_recorded(
    store: JobStore, vault_root: Path, secret_file: Path, post: FakePost
) -> None:
    post.status = 401
    post.body = b'{"error": "Invalid signature"}'
    runner = JobRunner(store, KINDS, BASE_URL, secret_file, vault_root=vault_root, post=post)
    _enqueue(store)
    runner.run_once()
    error = store.list()[0].error
    assert error is not None
    assert "401" in error and "Invalid signature" in error


def test_a_long_response_body_is_truncated_in_the_error(
    store: JobStore, vault_root: Path, secret_file: Path, post: FakePost
) -> None:
    post.status = 500
    post.body = b"x" * 5000
    runner = JobRunner(store, KINDS, BASE_URL, secret_file, vault_root=vault_root, post=post)
    _enqueue(store)
    runner.run_once()
    error = store.list()[0].error
    assert error is not None
    assert len(error) <= 500


def test_an_unreachable_webhook_fails_the_job_without_raising(
    store: JobStore, vault_root: Path, secret_file: Path, post: FakePost
) -> None:
    post.error = OSError("connection refused")
    runner = JobRunner(store, KINDS, BASE_URL, secret_file, vault_root=vault_root, post=post)
    _enqueue(store)
    assert runner.run_once() is True
    job = store.list()[0]
    assert (job.status, job.attempts) == ("queued", 1)
    assert job.error is not None
    assert "connection refused" in job.error


def test_an_unknown_kind_in_the_queue_fails_the_job(
    store: JobStore, vault_root: Path, secret_file: Path, post: FakePost
) -> None:
    runner = JobRunner(store, KINDS, BASE_URL, secret_file, vault_root=vault_root, post=post)
    _enqueue(store, kind="retired_kind")
    runner.run_once()
    assert post.calls == []
    job = store.list()[0]
    assert job.status == "queued"
    assert job.error is not None
    assert "retired_kind" in job.error


def test_a_missing_secret_file_fails_the_job(
    store: JobStore, vault_root: Path, tmp_path: Path, post: FakePost
) -> None:
    runner = JobRunner(store, KINDS, BASE_URL, tmp_path / "missing.secret", vault_root=vault_root, post=post)
    _enqueue(store)
    runner.run_once()
    assert post.calls == []
    job = store.list()[0]
    assert job.status == "queued"
    assert job.error is not None
    assert "secret" in job.error


def test_an_empty_secret_file_fails_the_job(
    store: JobStore, vault_root: Path, tmp_path: Path, post: FakePost
) -> None:
    path = tmp_path / "empty.secret"
    path.write_text("  \n", encoding="utf-8")
    runner = JobRunner(store, KINDS, BASE_URL, path, vault_root=vault_root, post=post)
    _enqueue(store)
    runner.run_once()
    assert post.calls == []
    assert store.list()[0].error is not None


# --- the result note -----------------------------------------------------------------------


def test_a_dispatched_job_finishes_when_its_note_exists(
    store: JobStore, runner: JobRunner, vault_root: Path
) -> None:
    job_id = _enqueue(store, note_path="ideas/heat-pumps.md")
    runner.run_once()
    runner.sweep()
    assert store.list()[0].status == "dispatched"
    (vault_root / "ideas" / "heat-pumps.md").write_text("---\ntype: idea\n---\n", encoding="utf-8")
    runner.sweep()
    job = store.list()[0]
    assert (job.id, job.status) == (job_id, "finished")
    assert job.finished is not None
    assert job.error is None


def test_a_dispatched_job_without_a_note_path_waits_for_the_timeout(
    tmp_path: Path, vault_root: Path, secret_file: Path, post: FakePost
) -> None:
    clock = ManualClock()
    store = _store_with_clock(tmp_path, clock)
    runner = JobRunner(store, KINDS, BASE_URL, secret_file, vault_root=vault_root, post=post, clock=clock)
    _enqueue(store)
    runner.run_once()
    runner.sweep()
    assert store.list()[0].status == "dispatched"
    clock.advance(DEFAULT_DISPATCH_TIMEOUT + 1)
    runner.sweep()
    job = store.list()[0]
    assert job.status == "failed"
    assert job.error is not None


def test_a_dispatched_job_fails_when_the_note_never_appears(
    tmp_path: Path, vault_root: Path, secret_file: Path, post: FakePost
) -> None:
    clock = ManualClock()
    store = _store_with_clock(tmp_path, clock)
    runner = JobRunner(store, KINDS, BASE_URL, secret_file, vault_root=vault_root, post=post, clock=clock)
    job_id = _enqueue(store, note_path="ideas/heat-pumps.md")
    runner.run_once()
    clock.advance(DEFAULT_DISPATCH_TIMEOUT - 1)
    runner.sweep()
    assert store.list()[0].status == "dispatched"
    clock.advance(2)
    runner.sweep()
    job = store.list()[0]
    assert (job.id, job.status) == (job_id, "failed")
    assert job.error is not None
    assert "note" in job.error
    assert len(post.calls) == 1


def test_a_timed_out_job_is_not_sent_again(
    tmp_path: Path, vault_root: Path, secret_file: Path, post: FakePost
) -> None:
    clock = ManualClock()
    store = _store_with_clock(tmp_path, clock)
    runner = JobRunner(store, KINDS, BASE_URL, secret_file, vault_root=vault_root, post=post, clock=clock)
    _enqueue(store)
    runner.run_once()
    clock.advance(DEFAULT_DISPATCH_TIMEOUT + 1)
    runner.sweep()
    assert runner.run_once() is False
    assert len(post.calls) == 1


def test_the_dispatch_timeout_defaults_to_one_hour() -> None:
    assert DEFAULT_DISPATCH_TIMEOUT == 3600.0


def test_the_dispatch_timeout_is_configurable(
    tmp_path: Path, vault_root: Path, secret_file: Path, post: FakePost
) -> None:
    clock = ManualClock()
    store = _store_with_clock(tmp_path, clock)
    runner = JobRunner(
        store,
        KINDS,
        BASE_URL,
        secret_file,
        vault_root=vault_root,
        post=post,
        clock=clock,
        dispatch_timeout=60,
    )
    _enqueue(store)
    runner.run_once()
    clock.advance(61)
    runner.sweep()
    assert store.list()[0].status == "failed"


def test_a_useless_dispatch_timeout_is_refused(store: JobStore, vault_root: Path, secret_file: Path) -> None:
    with pytest.raises(ValueError):
        JobRunner(store, KINDS, BASE_URL, secret_file, vault_root=vault_root, dispatch_timeout=0)


def test_a_note_outside_the_vault_does_not_finish_a_job(
    store: JobStore, runner: JobRunner, vault_root: Path
) -> None:
    _enqueue(store, note_path="ideas/heat-pumps.md")
    runner.run_once()
    (vault_root.parent / "heat-pumps.md").write_text("outside", encoding="utf-8")
    runner.sweep()
    assert store.list()[0].status == "dispatched"


def test_a_directory_is_not_a_result_note(store: JobStore, runner: JobRunner, vault_root: Path) -> None:
    _enqueue(store, note_path="ideas/heat-pumps.md")
    runner.run_once()
    (vault_root / "ideas" / "heat-pumps.md").mkdir()
    runner.sweep()
    assert store.list()[0].status == "dispatched"


def test_a_symlink_out_of_the_vault_is_not_a_result_note(
    store: JobStore, runner: JobRunner, vault_root: Path, tmp_path: Path
) -> None:
    _enqueue(store, note_path="ideas/heat-pumps.md")
    runner.run_once()
    outside = tmp_path / "outside.md"
    outside.write_text("outside", encoding="utf-8")
    (vault_root / "ideas" / "heat-pumps.md").symlink_to(outside)
    runner.sweep()
    assert store.list()[0].status == "dispatched"


def test_the_runner_needs_a_vault_root(tmp_path: Path, secret_file: Path) -> None:
    store = JobStore(tmp_path / "jobs.sqlite3")
    with pytest.raises(ValueError):
        JobRunner(store, KINDS, BASE_URL, secret_file, vault_root=tmp_path / "missing")


# --- stale jobs ----------------------------------------------------------------------------


def test_sweep_reclaims_a_stale_running_job(
    tmp_path: Path, vault_root: Path, secret_file: Path, post: FakePost
) -> None:
    clock = ManualClock()
    store = _store_with_clock(tmp_path, clock)
    runner = JobRunner(store, KINDS, BASE_URL, secret_file, vault_root=vault_root, post=post, clock=clock)
    job_id = _enqueue(store)
    store.claim()
    clock.advance(901)
    runner.sweep()
    job = store.list()[0]
    assert (job.id, job.status, job.attempts) == (job_id, "queued", 1)
    assert len(post.calls) == 0


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
    assert len(store.list("dispatched")) == 1


def test_one_job_at_a_time_by_default(runner: JobRunner) -> None:
    assert runner.max_concurrent == 1


def test_max_concurrent_must_be_positive(store: JobStore, vault_root: Path, secret_file: Path) -> None:
    with pytest.raises(ValueError):
        JobRunner(store, KINDS, BASE_URL, secret_file, vault_root=vault_root, max_concurrent=0)


def test_an_empty_base_url_is_refused(store: JobStore, vault_root: Path, secret_file: Path) -> None:
    with pytest.raises(ValueError):
        JobRunner(store, KINDS, "  ", secret_file, vault_root=vault_root)


class Stopper:
    """A sleep that records its argument, moves the clock and stops the runner after a few ticks."""

    def __init__(self, after: int = 2, clock: ManualClock | None = None) -> None:
        self.after = after
        self.clock = clock
        self.calls: list[float] = []
        self.runner: JobRunner | None = None

    def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)
        if self.clock is not None:
            self.clock.advance(seconds)
        if len(self.calls) >= self.after and self.runner is not None:
            self.runner.stop()


def test_run_forever_drains_the_queue_then_sleeps(
    store: JobStore, vault_root: Path, secret_file: Path, post: FakePost
) -> None:
    sleep = Stopper()
    runner = JobRunner(store, KINDS, BASE_URL, secret_file, vault_root=vault_root, post=post, sleep=sleep)
    sleep.runner = runner
    _enqueue(store, question="First question.")
    _enqueue(store, question="Second question.")
    runner.run_forever(poll_seconds=3.0)
    assert len(post.calls) == 2
    assert len(store.list("dispatched")) == 2
    assert sleep.calls == [3.0, 3.0]


def test_run_forever_finishes_a_job_whose_note_appears(
    tmp_path: Path, vault_root: Path, secret_file: Path, post: FakePost
) -> None:
    (vault_root / "ideas" / "heat-pumps.md").write_text("done", encoding="utf-8")
    clock = ManualClock()
    store = _store_with_clock(tmp_path, clock)
    sleep = Stopper(after=3, clock=clock)
    runner = JobRunner(
        store,
        KINDS,
        BASE_URL,
        secret_file,
        vault_root=vault_root,
        post=post,
        clock=clock,
        sleep=sleep,
        sweep_seconds=1.0,
    )
    sleep.runner = runner
    _enqueue(store, note_path="ideas/heat-pumps.md")
    runner.run_forever(poll_seconds=1.0)
    assert store.list()[0].status == "finished"


def test_run_forever_stops_without_touching_the_queue(
    store: JobStore, vault_root: Path, secret_file: Path, post: FakePost
) -> None:
    sleep = Stopper(after=1)
    runner = JobRunner(store, KINDS, BASE_URL, secret_file, vault_root=vault_root, post=post, sleep=sleep)
    sleep.runner = runner
    runner.run_forever(poll_seconds=1.0)
    assert post.calls == []
    assert sleep.calls == [1.0]


def test_run_forever_refuses_a_useless_poll_interval(runner: JobRunner) -> None:
    with pytest.raises(ValueError):
        runner.run_forever(poll_seconds=0)


def test_run_forever_survives_a_broken_store(vault_root: Path, secret_file: Path, post: FakePost) -> None:
    class BrokenStore(JobStore):
        def claim(self) -> Job | None:
            raise RuntimeError("database is gone")

    store = BrokenStore(secret_file.parent / "broken.sqlite3")
    sleep = Stopper(after=2)
    runner = JobRunner(store, KINDS, BASE_URL, secret_file, vault_root=vault_root, post=post, sleep=sleep)
    sleep.runner = runner
    runner.run_forever(poll_seconds=1.0)
    assert len(sleep.calls) == 2


# --- the outbox sweep ----------------------------------------------------------------------

OUTBOX_SECRET = "outbox-secret-not-a-real-one"
SCHEMA = """# Schema

```yaml
language: en
zones:
  inbox: inbox
  raw: raw
  calls: calls
  topics: topics
  knowledge: knowledge
  ideas: ideas
  projects: projects
  people: people
  work: work
  documents: documents
  answers: answers
  drawings: drawings
  archive: archive
  _brain: _brain
  _meta: _meta
types:
  capture: something captured and not processed yet
  idea: an idea, with its state
```
"""


def _research_delivery(
    job_id: str, reply: str = "Turbines use the current. See https://example.org/t."
) -> bytes:
    payload = {
        "hook_event_name": "post_llm_call",
        "profile": "default",
        "session_id": "session-1",
        "extra": {
            "platform": "webhook",
            "assistant_response": reply,
            "user_message": f"Background research job {job_id}. Tidal energy\n\nHow does it work?",
        },
        "delivery_id": "0123456789abcdef",
    }
    return json.dumps(payload).encode("utf-8")


def _outbox_runner(
    store: JobStore,
    vault_root: Path,
    secret_file: Path,
    post: FakePost,
    tmp_path: Path,
) -> tuple[JobRunner, Path]:
    """A runner with an importer, and the vault it writes into."""
    (vault_root / "_meta").mkdir(parents=True, exist_ok=True)
    (vault_root / "_meta" / "SCHEMA.md").write_text(SCHEMA, encoding="utf-8")
    for command in (
        ["init", "-q"],
        ["config", "user.name", "tester"],
        ["config", "user.email", "t@localhost"],
    ):
        subprocess.run(["git", "-C", str(vault_root), *command], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(vault_root), "commit", "-q", "--allow-empty", "-m", "init"],
        check=True,
        capture_output=True,
    )
    outbox_dir = tmp_path / "outbox"
    outbox_dir.mkdir()
    vault = Vault(VaultConfig(root=vault_root, quiet_seconds=0))
    importer = OutboxImporter(outbox_dir, vault, load_schema(vault_root), store, KINDS, "develop_idea")
    runner = JobRunner(
        store, KINDS, BASE_URL, secret_file, vault_root=vault_root, post=post, importer=importer
    )
    return runner, outbox_dir


def test_the_sweep_imports_a_stored_result_and_queues_the_follow_up(
    store: JobStore, vault_root: Path, secret_file: Path, post: FakePost, tmp_path: Path
) -> None:
    runner, outbox_dir = _outbox_runner(store, vault_root, secret_file, post, tmp_path)
    job_id = _enqueue(store)
    runner.run_once()
    (outbox_dir / "0123456789abcdef.json").write_bytes(_research_delivery(job_id))
    runner.sweep()
    assert store.get(job_id).status == "finished"  # type: ignore[union-attr]
    notes = list((vault_root / "raw" / "web").glob("*.md"))
    assert len(notes) == 1
    assert "https://example.org/t" in notes[0].read_text(encoding="utf-8")
    followups = store.list("queued")
    assert [job.kind for job in followups] == ["develop_idea"]
    assert "raw/web/" in followups[0].payload.question


def test_the_sweep_keeps_going_when_the_outbox_is_broken(
    store: JobStore, vault_root: Path, secret_file: Path, post: FakePost, tmp_path: Path
) -> None:
    class BrokenImporter(OutboxImporter):
        def sweep(self) -> list[Any]:
            raise RuntimeError("the outbox is gone")

    runner, outbox_dir = _outbox_runner(store, vault_root, secret_file, post, tmp_path)
    runner._importer = BrokenImporter(  # the sweep must not care
        outbox_dir,
        Vault(VaultConfig(root=vault_root, quiet_seconds=0)),
        load_schema(vault_root),
        store,
        KINDS,
        "develop_idea",
    )
    job_id = _enqueue(store, note_path="ideas/heat-pumps.md")
    runner.run_once()
    (vault_root / "ideas" / "heat-pumps.md").write_text("---\ntype: idea\n---\n", encoding="utf-8")
    runner.sweep()
    assert store.get(job_id).status == "finished"  # type: ignore[union-attr]


def test_a_runner_without_an_importer_only_watches_for_notes(store: JobStore, runner: JobRunner) -> None:
    job_id = _enqueue(store)
    runner.run_once()
    runner.sweep()
    assert store.get(job_id).status == "dispatched"  # type: ignore[union-attr]
