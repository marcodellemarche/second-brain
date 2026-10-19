"""The background job queue: enqueue here, a sidecar runs the job as a separate Hermes run.

Hermes' own background delegation is not used for this. The result of a delegated run comes back
through a path that skips the guard's result hook, so the parent session would take whatever the
child read as if it came from the user. Instead the work is queued here and a sidecar starts it as
a new Hermes run through a webhook route whose toolset is restricted by hand in the Hermes config.

The payload never carries vault content: a title, a question and an optional note path. A research
job therefore cannot see the vault, which is what makes it safe to give it the web.

The queue is one SQLite file. `claim` moves a job from queued to running inside an immediate
transaction, so two runners never take the same job. A job that fails goes back to the queue until
it has been tried three times, then it is `failed` with the last error. Enqueueing the same kind
with the same canonical payload while a job is still queued or running returns that job's id, so a
model that asks twice does not start two runs.

Error code of the tool: `unknown_kind`.
"""

from __future__ import annotations

import sqlite3
import threading
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from second_brain.core.types import Autonomy, Effect, ToolSpec
from second_brain.tools.registry import Tool
from second_brain.tools.vault import Provenance

JOB_HOSTS: tuple[str, ...] = ()
JOB_CREDENTIALS: tuple[str, ...] = ()

MAX_ATTEMPTS = 3
MAX_ERROR_LENGTH = 500
STATUSES: tuple[str, ...] = ("queued", "running", "finished", "failed")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    payload TEXT NOT NULL,
    status TEXT NOT NULL,
    requested_by TEXT NOT NULL,
    session TEXT NOT NULL,
    created REAL NOT NULL,
    started REAL,
    finished REAL,
    attempts INTEGER NOT NULL DEFAULT 0,
    error TEXT
);
CREATE INDEX IF NOT EXISTS jobs_status_created ON jobs (status, created);
"""


class JobError(Exception):
    """A refused job enqueue. `code` is stable and machine-readable."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


class JobKind(BaseModel):
    """One kind of background work and the webhook route that runs it."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    route: str = Field(
        pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$",
        description="Name of the Hermes webhook route, as configured by hand.",
    )
    description: str = Field(min_length=1, max_length=200, description="What this kind does.")


def _vault_path(value: str) -> str:
    """A vault-relative note path: forward slashes, no way out of the vault, a markdown file."""
    path = value.strip()
    if not path:
        raise ValueError("note_path must not be empty")
    if len(path) > 400:
        raise ValueError("note_path is too long")
    if path.startswith(("/", "\\")):
        raise ValueError("note_path must be relative to the vault")
    if "\\" in path:
        raise ValueError("note_path must use forward slashes")
    if any(ord(char) < 32 for char in path):
        raise ValueError("note_path may not contain control characters")
    segments = path.split("/")
    if any(segment in ("", ".", "..") for segment in segments):
        raise ValueError("note_path may not contain empty, '.' or '..' segments")
    if not path.endswith(".md"):
        raise ValueError("note_path must be a markdown file")
    return path


class JobPayload(BaseModel):
    """What the background run receives. Nothing here comes from the vault."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    title: str = Field(min_length=1, max_length=120, description="Short title of the job.")
    question: str = Field(
        min_length=1,
        max_length=2000,
        description="The work, written out in full: the background run cannot read the vault.",
    )
    note_path: str | None = Field(
        default=None,
        description="Vault-relative path of the note the result goes into, for example "
        "'ideas/foo.md'. Omit to let the run choose.",
    )

    @field_validator("title")
    @classmethod
    def _check_title(cls, value: str) -> str:
        title = " ".join(value.split())
        if not title:
            raise ValueError("title must not be empty")
        if len(title) > 120:
            raise ValueError("title must be at most 120 characters")
        return title

    @field_validator("question")
    @classmethod
    def _check_question(cls, value: str) -> str:
        question = value.strip()
        if not question:
            raise ValueError("question must not be empty")
        if any(ord(char) < 32 and char not in "\n\t" for char in question):
            raise ValueError("question may not contain control characters")
        return question

    @field_validator("note_path")
    @classmethod
    def _check_note_path(cls, value: str | None) -> str | None:
        return None if value is None else _vault_path(value)


class JobEnqueueParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: str = Field(min_length=1, max_length=64, description="Kind of job, one of the configured ones.")
    title: str = Field(min_length=1, max_length=120, description="Short title of the job.")
    question: str = Field(
        min_length=1,
        max_length=2000,
        description="The work, written out in full: the background run cannot read the vault, so "
        "the question must be self-contained.",
    )
    note_path: str | None = Field(
        default=None,
        description="Vault-relative path of the note the result goes into, for example "
        "'ideas/foo.md'. Omit to let the run choose.",
    )


@dataclass(frozen=True, slots=True)
class Job:
    id: str
    kind: str
    payload: JobPayload
    status: str
    requested_by: str
    session: str
    created: float
    started: float | None
    finished: float | None
    attempts: int
    error: str | None


def _job(row: sqlite3.Row, **overrides: Any) -> Job:
    values: dict[str, Any] = {
        "id": row["id"],
        "kind": row["kind"],
        "payload": JobPayload.model_validate_json(row["payload"]),
        "status": row["status"],
        "requested_by": row["requested_by"],
        "session": row["session"],
        "created": row["created"],
        "started": row["started"],
        "finished": row["finished"],
        "attempts": row["attempts"],
        "error": row["error"],
    }
    values.update(overrides)
    return Job(**values)


class JobStore:
    """One SQLite file with the queue. Every operation is serialized inside the process."""

    def __init__(self, path: Path | str, *, clock: Callable[[], float] = time.time) -> None:
        self._path = Path(path)
        self._clock = clock
        self._lock = threading.Lock()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(self._path, check_same_thread=False, isolation_level=None)
        self._connection.row_factory = sqlite3.Row
        with self._lock:
            self._connection.execute("PRAGMA journal_mode=WAL")
            self._connection.execute("PRAGMA busy_timeout=5000")
            self._connection.executescript(_SCHEMA)

    @property
    def path(self) -> Path:
        return self._path

    def enqueue(
        self,
        kind: str,
        payload: JobPayload | Mapping[str, Any],
        requested_by: str,
        session: str,
    ) -> str:
        """Queue a job and return its id, or the id of the identical job already waiting."""
        if not kind or not kind.strip():
            raise ValueError("kind must not be empty")
        if not requested_by.strip():
            raise ValueError("requested_by must not be empty")
        if not session.strip():
            raise ValueError("session must not be empty")
        canonical = JobPayload.model_validate(payload).model_dump_json()
        with self._lock:
            cursor = self._connection.cursor()
            cursor.execute("BEGIN IMMEDIATE")
            try:
                waiting = cursor.execute(
                    "SELECT id FROM jobs WHERE kind = ? AND payload = ? AND status IN ('queued', 'running')"
                    " ORDER BY created, rowid LIMIT 1",
                    (kind, canonical),
                ).fetchone()
                if waiting is not None:
                    cursor.execute("COMMIT")
                    return str(waiting["id"])
                job_id = uuid.uuid4().hex
                cursor.execute(
                    "INSERT INTO jobs"
                    " (id, kind, payload, status, requested_by, session, created, started, finished,"
                    " attempts, error)"
                    " VALUES (?, ?, ?, 'queued', ?, ?, ?, NULL, NULL, 0, NULL)",
                    (job_id, kind, canonical, requested_by, session, self._clock()),
                )
                cursor.execute("COMMIT")
            except BaseException:
                cursor.execute("ROLLBACK")
                raise
        return job_id

    def claim(self) -> Job | None:
        """Take the oldest queued job and mark it running, in one immediate transaction."""
        with self._lock:
            cursor = self._connection.cursor()
            cursor.execute("BEGIN IMMEDIATE")
            try:
                row = cursor.execute(
                    "SELECT * FROM jobs WHERE status = 'queued' ORDER BY created, rowid LIMIT 1"
                ).fetchone()
                if row is None:
                    cursor.execute("COMMIT")
                    return None
                started = self._clock()
                cursor.execute(
                    "UPDATE jobs SET status = 'running', started = ?, attempts = attempts + 1"
                    " WHERE id = ? AND status = 'queued'",
                    (started, row["id"]),
                )
                if cursor.rowcount != 1:
                    raise RuntimeError(f"job {row['id']} changed while it was being claimed")
                cursor.execute("COMMIT")
            except BaseException:
                cursor.execute("ROLLBACK")
                raise
        return _job(row, status="running", started=started, attempts=row["attempts"] + 1)

    def finish(self, job_id: str) -> None:
        """Mark a running job finished. A job that is not running is left alone."""
        with self._lock:
            self._connection.execute(
                "UPDATE jobs SET status = 'finished', finished = ?, error = NULL"
                " WHERE id = ? AND status = 'running'",
                (self._clock(), job_id),
            )

    def fail(self, job_id: str, error: str) -> None:
        """Record the error, and put the job back in the queue until it has been tried enough."""
        message = " ".join(str(error).split())[:MAX_ERROR_LENGTH] or "unknown error"
        with self._lock:
            row = self._connection.execute("SELECT attempts FROM jobs WHERE id = ?", (job_id,)).fetchone()
            if row is None:
                return
            if row["attempts"] < MAX_ATTEMPTS:
                self._connection.execute(
                    "UPDATE jobs SET status = 'queued', started = NULL, error = ?"
                    " WHERE id = ? AND status = 'running'",
                    (message, job_id),
                )
                return
            self._connection.execute(
                "UPDATE jobs SET status = 'failed', finished = ?, error = ?"
                " WHERE id = ? AND status = 'running'",
                (self._clock(), message, job_id),
            )

    def list(self, status: str | None = None, limit: int = 50) -> list[Job]:
        """The most recent jobs first, optionally only those in one status."""
        if status is not None and status not in STATUSES:
            raise ValueError(f"unknown job status: {status!r}")
        if limit < 1:
            raise ValueError("limit must be at least 1")
        if status is None:
            query = "SELECT * FROM jobs ORDER BY created DESC, rowid DESC LIMIT ?"
            params: tuple[Any, ...] = (limit,)
        else:
            query = "SELECT * FROM jobs WHERE status = ? ORDER BY created DESC, rowid DESC LIMIT ?"
            params = (status, limit)
        with self._lock:
            rows = self._connection.execute(query, params).fetchall()
        return [_job(row) for row in rows]


def _refused(exc: JobError) -> dict[str, Any]:
    return {"ok": False, "code": exc.code, "message": exc.message}


def _guarded(run: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    """Run an enqueue, turning a refusal into a result instead of an exception."""
    try:
        return {"ok": True, **run()}
    except JobError as exc:
        return _refused(exc)


def job_tools(store: JobStore, kinds: dict[str, JobKind]) -> list[Tool]:
    def job_enqueue(params: BaseModel, provenance: Provenance) -> dict[str, Any]:
        assert isinstance(params, JobEnqueueParams)

        def run() -> dict[str, Any]:
            kind = params.kind.strip()
            if kind not in kinds:
                known = ", ".join(sorted(kinds)) or "none"
                raise JobError("unknown_kind", f"no job kind {kind!r}, configured: {known}")
            payload = JobPayload(title=params.title, question=params.question, note_path=params.note_path)
            job_id = store.enqueue(kind, payload, provenance.requested_by, provenance.session)
            return {"job_id": job_id, "kind": kind, "status": "queued"}

        return _guarded(run)

    return [
        Tool(
            spec=ToolSpec(
                name="job_enqueue",
                description=(
                    "Queue work to run in the background: developing an idea, researching a "
                    "question, answering an open question in a note. The background run receives "
                    "only the title and the question, never the vault, so write the question out "
                    "in full. The result lands in a note. The same kind with the same question "
                    "while one is still waiting returns the job already queued."
                ),
                params=JobEnqueueParams,
                effect=Effect.INTERNAL,
                autonomy=Autonomy.NOTIFY,
                hosts=JOB_HOSTS,
                credentials=JOB_CREDENTIALS,
            ),
            handler=job_enqueue,
        )
    ]
