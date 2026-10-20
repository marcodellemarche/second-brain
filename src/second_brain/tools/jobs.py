"""The background job queue: enqueue here, a sidecar runs the job as a separate Hermes run.

Hermes' own background delegation is not used for this. The result of a delegated run comes back
through a path that skips the guard's result hook, so the parent session would take whatever the
child read as if it came from the user. Instead the work is queued here and a sidecar starts it as
a new Hermes run through a webhook route whose toolset is restricted by hand in the Hermes config.

The payload never carries vault content: a title, a question and an optional note path. A research
job therefore cannot see the vault. It can still reach the web, and whatever the model writes into
the question leaves the system, so a kind with `web` set is enqueued through its own tool with
effect `external`, which puts it under the guard's rule of two. Kinds without the web go through
`job_enqueue`, which only changes our own data.

The queue is one SQLite file. `claim` moves a job from queued to running inside an immediate
transaction, so two runners never take the same job. A 2xx from the webhook means Hermes accepted
the run, not that the work is done, so the job becomes `dispatched` and only `finished` once its
result note exists. A job that cannot be sent goes back to the queue until it has been tried three
times, then it is `failed` with the last error. A job left `running` for too long (the runner died
between the claim and the send) is reclaimed and counted as a used attempt. Enqueueing the same
kind with the same canonical payload while a job is still queued, running or dispatched returns
that job's id, so a model that asks twice does not start two runs.

Error codes of the tools: `unknown_kind`, `wrong_tool` and `bad_note_path`.
"""

from __future__ import annotations

import sqlite3
import threading
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
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
MAX_QUESTION_CHARS = 2000
DEFAULT_STALE_AFTER = 15 * 60.0
STALE_ERROR = "the run was claimed and never reported back"
STATUSES: tuple[str, ...] = ("queued", "running", "dispatched", "finished", "failed")
_IN_FLIGHT: tuple[str, ...] = ("queued", "running", "dispatched")

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
    dispatched REAL,
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
    web: bool = Field(
        description="True when the route can reach the web, which makes enqueueing it an "
        "external call. There is no default: forgetting it must not silently relax the guard."
    )


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
        max_length=MAX_QUESTION_CHARS,
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
    """The params of both enqueue tools: they differ in effect, not in what they carry."""

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
        description="Vault-relative path of the note the result goes into: a note inside a vault "
        "zone, e.g. 'knowledge/x.md'. Omit to let the run choose.",
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
    dispatched: float | None
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
        "dispatched": row["dispatched"],
        "finished": row["finished"],
        "attempts": row["attempts"],
        "error": row["error"],
    }
    values.update(overrides)
    return Job(**values)


class JobStore:
    """One SQLite file with the queue. Every operation is serialized inside the process."""

    def __init__(
        self,
        path: Path | str,
        *,
        clock: Callable[[], float] = time.time,
        stale_after: float = DEFAULT_STALE_AFTER,
    ) -> None:
        if stale_after <= 0:
            raise ValueError("stale_after must be positive")
        self._path = Path(path)
        self._clock = clock
        self._stale_after = stale_after
        self._lock = threading.Lock()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(self._path, check_same_thread=False, isolation_level=None)
        self._connection.row_factory = sqlite3.Row
        with self._lock:
            self._connection.execute("PRAGMA journal_mode=WAL")
            self._connection.execute("PRAGMA busy_timeout=5000")
            self._connection.executescript(_SCHEMA)
            self._migrate()

    def _migrate(self) -> None:
        """Add the columns a queue file from an older release is missing."""
        columns = {row["name"] for row in self._connection.execute("PRAGMA table_info(jobs)")}
        if "dispatched" not in columns:
            self._connection.execute("ALTER TABLE jobs ADD COLUMN dispatched REAL")

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
                    "SELECT id FROM jobs WHERE kind = ? AND payload = ?"
                    " AND status IN ('queued', 'running', 'dispatched')"
                    " ORDER BY created, rowid LIMIT 1",
                    (kind, canonical),
                ).fetchone()
                if waiting is not None:
                    cursor.execute("COMMIT")
                    return str(waiting["id"])
                job_id = uuid.uuid4().hex
                cursor.execute(
                    "INSERT INTO jobs"
                    " (id, kind, payload, status, requested_by, session, created, started,"
                    " dispatched, finished, attempts, error)"
                    " VALUES (?, ?, ?, 'queued', ?, ?, ?, NULL, NULL, NULL, 0, NULL)",
                    (job_id, kind, canonical, requested_by, session, self._clock()),
                )
                cursor.execute("COMMIT")
            except BaseException:
                cursor.execute("ROLLBACK")
                raise
        return job_id

    def get(self, job_id: str) -> Job | None:
        """One job by id, or None when the queue never had it."""
        with self._lock:
            row = self._connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return None if row is None else _job(row)

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
                    "UPDATE jobs SET status = 'running', started = ?, dispatched = NULL,"
                    " attempts = attempts + 1 WHERE id = ? AND status = 'queued'",
                    (started, row["id"]),
                )
                if cursor.rowcount != 1:
                    raise RuntimeError(f"job {row['id']} changed while it was being claimed")
                cursor.execute("COMMIT")
            except BaseException:
                cursor.execute("ROLLBACK")
                raise
        return _job(row, status="running", started=started, dispatched=None, attempts=row["attempts"] + 1)

    def dispatch(self, job_id: str) -> None:
        """Record that Hermes accepted the run. The work itself is not done yet."""
        with self._lock:
            self._connection.execute(
                "UPDATE jobs SET status = 'dispatched', dispatched = ?, error = NULL"
                " WHERE id = ? AND status = 'running'",
                (self._clock(), job_id),
            )

    def mark_finished(self, job_id: str) -> None:
        """Mark a job finished: its result note exists, so the run did what it was asked."""
        with self._lock:
            self._connection.execute(
                "UPDATE jobs SET status = 'finished', finished = ?, error = NULL"
                " WHERE id = ? AND status IN ('running', 'dispatched')",
                (self._clock(), job_id),
            )

    def fail(self, job_id: str, error: str, *, retry: bool = True) -> None:
        """Record the error, and put the job back in the queue until it has been tried enough.

        `retry=False` gives up immediately: a run that was already dispatched may still be
        working, so sending it again would duplicate the work instead of recovering it.
        """
        message = " ".join(str(error).split())[:MAX_ERROR_LENGTH] or "unknown error"
        with self._lock:
            row = self._connection.execute(
                "SELECT status, attempts FROM jobs WHERE id = ?", (job_id,)
            ).fetchone()
            if row is None or row["status"] not in ("running", "dispatched"):
                return
            if retry and row["attempts"] < MAX_ATTEMPTS:
                self._connection.execute(
                    "UPDATE jobs SET status = 'queued', started = NULL, dispatched = NULL, error = ?"
                    " WHERE id = ? AND status IN ('running', 'dispatched')",
                    (message, job_id),
                )
                return
            self._connection.execute(
                "UPDATE jobs SET status = 'failed', finished = ?, error = ?"
                " WHERE id = ? AND status IN ('running', 'dispatched')",
                (self._clock(), message, job_id),
            )

    def reclaim_stale(self) -> list[str]:
        """Return the jobs left running for too long to the queue, or fail them for good.

        A job is running only between the claim and the send, so one that has been running for
        longer than `stale_after` belongs to a runner that died. The attempt it used is kept, so a
        job that keeps killing its runner stops after `MAX_ATTEMPTS`.
        """
        now = self._clock()
        reclaimed: list[str] = []
        with self._lock:
            cursor = self._connection.cursor()
            cursor.execute("BEGIN IMMEDIATE")
            try:
                rows = cursor.execute(
                    "SELECT id, attempts FROM jobs WHERE status = 'running'"
                    " AND started IS NOT NULL AND started < ?",
                    (now - self._stale_after,),
                ).fetchall()
                for row in rows:
                    if row["attempts"] >= MAX_ATTEMPTS:
                        cursor.execute(
                            "UPDATE jobs SET status = 'failed', finished = ?, error = ?"
                            " WHERE id = ? AND status = 'running'",
                            (now, STALE_ERROR, row["id"]),
                        )
                    else:
                        cursor.execute(
                            "UPDATE jobs SET status = 'queued', started = NULL, dispatched = NULL,"
                            " error = ? WHERE id = ? AND status = 'running'",
                            (STALE_ERROR, row["id"]),
                        )
                    reclaimed.append(str(row["id"]))
                cursor.execute("COMMIT")
            except BaseException:
                cursor.execute("ROLLBACK")
                raise
        return reclaimed

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


def _check_note_folder(note_path: str, note_folders: Sequence[str]) -> None:
    """Refuse a note path whose first segment is no zone of this vault.

    The job runner finishes a job when its note path exists, so a path in a folder nothing writes
    to leaves the job hanging instead of failing it.
    """
    head = note_path.split("/", 1)[0]
    if head not in note_folders:
        raise JobError("bad_note_path", f"note_path must be inside one of: {', '.join(note_folders)}")


def _enqueue(
    store: JobStore,
    accepted: Mapping[str, JobKind],
    kinds: Mapping[str, JobKind],
    tool_name: str,
    note_folders: Sequence[str] | None = None,
    *,
    note_path_required: bool = False,
) -> Callable[[BaseModel, Provenance], dict[str, Any]]:
    """The handler of one enqueue tool, accepting only the kinds of its own effect class.

    `note_path_required` is for research: the follow-up job is told to write the final note at
    that path, and without it the job has nothing to look for and waits out its whole timeout
    after the work is done.
    """

    def handler(params: BaseModel, provenance: Provenance) -> dict[str, Any]:
        assert isinstance(params, JobEnqueueParams)

        def run() -> dict[str, Any]:
            kind = params.kind.strip()
            if kind not in accepted:
                if kind in kinds:
                    other = "research_enqueue" if kinds[kind].web else "job_enqueue"
                    raise JobError(
                        "wrong_tool",
                        f"job kind {kind!r} is not enqueued with {tool_name}, use {other}",
                    )
                known = ", ".join(sorted(accepted)) or "none"
                raise JobError("unknown_kind", f"no job kind {kind!r}, configured: {known}")
            if note_path_required and not params.note_path:
                raise JobError(
                    "bad_note_path",
                    f"{tool_name} needs the note_path the result goes into: the follow-up job "
                    "writes the final note there",
                )
            payload = JobPayload(title=params.title, question=params.question, note_path=params.note_path)
            if note_folders is not None and payload.note_path is not None:
                _check_note_folder(payload.note_path, note_folders)
            job_id = store.enqueue(kind, payload, provenance.requested_by, provenance.session)
            return {"job_id": job_id, "kind": kind, "status": "queued"}

        try:
            return {"ok": True, **run()}
        except JobError as exc:
            return _refused(exc)

    return handler


def job_tools(
    store: JobStore,
    kinds: Mapping[str, JobKind],
    note_folders: Sequence[str] | None = None,
) -> list[Tool]:
    """The enqueue tools this brain needs, one per effect class.

    A kind whose route can reach the web gets its own external tool, so the guard's rule of two
    sees the call. A brain with no web kind does not get the research tool at all.

    `note_folders` are the vault folders a result note may live in. None skips the check, which is
    what a caller with no schema gets.
    """
    web_kinds = {name: kind for name, kind in kinds.items() if kind.web}
    local_kinds = {name: kind for name, kind in kinds.items() if not kind.web}
    tools: list[Tool] = []
    if web_kinds:
        tools.append(
            Tool(
                spec=ToolSpec(
                    name="research_enqueue",
                    description=(
                        "Queue research to run in the background with web access. The background "
                        "run receives only the title and the question, never the vault, but "
                        "whatever the question says leaves the system, so it must be the user's "
                        "own question and must not carry private content. The result lands in a "
                        "note. The same kind with the same question while one is still in flight "
                        "returns the job already queued."
                    ),
                    params=JobEnqueueParams,
                    effect=Effect.EXTERNAL,
                    autonomy=Autonomy.AUTO,
                    hosts=JOB_HOSTS,
                    credentials=JOB_CREDENTIALS,
                ),
                handler=_enqueue(
                    store, web_kinds, kinds, "research_enqueue", note_folders, note_path_required=True
                ),
            )
        )
    if local_kinds:
        tools.append(
            Tool(
                spec=ToolSpec(
                    name="job_enqueue",
                    description=(
                        "Queue work to run in the background without web access: developing an "
                        "idea, answering an open question in a note. The background run receives "
                        "only the title and the question, never the vault, so write the question "
                        "out in full. The result lands in a note. The same kind with the same "
                        "question while one is still in flight returns the job already queued."
                    ),
                    params=JobEnqueueParams,
                    effect=Effect.INTERNAL,
                    autonomy=Autonomy.NOTIFY,
                    hosts=JOB_HOSTS,
                    credentials=JOB_CREDENTIALS,
                ),
                handler=_enqueue(store, local_kinds, kinds, "job_enqueue", note_folders),
            )
        )
    return tools
