"""Run the queued background jobs as separate Hermes runs, through signed webhook calls.

Each job is a POST to a Hermes webhook route whose toolset is restricted by hand in the Hermes
config, so a background run can do the work and nothing else. The sidecar never talks to the model
and never writes the vault: it takes a job from the queue, signs the body, reports that the run was
accepted, and later notices the result note appearing.

Hermes v0.22.2 validates the replay-safe signature as `X-Webhook-Signature-V2`, the hex HMAC-SHA256
of `"<timestamp>.<body>"`, with the same timestamp in `X-Webhook-Timestamp` and at most five
minutes of skew (`gateway/platforms/webhook.py:800-808`). The body-only `X-Hub-Signature-256` is
the legacy path: it has no timestamp, so a captured request replays forever.

A 2xx answer means Hermes accepted the run, not that the work is done, so the job becomes
`dispatched`. It becomes `finished` when the result note exists in the vault, and `failed` when the
note has not appeared after `dispatch_timeout`.

One job at a time by default: the model is shared with the conversation, and a queue of research
runs must not starve it. The POST function is injectable, so the tests drive the whole loop without
a network or a running Hermes.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from pathlib import Path

from second_brain.tools.jobs import Job, JobKind, JobStore

log = logging.getLogger(__name__)

SIGNATURE_HEADER = "X-Webhook-Signature-V2"
TIMESTAMP_HEADER = "X-Webhook-Timestamp"
DEFAULT_POLL_SECONDS = 5.0
DEFAULT_TIMEOUT = 30.0
DEFAULT_DISPATCH_TIMEOUT = 60 * 60.0
DEFAULT_SWEEP_SECONDS = 60.0
_DISPATCHED_SCAN = 500

# What one POST attempt answers: the HTTP status and the response body, for the error message.
HttpPost = Callable[[str, bytes, Mapping[str, str], float], tuple[int, bytes]]


class JobRunnerError(RuntimeError):
    """The runner cannot start: a missing secret file, for example."""


def sign_body(secret: bytes, body: bytes, timestamp: str) -> str:
    """The header value Hermes expects: the hex HMAC-SHA256 of `"<timestamp>.<body>"`."""
    signed = timestamp.encode("ascii") + b"." + body
    return hmac.new(secret, signed, hashlib.sha256).hexdigest()


def _http_post(url: str, body: bytes, headers: Mapping[str, str], timeout: float) -> tuple[int, bytes]:
    request = urllib.request.Request(url, data=body, headers=dict(headers), method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()
    except (OSError, ValueError) as exc:
        raise JobRunnerError(f"{url} unreachable: {exc}") from exc


def job_body(job: Job) -> bytes:
    """The JSON body of one job. Only the payload fields, nothing from the vault."""
    payload = {
        "job_id": job.id,
        "kind": job.kind,
        "title": job.payload.title,
        "question": job.payload.question,
        "note_path": job.payload.note_path,
    }
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


class JobRunner:
    """Claim one job at a time from the store, send it, then watch for its result note."""

    def __init__(
        self,
        store: JobStore,
        kinds: Mapping[str, JobKind],
        webhook_base_url: str,
        hmac_secret_file: Path | str,
        *,
        vault_root: Path | str,
        max_concurrent: int = 1,
        post: HttpPost | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        dispatch_timeout: float = DEFAULT_DISPATCH_TIMEOUT,
        sweep_seconds: float = DEFAULT_SWEEP_SECONDS,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if max_concurrent < 1:
            raise ValueError("max_concurrent must be at least 1")
        if dispatch_timeout <= 0:
            raise ValueError("dispatch_timeout must be positive")
        if sweep_seconds <= 0:
            raise ValueError("sweep_seconds must be positive")
        base = webhook_base_url.strip().rstrip("/")
        if not base:
            raise ValueError("webhook_base_url must not be empty")
        root = Path(vault_root).resolve()
        if not root.is_dir():
            raise ValueError(f"vault_root is not a directory: {vault_root}")
        self._store = store
        self._kinds = dict(kinds)
        self._base = base
        self._secret_file = Path(hmac_secret_file)
        self._vault_root = root
        self._max_concurrent = max_concurrent
        self._post = post or _http_post
        self._timeout = timeout
        self._dispatch_timeout = dispatch_timeout
        self._sweep_seconds = sweep_seconds
        self._clock = clock
        self._sleep = sleep
        self._last_sweep: float | None = None
        self._stopped = False

    @property
    def max_concurrent(self) -> int:
        return self._max_concurrent

    def url(self, kind: str) -> str:
        """The webhook URL of a kind. An unknown kind is a bug, not a job to run."""
        try:
            route = self._kinds[kind].route
        except KeyError:
            raise JobRunnerError(f"no job kind {kind!r} configured") from None
        return f"{self._base}/{route}"

    def _secret(self) -> bytes:
        """The HMAC secret, read at every send so a rotation does not need a restart."""
        try:
            secret = self._secret_file.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise JobRunnerError(f"cannot read the HMAC secret: {exc}") from exc
        if not secret:
            raise JobRunnerError(f"the HMAC secret in {self._secret_file} is empty")
        return secret.encode("utf-8")

    def _headers(self, body: bytes, timestamp: str) -> dict[str, str]:
        return {
            SIGNATURE_HEADER: sign_body(self._secret(), body, timestamp),
            TIMESTAMP_HEADER: timestamp,
            "Content-Type": "application/json",
            "User-Agent": "second-brain-job-runner",
        }

    def run_job(self, job: Job) -> bool:
        """Send one claimed job and report the outcome. True when Hermes accepted the run."""
        body = job_body(job)
        timestamp = str(int(self._clock()))
        try:
            headers = self._headers(body, timestamp)
            status, response = self._post(self.url(job.kind), body, headers, self._timeout)
        except (JobRunnerError, OSError, ValueError) as exc:
            self._store.fail(job.id, str(exc))
            log.warning("job %s (%s) not sent: %s", job.id, job.kind, exc)
            return False
        if 200 <= status < 300:
            self._store.dispatch(job.id)
            log.info("job %s (%s) dispatched with %d", job.id, job.kind, status)
            return True
        detail = response[:200].decode("utf-8", "replace")
        self._store.fail(job.id, f"webhook answered {status}: {detail}")
        log.warning("job %s (%s) refused with %d: %s", job.id, job.kind, status, detail)
        return False

    def run_once(self) -> bool:
        """Claim and run one job. False when the queue was empty."""
        job = self._store.claim()
        if job is None:
            return False
        self.run_job(job)
        return True

    def sweep(self) -> None:
        """Reclaim the jobs whose runner died and settle the ones that were dispatched."""
        reclaimed = self._store.reclaim_stale()
        if reclaimed:
            log.warning("reclaimed %d stale job(s): %s", len(reclaimed), ", ".join(reclaimed))
        self._settle_dispatched()
        self._last_sweep = self._clock()

    def _settle_dispatched(self) -> None:
        """Finish the dispatched jobs whose result note exists, fail the ones that timed out."""
        now = self._clock()
        for job in self._store.list("dispatched", limit=_DISPATCHED_SCAN):
            # Without a note_path the run picks the note itself, so there is nothing to look for
            # and the job simply expires.
            note_path = job.payload.note_path
            if note_path is not None and self._note_exists(note_path):
                self._store.mark_finished(job.id)
                log.info("job %s (%s) finished: %s exists", job.id, job.kind, note_path)
                continue
            dispatched = job.dispatched if job.dispatched is not None else job.created
            if now - dispatched > self._dispatch_timeout:
                self._store.fail(
                    job.id,
                    f"no result note after {int(self._dispatch_timeout)}s",
                    retry=False,
                )
                log.warning("job %s (%s) never produced its result note", job.id, job.kind)

    def _note_exists(self, note_path: str) -> bool:
        """True when the note is a file inside the vault. Read-only, and never outside the root."""
        candidate = (self._vault_root / note_path).resolve()
        if not candidate.is_relative_to(self._vault_root):
            return False
        return candidate.is_file()

    def stop(self) -> None:
        self._stopped = True

    def run_forever(self, poll_seconds: float = DEFAULT_POLL_SECONDS) -> None:
        """Drain the queue, one job at a time, sleeping `poll_seconds` whenever it is empty."""
        if poll_seconds <= 0:
            raise ValueError("poll_seconds must be positive")
        log.info("job runner watching %s for %d kinds", self._base, len(self._kinds))
        while not self._stopped:
            try:
                if self._due_for_sweep():
                    self.sweep()
                ran = self.run_once()
            except Exception:
                # A failing job must not take the sidecar down: the next tick claims the next one.
                log.exception("job runner loop failed")
                ran = False
            if not ran:
                self._sleep(poll_seconds)

    def _due_for_sweep(self) -> bool:
        if self._last_sweep is None:
            return True
        return self._clock() - self._last_sweep >= self._sweep_seconds
