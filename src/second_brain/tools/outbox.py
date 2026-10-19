"""Bring the reply of a research run back into the vault: Hermes outbound hook -> untrusted raw note.

A research job runs on a Hermes webhook route whose toolset is `[web, no_mcp]`: no vault, no
brain_tools, so it cannot write its result anywhere. A webhook route cannot deliver its reply to a
file or a URL either (the route's `deliver` is `log`). What Hermes does offer is `hooks.outbound`,
a list of targets it POSTs to on lifecycle events. The facts below are read from Hermes v0.22.2,
in `agent/outbound_webhooks.py` unless another file is named.

- The event that carries a finished reply is `post_llm_call`: fired once per turn after the tool
  loop, with `assistant_response=final_response` (`agent/turn_finalizer.py:469-479`).
- A target is `{url, events, secret_env | secret, matcher, timeout}`. `url` must be http(s), the
  timeout is clamped to 1..60 seconds, and `secret_env` (an env var name) wins over an inline
  `secret` (`:158-220`).
- The body is JSON with `hook_event_name`, `profile`, the shell-hook fields (`tool_name`,
  `tool_input`, `session_id`, `cwd`, `profile`, `extra`) and `delivery_id` + `timestamp`
  (`:241-252`, `agent/shell_hooks.py:79-94`). The reply text is `extra["assistant_response"]`, the
  platform is `extra["platform"]` ("webhook" for a route), the run's session is `session_id`, and
  `extra["user_message"]` is the prompt Hermes rendered for the route.
- The signature is `X-Hermes-Signature-256: sha256=<hex>`, the HMAC-SHA256 of the raw body with
  the target's secret, and it carries no timestamp: replay protection is the `delivery_id` inside
  the signed body (`:255-263`).
- `matcher` is honored only for `pre_tool_call` / `post_tool_call`; on any other event it is
  dropped with a warning (`:189-195`). It therefore cannot select our research routes, so the
  receiver checks the payload itself and refuses everything that is not the result of a job kind
  whose route has the web.
- Delivery is fire-and-forget through a bounded queue, with 4xx and 3xx final and connection
  errors or 5xx retried twice (`:309-345`). A receiver that is down loses the delivery, so a
  result that never arrives is caught by the job's own dispatch timeout instead.

Two halves, both here because they share the payload shape: `OutboxReceiver` verifies one delivery
and stores it as a JSON file, `OutboxImporter` turns the stored files into untrusted raw notes and
hands the job back to the vault. A file is written atomically, so the importer never reads half of
one, and a file that cannot be imported is moved to `failed/` with a reason next to it instead of
being retried forever.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import tempfile
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field

from second_brain.core.schema import VaultSchema
from second_brain.core.types import Zone
from second_brain.tools.jobs import MAX_QUESTION_CHARS, Job, JobKind, JobPayload, JobStore
from second_brain.tools.vault import Provenance, Vault, VaultError

log = logging.getLogger(__name__)

OUTBOX_PATH = "/outbox"
SIGNATURE_HEADER = "x-hermes-signature-256"
EVENT_HEADER = "x-hermes-event"
REPLY_EVENT = "post_llm_call"
WEB_PLATFORM = "webhook"
JOB_PROMPT_PREFIX = "Background research job "
DONE_DIR = "done"
FAILED_DIR = "failed"
MAX_BODY_BYTES = 256 * 1024
MAX_SOURCES = 20
MAX_TITLE_CHARS = 60

_SIGNATURE_PREFIX = "sha256="
_DELIVERY_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")
_JOB_PROMPT = re.compile(rf"{re.escape(JOB_PROMPT_PREFIX)}([0-9a-f]{{32}})(?![0-9a-f])")
_URL = re.compile(r"https?://[^\s<>()\[\]\"']+")
_UNSAFE_NAME = re.compile(r"[^A-Za-z0-9 ._-]+")
_DOTS = re.compile(r"\.{2,}")


class OutboxError(RuntimeError):
    """The receiver cannot verify anything: an unreadable or empty secret file."""


@dataclass(frozen=True, slots=True)
class Receipt:
    """What the receiver answers. `status` is the HTTP status, `code` is stable and readable."""

    status: int
    code: str
    message: str
    path: Path | None = None


@dataclass(frozen=True, slots=True)
class ImportResult:
    """One outbox file, imported or refused. `note` is the vault path it was written to."""

    source: str
    job_id: str | None
    note: str | None
    error: str | None

    @property
    def ok(self) -> bool:
        return self.error is None


def _read_secret(secret_file: Path) -> bytes:
    try:
        secret = secret_file.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise OutboxError(f"cannot read the outbox secret: {exc}") from exc
    if not secret:
        raise OutboxError(f"the outbox secret in {secret_file} is empty")
    return secret.encode("utf-8")


def _atomic_write(target: Path, payload: bytes) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    handle_fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=".tmp-")
    try:
        with os.fdopen(handle_fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, target)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def job_id_from_prompt(user_message: Any) -> str | None:
    """The job id the route prompt starts with, or None when this prompt is not one of ours."""
    if not isinstance(user_message, str):
        return None
    first = user_message.strip().split("\n", 1)[0].strip()
    match = _JOB_PROMPT.match(first)
    return match.group(1) if match else None


def _read_result(
    payload: Any,
    store: JobStore,
    kinds: Mapping[str, JobKind],
) -> tuple[Job, str, dict[str, Any]] | str:
    """The job, the reply text and the body of one delivery, or why it is not a research result."""
    if not isinstance(payload, Mapping):
        return "the body is not a JSON object"
    event = payload.get("hook_event_name")
    if event != REPLY_EVENT:
        return f"not a finished turn: {event!r}"
    extra = payload.get("extra")
    if not isinstance(extra, Mapping):
        return "the delivery carries no extra fields"
    platform = extra.get("platform")
    if platform != WEB_PLATFORM:
        return f"not a webhook route: platform {platform!r}"
    reply = extra.get("assistant_response")
    if not isinstance(reply, str) or not reply.strip():
        return "the turn produced no reply text"
    job_id = job_id_from_prompt(extra.get("user_message"))
    if job_id is None:
        return "the route prompt does not name a job"
    job = store.get(job_id)
    if job is None:
        return f"the queue has no job {job_id}"
    kind = kinds.get(job.kind)
    if kind is None or not kind.web:
        return f"job {job_id} ({job.kind}) is not a research job"
    return job, reply, dict(payload)


class OutboxReceiver:
    """Verify one Hermes outbound delivery and keep the ones that are a research result.

    The signature is checked first and with the secret read on every request, so a rotated secret
    needs no restart. Everything that is not the finished turn of a research route is refused: the
    endpoint is on the internal network and outside the MCP bearer token, so the signature is the
    only thing that says a delivery comes from Hermes.
    """

    def __init__(
        self,
        outbox_dir: Path | str,
        secret_file: Path | str,
        store: JobStore,
        kinds: Mapping[str, JobKind],
    ) -> None:
        self._outbox = Path(outbox_dir)
        self._outbox.mkdir(parents=True, exist_ok=True)
        self._secret_file = Path(secret_file)
        self._store = store
        self._kinds = dict(kinds)

    @property
    def outbox_dir(self) -> Path:
        return self._outbox

    def handle(self, headers: Mapping[str, str], body: bytes) -> Receipt:
        """Check one POST and store it. Never raises: the answer is a status and a code."""
        provided = {key.lower(): value for key, value in headers.items()}
        try:
            secret = _read_secret(self._secret_file)
        except OutboxError as exc:
            return Receipt(503, "no_secret", str(exc))
        if len(body) > MAX_BODY_BYTES:
            return Receipt(413, "too_large", f"the body is over {MAX_BODY_BYTES} bytes")
        signature = provided.get(SIGNATURE_HEADER, "")
        digest = hmac.new(secret, body, hashlib.sha256).hexdigest()
        if not signature.startswith(_SIGNATURE_PREFIX):
            return Receipt(401, "unsigned", f"missing {SIGNATURE_HEADER}")
        if not hmac.compare_digest(signature[len(_SIGNATURE_PREFIX) :], digest):
            return Receipt(401, "bad_signature", f"{SIGNATURE_HEADER} does not match the body")
        event = provided.get(EVENT_HEADER, "")
        if event and event != REPLY_EVENT:
            return Receipt(403, "wrong_event", f"{EVENT_HEADER} is {event!r}, not {REPLY_EVENT!r}")
        try:
            payload = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            return Receipt(400, "bad_json", f"the body is not JSON: {exc}")
        result = _read_result(payload, self._store, self._kinds)
        if isinstance(result, str):
            return Receipt(403, "not_a_research_result", result)
        job, _reply, body_fields = result
        delivery = body_fields.get("delivery_id")
        name = (
            delivery if isinstance(delivery, str) and _DELIVERY_ID.fullmatch(delivery) else uuid.uuid4().hex
        )
        target = self._outbox / f"{name}.json"
        if target.exists():
            return Receipt(200, "duplicate", f"delivery {name} was already stored", target)
        _atomic_write(target, body)
        log.info("stored the result of job %s in %s", job.id, target)
        return Receipt(202, "stored", f"the result of job {job.id} is stored", target)


def _safe_name(title: str) -> str:
    """A file name from a job title: one line, no separators, short enough to stay readable."""
    name = _UNSAFE_NAME.sub(" ", " ".join(title.split()))
    name = _DOTS.sub(".", " ".join(name.split())).strip(" .-_")
    return name[:MAX_TITLE_CHARS].strip(" .-_") or "research"


def sources_in(text: str) -> list[str]:
    """The URLs the reply mentions, in order and without the punctuation around them."""
    found: list[str] = []
    for match in _URL.finditer(text):
        url = match.group(0).rstrip(".,;:!?)]}'\"")
        if url not in found:
            found.append(url)
        if len(found) >= MAX_SOURCES:
            break
    return found


class OutboxImporter:
    """Turn the stored deliveries into untrusted raw notes, and hand the job back to the vault.

    A note lands in `raw/web/`, named after the day and the job title, with `trust: untrusted` and
    the URLs the reply mentions. The research job is finished once its result is in the vault, and
    a follow-up job of `followup_kind` is queued with the original question and the raw note path,
    so a route that has the vault writes the note the job asked for. Importing the same file twice
    cannot happen: it is moved out of the way before the follow-up is queued.
    """

    def __init__(
        self,
        outbox_dir: Path | str,
        vault: Vault,
        schema: VaultSchema,
        store: JobStore,
        kinds: Mapping[str, JobKind],
        followup_kind: str,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._outbox = Path(outbox_dir)
        self._vault = vault
        self._schema = schema
        self._store = store
        self._kinds = dict(kinds)
        if not followup_kind.strip():
            raise ValueError("followup_kind must not be empty")
        self._followup_kind = followup_kind
        self._clock = clock

    @property
    def outbox_dir(self) -> Path:
        return self._outbox

    def sweep(self) -> list[ImportResult]:
        """Import every stored delivery. One bad file never stops the sweep."""
        if not self._outbox.is_dir():
            return []
        results: list[ImportResult] = []
        for path in sorted(self._outbox.glob("*.json")):
            try:
                results.append(self.import_file(path))
            except Exception:  # a broken file must not take the sidecar down
                log.exception("could not import %s", path)
                results.append(ImportResult(path.name, None, None, "unexpected import failure"))
        return results

    def import_file(self, path: Path) -> ImportResult:
        """Import one stored delivery, moving it to `done/` or to `failed/` with a reason."""
        try:
            payload = json.loads(path.read_bytes())
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            return self._fail(path, None, f"the stored delivery is not JSON: {exc}")
        result = _read_result(payload, self._store, self._kinds)
        if isinstance(result, str):
            return self._fail(path, None, result)
        job, reply, _fields = result
        created = datetime.fromtimestamp(self._clock(), UTC).strftime("%Y-%m-%d")
        note_path = self._note_path(created, job)
        try:
            self._vault.write(note_path, self._render(job, reply, created), self._provenance(job))
        except VaultError as exc:
            return self._fail(path, job.id, f"{exc.code}: {exc.message}")
        self._move(path, DONE_DIR)
        self._store.mark_finished(job.id)
        followup = self._enqueue_followup(job, note_path)
        log.info("imported the result of job %s as %s, follow-up %s", job.id, note_path, followup)
        return ImportResult(path.name, job.id, note_path, None)

    def _note_path(self, created: str, job: Job) -> str:
        folder = self._schema.folder(Zone.RAW)
        stem = f"{created} {_safe_name(job.payload.title)}"
        candidate = f"{folder}/web/{stem}.md"
        if not (self._vault.root / candidate).exists():
            return candidate
        # A second run for the same title on the same day would overwrite the first result.
        return f"{folder}/web/{stem} ({job.id[:8]}).md"

    def _render(self, job: Job, reply: str, created: str) -> str:
        frontmatter: dict[str, Any] = {
            "type": "capture",
            "created": created,
            "trust": "untrusted",
            "sources": sources_in(reply),
            "job": job.id,
        }
        head = yaml.safe_dump(frontmatter, sort_keys=False, allow_unicode=True).rstrip()
        return f"---\n{head}\n---\n\n{reply.strip()}\n"

    @staticmethod
    def _provenance(job: Job) -> Provenance:
        return Provenance(
            requested_by=f"job:{job.id}",
            reason=f"result of research job {job.id}",
            source_integrity="untrusted",
            session=job.session,
        )

    def _enqueue_followup(self, job: Job, note_path: str) -> str | None:
        """Queue the vault job that writes the final note from the raw one."""
        suffix = f"\n\nThe research result is in {note_path}. Write the note from it."
        question = job.payload.question[: MAX_QUESTION_CHARS - len(suffix)]
        payload = JobPayload(
            title=job.payload.title,
            question=f"{question.rstrip()}{suffix}",
            note_path=job.payload.note_path,
        )
        try:
            return self._store.enqueue(
                self._followup_kind,
                payload,
                f"job:{job.id}",
                job.session,
            )
        except (ValueError, OSError) as exc:
            log.error("could not queue the follow-up of job %s: %s", job.id, exc)
            return None

    def _move(self, path: Path, subdir: str) -> Path:
        target_dir = self._outbox / subdir
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / path.name
        if target.exists():
            target = target_dir / f"{path.stem}-{uuid.uuid4().hex[:8]}{path.suffix}"
        os.replace(path, target)
        return target

    def _fail(self, path: Path, job_id: str | None, reason: str) -> ImportResult:
        log.warning("not importing %s: %s", path.name, reason)
        moved = self._move(path, FAILED_DIR)
        _atomic_write(moved.with_suffix(".reason.txt"), f"{reason}\n".encode())
        return ImportResult(path.name, job_id, None, reason)


class OutboxConfig(BaseModel):
    """Where the replies of research runs land, and the kind that writes the final note.

    `secret_file` holds the same secret Hermes gets as `HERMES_OUTBOX_SECRET`: it is the only thing
    that says a delivery comes from Hermes, because this endpoint is not behind the MCP token.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    dir: Path
    secret_file: Path
    research_followup_kind: str = Field(min_length=1, max_length=64)

    def importer(
        self,
        vault: Vault,
        schema: VaultSchema,
        store: JobStore,
        kinds: Mapping[str, JobKind],
        *,
        clock: Callable[[], float] = time.time,
    ) -> OutboxImporter:
        return OutboxImporter(
            self.dir,
            vault,
            schema,
            store,
            kinds,
            self.research_followup_kind,
            clock=clock,
        )

    def receiver(self, store: JobStore, kinds: Mapping[str, JobKind]) -> OutboxReceiver:
        return OutboxReceiver(self.dir, self.secret_file, store, kinds)


__all__ = [
    "DONE_DIR",
    "EVENT_HEADER",
    "FAILED_DIR",
    "JOB_PROMPT_PREFIX",
    "MAX_BODY_BYTES",
    "OUTBOX_PATH",
    "REPLY_EVENT",
    "SIGNATURE_HEADER",
    "WEB_PLATFORM",
    "ImportResult",
    "OutboxConfig",
    "OutboxError",
    "OutboxImporter",
    "OutboxReceiver",
    "Receipt",
    "job_id_from_prompt",
    "sources_in",
]
