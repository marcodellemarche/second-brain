"""Notice what changed in the vault and tell Hermes, with a signed webhook and nothing else.

The watcher never writes the vault and never reads a note's content. It keeps a snapshot of every
file's size and mtime, compares it on every poll, and sends `{kind, path}` for what moved. Polling
instead of inotify: the vault is a network-synced folder, where inotify misses what Nextcloud
writes underneath and fires on temporary files that never become notes.

What is never reported:

- the folders Nextcloud, Obsidian and our own trash keep their bookkeeping in (`.git`, `.obsidian`,
  `.trash`), plus the temporary files of a sync in progress (`.~*`, `*.part`);
- a path the brain changed itself, recognised by a commit authored by `brain@localhost` in the last
  two polls, so the brain's own writes do not come back as a user edit;
- a file whose content is not markdown, unless it is a sync conflict.

A conflict copy (`*(conflicted copy*`, `*.sync-conflict-*`) is reported as soon as it appears: it is
never ignored, because it is the one thing a human has to look at. A changed note waits until its
size and mtime have not moved for `quiet_seconds`, because the user may still be typing on the
phone and a half-written note is worse than a late event.

An event Hermes did not accept is kept and tried again on the next poll, as long as it still
describes the vault: a failed delete is dropped once the path is back, a failed conflict once the
copy is gone.
"""

from __future__ import annotations

import fnmatch
import logging
import os
import signal
import stat
import subprocess
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from second_brain.sidecars.webhook import SendEvent, WebhookError

log = logging.getLogger(__name__)

DEFAULT_IGNORE: tuple[str, ...] = (".git", ".obsidian", ".trash")
DEFAULT_QUIET_SECONDS = 20.0
DEFAULT_POLL_SECONDS = 5.0
DEFAULT_BRAIN_EMAIL = "brain@localhost"
CONFLICT_PATTERNS: tuple[str, ...] = ("*(conflicted copy*", "*.sync-conflict-*")
TEMP_PATTERNS: tuple[str, ...] = (".~*", "*.part")
NOTE_SUFFIX = ".md"

# The paths a commit authored by the given email touched since the given time.
RecentCommits = Callable[[float, str], Iterable[str]]


@dataclass(frozen=True, slots=True)
class _Entry:
    size: int
    mtime_ns: int


@dataclass(frozen=True, slots=True)
class _Pending:
    entry: _Entry
    since: float
    kind: str


def _is_conflict(rel: str) -> bool:
    name = rel.rsplit("/", 1)[-1].casefold()
    return any(fnmatch.fnmatchcase(name, pattern) for pattern in CONFLICT_PATTERNS)


def _is_temp(rel: str) -> bool:
    name = rel.rsplit("/", 1)[-1]
    return any(fnmatch.fnmatchcase(name, pattern) for pattern in TEMP_PATTERNS)


def _is_note(rel: str) -> bool:
    return rel.casefold().endswith(NOTE_SUFFIX)


def git_paths(root: Path, email: str, since: float) -> list[str]:
    """The paths touched by the commits authored by `email` since `since`. Read-only."""
    command = [
        "git",
        "-C",
        str(root),
        "log",
        f"--since=@{int(since)}",
        f"--author=<{email}>",
        "--pretty=format:",
        "--name-only",
        "-z",
        "-n",
        "200",
    ]
    try:
        result = subprocess.run(command, capture_output=True, text=True, check=False)
    except OSError as exc:
        log.debug("git is not available, so brain commits cannot be told apart: %s", exc)
        return []
    if result.returncode != 0:
        log.debug("git log failed in %s: %s", root, result.stderr.strip())
        return []
    return [path for path in result.stdout.split("\0") if path]


class VaultWatcher:
    """Poll one vault and send one event per change it is allowed to report."""

    def __init__(
        self,
        root: Path | str,
        ignore: Iterable[str] | None = None,
        quiet_seconds: float = DEFAULT_QUIET_SECONDS,
        send: SendEvent | None = None,
        *,
        brain_email: str = DEFAULT_BRAIN_EMAIL,
        poll_seconds: float = DEFAULT_POLL_SECONDS,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
        recent_commits: RecentCommits | None = None,
    ) -> None:
        if send is None:
            raise ValueError("send is required")
        if quiet_seconds < 0:
            raise ValueError("quiet_seconds must not be negative")
        if poll_seconds <= 0:
            raise ValueError("poll_seconds must be positive")
        resolved = Path(root).resolve()
        if not resolved.is_dir():
            raise ValueError(f"root is not a directory: {root}")
        self._root = resolved
        self._ignore = tuple(DEFAULT_IGNORE if ignore is None else ignore)
        self._quiet_seconds = quiet_seconds
        self._send = send
        self._brain_email = brain_email
        self._poll_seconds = poll_seconds
        self._clock = clock
        self._sleep = sleep
        self._recent_commits = recent_commits
        self._snapshot: dict[str, _Entry] = self._scan()
        self._pending: dict[str, _Pending] = {}
        self._undelivered: dict[str, str] = {}
        self._stopped = False

    @property
    def root(self) -> Path:
        return self._root

    @property
    def quiet_seconds(self) -> float:
        return self._quiet_seconds

    @property
    def pending(self) -> tuple[str, ...]:
        """The paths waiting for their quiet window, oldest first. Read-only, for the tests."""
        ordered = sorted(self._pending.items(), key=lambda item: item[1].since)
        return tuple(path for path, _ in ordered)

    def poll_once(self) -> list[dict[str, Any]]:
        """One pass over the vault. Returns the events Hermes accepted during it."""
        now = self._clock()
        current = self._scan()
        from_brain = set(self._brain_paths(now))
        sent: list[dict[str, Any]] = []

        for path, kind in sorted(self._undelivered.items()):
            if not self._still_true(path, kind, current):
                self._undelivered.pop(path, None)
                continue
            if self._deliver(path, kind):
                self._undelivered.pop(path, None)
                sent.append({"kind": kind, "path": path})

        for path in sorted(current.keys() | self._snapshot.keys()):
            entry = current.get(path)
            previous = self._snapshot.get(path)
            if entry == previous:
                continue
            if path in from_brain:
                self._pending.pop(path, None)
                continue
            if entry is None:
                self._pending.pop(path, None)
                if previous is not None and (_is_note(path) or _is_conflict(path)):
                    self._offer(sent, path, "note_deleted")
                continue
            if _is_conflict(path):
                self._pending.pop(path, None)
                self._offer(sent, path, "conflict")
                continue
            if not _is_note(path):
                continue
            kind = "note_created" if previous is None else "note_changed"
            waiting = self._pending.get(path)
            if waiting is not None and waiting.entry == entry and waiting.kind == kind:
                continue
            self._pending[path] = _Pending(entry, self._stable_since(now, entry), kind)

        for path, waiting in sorted(self._pending.items(), key=lambda item: item[1].since):
            if current.get(path) != waiting.entry or now - waiting.since < self._quiet_seconds:
                continue
            if self._deliver(path, waiting.kind):
                self._pending.pop(path, None)
                sent.append({"kind": waiting.kind, "path": path})

        self._snapshot = current
        return sent

    def run_forever(self, poll_seconds: float | None = None) -> None:
        """Poll until stopped. A failing poll logs and the next one runs: the sidecar stays up."""
        interval = self._poll_seconds if poll_seconds is None else poll_seconds
        if interval <= 0:
            raise ValueError("poll_seconds must be positive")
        log.info("vault watcher polling %s every %.0fs", self._root, interval)
        while not self._stopped:
            try:
                self.poll_once()
            except Exception:
                log.exception("vault watcher poll failed")
            self._sleep(interval)

    def stop(self) -> None:
        self._stopped = True

    def _stable_since(self, now: float, entry: _Entry) -> float:
        """When the note started being stable: its own mtime, or this poll if that is newer.

        A note written before the watcher started, or while it was down, is already past its
        quiet window. An mtime in the future (a clock that disagrees with ours) waits in full.
        """
        return min(now, entry.mtime_ns / 1_000_000_000)

    def _offer(self, sent: list[dict[str, Any]], path: str, kind: str) -> None:
        if self._deliver(path, kind):
            self._undelivered.pop(path, None)
            sent.append({"kind": kind, "path": path})
        else:
            self._undelivered[path] = kind

    @staticmethod
    def _still_true(path: str, kind: str, current: dict[str, _Entry]) -> bool:
        """Whether a delete or a conflict that could not be sent still describes the vault."""
        if kind == "note_deleted":
            return path not in current
        if kind == "conflict":
            return path in current and _is_conflict(path)
        return False

    def _scan(self) -> dict[str, _Entry]:
        """Size and mtime of every regular file worth watching. No content, no symlinks."""
        entries: dict[str, _Entry] = {}
        for dirpath, dirnames, filenames in os.walk(self._root, followlinks=False):
            dirnames[:] = sorted(name for name in dirnames if not self._ignored(name))
            for name in filenames:
                full = Path(dirpath, name)
                rel = full.relative_to(self._root).as_posix()
                if self._ignored(name):
                    continue
                try:
                    info = full.lstat()
                except OSError:
                    continue
                if not stat.S_ISREG(info.st_mode):
                    continue
                entries[rel] = _Entry(info.st_size, info.st_mtime_ns)
        return entries

    def _ignored(self, name: str) -> bool:
        if any(fnmatch.fnmatchcase(name, pattern) for pattern in TEMP_PATTERNS):
            return True
        return any(fnmatch.fnmatchcase(name, pattern) for pattern in self._ignore)

    def _brain_paths(self, now: float) -> list[str]:
        """The paths the brain committed in the last two polls, so its writes are not reported."""
        since = now - 2 * self._poll_seconds
        if self._recent_commits is not None:
            return list(self._recent_commits(since, self._brain_email))
        return git_paths(self._root, self._brain_email, since)

    def _deliver(self, path: str, kind: str) -> bool:
        try:
            status = self._send({"kind": kind, "path": path})
        except (WebhookError, OSError, ValueError) as exc:
            log.warning("event %s for %s not sent: %s", kind, path, exc)
            return False
        if not 200 <= status < 300:
            log.warning("event %s for %s refused with %d", kind, path, status)
            return False
        log.info("event %s for %s accepted", kind, path)
        return True


def main() -> None:  # pragma: no cover - process entry point
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    from second_brain.sidecars.config import SidecarsConfig

    config = SidecarsConfig.load()
    section = config.require("vault_watcher")
    watcher = VaultWatcher(
        config.vault_dir,
        ignore=section.ignore,
        quiet_seconds=section.quiet_seconds,
        send=config.sender(section.route),
        brain_email=section.brain_email,
        poll_seconds=section.poll_seconds,
    )
    log.info("vault watcher on %s, route %s", watcher.root, section.route)

    def _stop(*_args: object) -> None:
        watcher.stop()

    signal.signal(signal.SIGTERM, _stop)
    watcher.run_forever()


if __name__ == "__main__":
    main()


__all__ = [
    "CONFLICT_PATTERNS",
    "DEFAULT_BRAIN_EMAIL",
    "DEFAULT_IGNORE",
    "DEFAULT_POLL_SECONDS",
    "DEFAULT_QUIET_SECONDS",
    "RecentCommits",
    "TEMP_PATTERNS",
    "VaultWatcher",
    "git_paths",
]
