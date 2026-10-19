"""Run the queued background jobs as separate Hermes runs, through signed webhook calls.

Each job is a POST to a Hermes webhook route whose toolset is restricted by hand in the Hermes
config, so a background run can do the work and nothing else. The sidecar never talks to the model
and never writes the vault itself: it takes a job from the queue, signs the body, reports that the
run was accepted, and later collects the result.

Hermes v0.22.2 validates the replay-safe signature as `X-Webhook-Signature-V2`, the hex HMAC-SHA256
of `"<timestamp>.<body>"`, with the same timestamp in `X-Webhook-Timestamp` and at most five
minutes of skew (`gateway/platforms/webhook.py:800-808`). The signing itself lives in
`sidecars/webhook.py`, shared with the other sidecars, so the two cannot drift apart.

A 2xx answer means Hermes accepted the run, not that the work is done, so the job becomes
`dispatched`. It becomes `finished` when the result note exists in the vault, and `failed` when the
note has not appeared after `dispatch_timeout`.

A research route cannot write the vault, so its reply comes back out of band: Hermes POSTs it to
the outbox endpoint of brain_tools, and `OutboxImporter` turns it into an untrusted raw note. That
is why every sweep imports the outbox first: an imported result finishes its research job and
queues the follow-up vault job, which then writes the note the job asked for. Without an importer
the sidecar only watches for notes, which is what a brain with no research kind does.

One job at a time by default: the model is shared with the conversation, and a queue of research
runs must not starve it. The POST function is injectable, so the tests drive the whole loop without
a network or a running Hermes.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable, Mapping
from pathlib import Path

from second_brain.sidecars.webhook import (
    DEFAULT_TIMEOUT,
    SIGNATURE_HEADER,
    TIMESTAMP_HEADER,
    HttpPost,
    WebhookError,
    http_post,
    read_secret,
    sign_body,
    signed_headers,
)
from second_brain.tools.jobs import Job, JobKind, JobStore
from second_brain.tools.outbox import OutboxImporter

log = logging.getLogger(__name__)

DEFAULT_POLL_SECONDS = 5.0
DEFAULT_DISPATCH_TIMEOUT = 60 * 60.0
DEFAULT_SWEEP_SECONDS = 60.0
_DISPATCHED_SCAN = 500


class JobRunnerError(RuntimeError):
    """The runner cannot start: a missing secret file, for example."""


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
        hmac_secret_file: Path | str | Mapping[str, Path | str],
        *,
        vault_root: Path | str,
        max_concurrent: int = 1,
        post: HttpPost | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        dispatch_timeout: float = DEFAULT_DISPATCH_TIMEOUT,
        sweep_seconds: float = DEFAULT_SWEEP_SECONDS,
        importer: OutboxImporter | None = None,
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
        # Hermes checks each route against its own secret, so a mapping names one file per route.
        if isinstance(hmac_secret_file, Mapping):
            self._secret_files = {route: Path(path) for route, path in hmac_secret_file.items()}
            self._secret_file: Path | None = None
        else:
            self._secret_files = {}
            self._secret_file = Path(hmac_secret_file)
        self._vault_root = root
        self._max_concurrent = max_concurrent
        self._post = post or http_post
        self._timeout = timeout
        self._dispatch_timeout = dispatch_timeout
        self._sweep_seconds = sweep_seconds
        self._importer = importer
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

    def _secret(self, kind: str) -> bytes:
        route = self._kinds[kind].route if kind in self._kinds else ""
        path = self._secret_files.get(route, self._secret_file)
        if path is None:
            raise JobRunnerError(f"no HMAC secret configured for route {route!r}")
        try:
            return read_secret(path)
        except WebhookError as exc:
            raise JobRunnerError(str(exc)) from exc

    def _headers(self, kind: str, body: bytes, timestamp: str) -> dict[str, str]:
        return signed_headers(self._secret(kind), body, timestamp, user_agent="second-brain-job-runner")

    def run_job(self, job: Job) -> bool:
        """Send one claimed job and report the outcome. True when Hermes accepted the run."""
        body = job_body(job)
        timestamp = str(int(self._clock()))
        try:
            url = self.url(job.kind)
            headers = self._headers(job.kind, body, timestamp)
            status, response = self._post(url, body, headers, self._timeout)
        except (JobRunnerError, WebhookError, OSError, ValueError) as exc:
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
        """Reclaim the jobs whose runner died, import the research results, settle the rest."""
        reclaimed = self._store.reclaim_stale()
        if reclaimed:
            log.warning("reclaimed %d stale job(s): %s", len(reclaimed), ", ".join(reclaimed))
        self._import_outbox()
        self._settle_dispatched()
        self._last_sweep = self._clock()

    def _import_outbox(self) -> None:
        """Bring in the replies Hermes posted while we were not looking. A bad file is not fatal."""
        if self._importer is None:
            return
        try:
            imported = self._importer.sweep()
        except Exception:
            log.exception("could not sweep the outbox")
            return
        for result in imported:
            if result.ok:
                log.info("imported %s as %s", result.source, result.note)
            else:
                log.warning("outbox file %s failed: %s", result.source, result.error)

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


def main() -> None:  # pragma: no cover - process entry point
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    from second_brain.sidecars.config import SidecarsConfig

    config = SidecarsConfig.load()
    section = config.require("job_runner")
    routes = section.routes()
    runner = JobRunner(
        section.jobs.store(),
        section.jobs.kinds,
        config.webhook_base_url,
        {route: config.route_secrets[route] for route in routes},
        vault_root=config.vault_dir,
        max_concurrent=section.max_concurrent,
        dispatch_timeout=section.dispatch_timeout,
        importer=None,
    )
    log.info("job runner on %s, %d kind(s)", section.jobs.store_path, len(routes))
    runner.run_forever()


if __name__ == "__main__":
    main()


__all__ = [
    "DEFAULT_DISPATCH_TIMEOUT",
    "DEFAULT_POLL_SECONDS",
    "DEFAULT_TIMEOUT",
    "HttpPost",
    "JobRunner",
    "JobRunnerError",
    "OutboxImporter",
    "SIGNATURE_HEADER",
    "TIMESTAMP_HEADER",
    "job_body",
    "sign_body",
]
