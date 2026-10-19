"""Run the queued background jobs as separate Hermes runs, through signed webhook calls.

Each job is a POST to a Hermes webhook route whose toolset is restricted by hand in the Hermes
config, so a background run can do the work and nothing else. The sidecar never talks to the model
or the vault: it takes a job from the queue, signs the body and reports the outcome back.

Hermes v0.22.2 validates the signature as a GitHub-style one: the header `X-Hub-Signature-256`
holding `sha256=` plus the hex HMAC-SHA256 of the raw body, keyed with the route secret
(`gateway/platforms/webhook.py:789`). A run answers 202 when it accepts the event, so anything in
the 2xx range marks the job finished.

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

SIGNATURE_HEADER = "X-Hub-Signature-256"
DEFAULT_POLL_SECONDS = 5.0
DEFAULT_TIMEOUT = 30.0

# What one POST attempt answers: the HTTP status and the response body, for the error message.
HttpPost = Callable[[str, bytes, Mapping[str, str], float], tuple[int, bytes]]


class JobRunnerError(RuntimeError):
    """The runner cannot start: a missing secret file, for example."""


def sign_body(secret: bytes, body: bytes) -> str:
    """The header value Hermes expects: `sha256=` and the hex HMAC-SHA256 of the raw body."""
    return "sha256=" + hmac.new(secret, body, hashlib.sha256).hexdigest()


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
    """Claim one job at a time from the store and run it through its webhook route."""

    def __init__(
        self,
        store: JobStore,
        kinds: Mapping[str, JobKind],
        webhook_base_url: str,
        hmac_secret_file: Path | str,
        max_concurrent: int = 1,
        *,
        post: HttpPost | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if max_concurrent < 1:
            raise ValueError("max_concurrent must be at least 1")
        base = webhook_base_url.strip().rstrip("/")
        if not base:
            raise ValueError("webhook_base_url must not be empty")
        self._store = store
        self._kinds = dict(kinds)
        self._base = base
        self._secret_file = Path(hmac_secret_file)
        self._max_concurrent = max_concurrent
        self._post = post or _http_post
        self._timeout = timeout
        self._sleep = sleep
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

    def run_job(self, job: Job) -> bool:
        """Send one claimed job and report the outcome to the store. True when it was accepted."""
        body = job_body(job)
        try:
            headers = {
                SIGNATURE_HEADER: sign_body(self._secret(), body),
                "Content-Type": "application/json",
                "User-Agent": "second-brain-job-runner",
            }
            status, response = self._post(self.url(job.kind), body, headers, self._timeout)
        except (JobRunnerError, OSError, ValueError) as exc:
            self._store.fail(job.id, str(exc))
            log.warning("job %s (%s) not sent: %s", job.id, job.kind, exc)
            return False
        if 200 <= status < 300:
            self._store.finish(job.id)
            log.info("job %s (%s) accepted with %d", job.id, job.kind, status)
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

    def stop(self) -> None:
        self._stopped = True

    def run_forever(self, poll_seconds: float = DEFAULT_POLL_SECONDS) -> None:
        """Drain the queue, one job at a time, sleeping `poll_seconds` whenever it is empty."""
        if poll_seconds <= 0:
            raise ValueError("poll_seconds must be positive")
        log.info("job runner watching %s for %d kinds", self._base, len(self._kinds))
        while not self._stopped:
            try:
                ran = self.run_once()
            except Exception:
                # A failing job must not take the sidecar down: the next tick claims the next one.
                log.exception("job runner loop failed")
                ran = False
            if not ran:
                self._sleep(poll_seconds)
