"""Tests for the vault watcher: polling, the quiet window, brain commits and conflict copies."""

from __future__ import annotations

import subprocess
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from second_brain.sidecars.vault_watcher import (
    DEFAULT_QUIET_SECONDS,
    VaultWatcher,
    git_paths,
)

QUIET = 20.0


class FakeSend:
    def __init__(self, status: int = 202) -> None:
        self.status = status
        self.calls: list[dict[str, Any]] = []

    def __call__(self, payload: Mapping[str, Any]) -> int:
        self.calls.append(dict(payload))
        return self.status

    @property
    def kinds(self) -> list[str]:
        return [call["kind"] for call in self.calls]


class ManualClock:
    def __init__(self, now: float = 1_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def root(tmp_path: Path) -> Path:
    vault = tmp_path / "vault"
    (vault / "topics").mkdir(parents=True)
    return vault


@pytest.fixture
def send() -> FakeSend:
    return FakeSend()


@pytest.fixture
def clock() -> ManualClock:
    return ManualClock()


@pytest.fixture
def watcher(root: Path, send: FakeSend, clock: ManualClock) -> VaultWatcher:
    return VaultWatcher(root, quiet_seconds=QUIET, send=send, clock=clock, poll_seconds=5.0)


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, check=True)
    return result.stdout.strip()


def _commit(root: Path, path: str, text: str, *, name: str, email: str) -> None:
    _write(root / path, text)
    _git(root, "add", "--", path)
    _git(
        root,
        "-c",
        f"user.name={name}",
        "-c",
        f"user.email={email}",
        "-c",
        "commit.gpgsign=false",
        "commit",
        "-q",
        "-m",
        f"edit {path}",
    )


def _git_repo(root: Path) -> Path:
    _git(root, "init", "-q")
    _git(root, "config", "user.name", "tester")
    _git(root, "config", "user.email", "tester@localhost")
    _git(root, "commit", "-q", "--allow-empty", "-m", "init")
    return root


# --- create, change, delete -----------------------------------------------------------------


def test_the_initial_snapshot_is_not_reported(root: Path, send: FakeSend, clock: ManualClock) -> None:
    _write(root / "topics/before.md", "---\ntype: topic\n---\n")
    watcher = VaultWatcher(root, quiet_seconds=0, send=send, clock=clock)
    assert watcher.poll_once() == []
    assert send.calls == []


def test_a_new_note_is_reported_as_created(
    root: Path, watcher: VaultWatcher, send: FakeSend, clock: ManualClock
) -> None:
    watcher.poll_once()
    _write(root / "topics/note.md", "---\ntype: topic\n---\n")
    assert watcher.poll_once() == []
    clock.advance(QUIET)
    assert watcher.poll_once() == [{"kind": "note_created", "path": "topics/note.md"}]
    assert send.calls == [{"kind": "note_created", "path": "topics/note.md"}]


def test_a_changed_note_is_reported_as_changed(
    root: Path, watcher: VaultWatcher, send: FakeSend, clock: ManualClock
) -> None:
    note = root / "topics/note.md"
    _write(note, "one\n")
    watcher.poll_once()
    _write(note, "one and two\n")
    watcher.poll_once()
    clock.advance(QUIET)
    assert watcher.poll_once() == [{"kind": "note_changed", "path": "topics/note.md"}]


def test_a_deleted_note_is_reported(
    root: Path, watcher: VaultWatcher, send: FakeSend, clock: ManualClock
) -> None:
    note = root / "topics/note.md"
    _write(note, "one\n")
    watcher.poll_once()
    note.unlink()
    assert watcher.poll_once() == [{"kind": "note_deleted", "path": "topics/note.md"}]


def test_a_deleted_note_is_reported_once(
    root: Path, watcher: VaultWatcher, send: FakeSend, clock: ManualClock
) -> None:
    note = root / "topics/note.md"
    _write(note, "one\n")
    watcher.poll_once()
    note.unlink()
    watcher.poll_once()
    assert watcher.poll_once() == []
    assert send.kinds == ["note_deleted"]


def test_a_note_that_comes_back_is_created_again(
    root: Path, watcher: VaultWatcher, send: FakeSend, clock: ManualClock
) -> None:
    note = root / "topics/note.md"
    _write(note, "one\n")
    watcher.poll_once()
    note.unlink()
    watcher.poll_once()
    _write(note, "two\n")
    watcher.poll_once()
    clock.advance(QUIET)
    assert watcher.poll_once() == [{"kind": "note_created", "path": "topics/note.md"}]


def test_deleting_a_folder_reports_the_notes_in_it(
    root: Path, watcher: VaultWatcher, send: FakeSend, clock: ManualClock
) -> None:
    import shutil

    _write(root / "topics/one.md", "x\n")
    _write(root / "topics/two.md", "x\n")
    watcher.poll_once()
    shutil.rmtree(root / "topics")
    sent = watcher.poll_once()
    assert {"kind": "note_deleted", "path": "topics/one.md"} in sent
    assert {"kind": "note_deleted", "path": "topics/two.md"} in sent


def test_deleting_a_non_markdown_file_is_not_reported(
    root: Path, watcher: VaultWatcher, send: FakeSend, clock: ManualClock
) -> None:
    asset = root / "topics/photo.png"
    _write(asset, "x\n")
    watcher.poll_once()
    asset.unlink()
    assert watcher.poll_once() == []


def test_a_rename_is_a_delete_and_a_create(
    root: Path, watcher: VaultWatcher, send: FakeSend, clock: ManualClock
) -> None:
    note = root / "topics/old.md"
    _write(note, "one\n")
    watcher.poll_once()
    note.rename(root / "topics/new.md")
    sent = watcher.poll_once()
    clock.advance(QUIET)
    sent += watcher.poll_once()
    assert {"kind": "note_deleted", "path": "topics/old.md"} in sent
    assert {"kind": "note_created", "path": "topics/new.md"} in sent


# --- the quiet window -----------------------------------------------------------------------


def test_a_note_is_not_reported_before_the_quiet_window(
    root: Path, watcher: VaultWatcher, send: FakeSend, clock: ManualClock
) -> None:
    watcher.poll_once()
    _write(root / "topics/note.md", "half a sentence")
    assert watcher.poll_once() == []
    clock.advance(QUIET - 1)
    assert watcher.poll_once() == []
    assert watcher.pending == ("topics/note.md",)
    assert send.calls == []


def test_typing_restarts_the_quiet_window(
    root: Path, watcher: VaultWatcher, send: FakeSend, clock: ManualClock
) -> None:
    watcher.poll_once()
    note = root / "topics/note.md"
    _write(note, "first")
    clock.advance(1)
    watcher.poll_once()
    clock.advance(5)
    _write(note, "first and more")
    watcher.poll_once()
    clock.advance(QUIET - 5)
    assert watcher.poll_once() == []
    clock.advance(6)
    assert watcher.poll_once() == [{"kind": "note_changed", "path": "topics/note.md"}]


def test_the_default_quiet_window_is_twenty_seconds() -> None:
    assert DEFAULT_QUIET_SECONDS == 20.0


def test_a_zero_quiet_window_reports_on_the_same_poll(root: Path, send: FakeSend, clock: ManualClock) -> None:
    watcher = VaultWatcher(root, quiet_seconds=0, send=send, clock=clock)
    watcher.poll_once()
    _write(root / "topics/note.md", "now\n")
    assert watcher.poll_once() == [{"kind": "note_created", "path": "topics/note.md"}]


# --- the payload ----------------------------------------------------------------------------


def test_the_payload_carries_only_the_kind_and_the_path(
    root: Path, watcher: VaultWatcher, send: FakeSend, clock: ManualClock
) -> None:
    watcher.poll_once()
    _write(root / "topics/note.md", "---\ntype: topic\n---\nsecret body text\n")
    watcher.poll_once()
    clock.advance(QUIET)
    watcher.poll_once()
    payload = send.calls[-1]
    assert sorted(payload) == ["kind", "path"]
    assert "secret body text" not in str(payload)


def test_the_path_is_relative_to_the_vault(
    root: Path, watcher: VaultWatcher, send: FakeSend, clock: ManualClock
) -> None:
    watcher.poll_once()
    _write(root / "topics/deep/note.md", "x\n")
    watcher.poll_once()
    clock.advance(QUIET)
    watcher.poll_once()
    assert send.calls[-1]["path"] == "topics/deep/note.md"


# --- what is ignored ------------------------------------------------------------------------


def test_non_markdown_files_are_not_reported(
    root: Path, watcher: VaultWatcher, send: FakeSend, clock: ManualClock
) -> None:
    watcher.poll_once()
    _write(root / "topics/photo.png", "not really a png\n")
    _write(root / "topics/data.json", "{}\n")
    clock.advance(QUIET)
    assert watcher.poll_once() == []


@pytest.mark.parametrize("folder", [".git", ".obsidian", ".trash"])
def test_the_bookkeeping_folders_are_not_reported(
    root: Path, watcher: VaultWatcher, send: FakeSend, clock: ManualClock, folder: str
) -> None:
    watcher.poll_once()
    _write(root / folder / "note.md", "internal\n")
    clock.advance(QUIET)
    assert watcher.poll_once() == []


def test_a_bookkeeping_folder_is_skipped_at_any_depth(
    root: Path, watcher: VaultWatcher, send: FakeSend, clock: ManualClock
) -> None:
    watcher.poll_once()
    _write(root / "topics/.obsidian/note.md", "internal\n")
    clock.advance(QUIET)
    assert watcher.poll_once() == []


@pytest.mark.parametrize("name", [".~lock.note.md#", ".~note.md", "note.md.part"])
def test_the_temp_files_of_a_sync_are_ignored(
    root: Path, watcher: VaultWatcher, send: FakeSend, clock: ManualClock, name: str
) -> None:
    watcher.poll_once()
    _write(root / "topics" / name, "partial\n")
    clock.advance(QUIET)
    assert watcher.poll_once() == []
    assert send.calls == []


def test_a_temp_file_that_becomes_a_note_is_reported(
    root: Path, watcher: VaultWatcher, send: FakeSend, clock: ManualClock
) -> None:
    watcher.poll_once()
    _write(root / "topics/note.md.part", "partial\n")
    clock.advance(QUIET)
    assert watcher.poll_once() == []
    _write(root / "topics/note.md", "complete\n")
    watcher.poll_once()
    clock.advance(QUIET)
    assert watcher.poll_once() == [{"kind": "note_created", "path": "topics/note.md"}]


def test_ignore_can_be_extended(root: Path, send: FakeSend, clock: ManualClock) -> None:
    watcher = VaultWatcher(
        root, ignore=[".git", ".obsidian", ".trash", "templates"], quiet_seconds=0, send=send, clock=clock
    )
    watcher.poll_once()
    _write(root / "templates/topic.md", "template\n")
    _write(root / "topics/note.md", "note\n")
    watcher.poll_once()
    assert send.calls == [{"kind": "note_created", "path": "topics/note.md"}]


def test_a_symlink_is_not_followed(
    root: Path, watcher: VaultWatcher, send: FakeSend, clock: ManualClock, tmp_path: Path
) -> None:
    outside = tmp_path / "outside.md"
    _write(outside, "outside\n")
    (root / "topics/link.md").symlink_to(outside)
    clock.advance(QUIET)
    assert watcher.poll_once() == []


# --- conflict copies ------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        "note (conflicted copy 2024-01-01).md",
        "note.md.sync-conflict-20240101-120000-ABCDEFG.md",
        "photo.png.sync-conflict-20240101-120000-ABCDEFG.png",
    ],
)
def test_a_conflict_copy_is_reported_as_conflict(
    root: Path, watcher: VaultWatcher, send: FakeSend, clock: ManualClock, name: str
) -> None:
    watcher.poll_once()
    _write(root / "topics" / name, "two versions\n")
    clock.advance(QUIET)
    assert watcher.poll_once() == [{"kind": "conflict", "path": f"topics/{name}"}]


def test_a_conflict_copy_is_reported_without_waiting(root: Path, send: FakeSend) -> None:
    """A conflict does not go through the quiet window: a fresh copy is reported at once."""
    watcher = VaultWatcher(root, quiet_seconds=3600, send=send, clock=time.time)
    watcher.poll_once()
    _write(root / "topics/note (conflicted copy 2024-01-01).md", "two versions\n")
    assert watcher.poll_once() == [
        {"kind": "conflict", "path": "topics/note (conflicted copy 2024-01-01).md"}
    ]


def test_a_conflict_copy_is_reported_once(
    root: Path, watcher: VaultWatcher, send: FakeSend, clock: ManualClock
) -> None:
    watcher.poll_once()
    _write(root / "topics/note (conflicted copy 2024-01-01).md", "two versions\n")
    watcher.poll_once()
    clock.advance(QUIET)
    assert watcher.poll_once() == []
    assert send.kinds == ["conflict"]


def test_a_conflict_copy_that_is_resolved_is_a_delete(
    root: Path, watcher: VaultWatcher, send: FakeSend, clock: ManualClock
) -> None:
    conflict = root / "topics/note (conflicted copy 2024-01-01).md"
    _write(conflict, "two versions\n")
    watcher.poll_once()
    conflict.unlink()
    assert watcher.poll_once() == [
        {"kind": "note_deleted", "path": "topics/note (conflicted copy 2024-01-01).md"}
    ]


# --- the brain's own writes -----------------------------------------------------------------


def test_a_brain_commit_is_skipped(root: Path, send: FakeSend) -> None:
    """The vault is a git repo and the brain just wrote a note: that is not a user edit."""
    _git_repo(root)
    watcher = VaultWatcher(root, quiet_seconds=0, send=send, clock=time.time, poll_seconds=30.0)
    watcher.poll_once()
    _commit(root, "topics/written-by-brain.md", "x\n", name="brain", email="brain@localhost")
    assert watcher.poll_once() == []
    assert send.calls == []


def test_a_commit_by_another_author_is_reported(root: Path, send: FakeSend) -> None:
    _git_repo(root)
    watcher = VaultWatcher(root, quiet_seconds=0, send=send, clock=time.time, poll_seconds=30.0)
    watcher.poll_once()
    _commit(root, "topics/written-by-human.md", "x\n", name="human", email="human@localhost")
    assert watcher.poll_once() == [{"kind": "note_created", "path": "topics/written-by-human.md"}]


def test_only_the_brain_paths_are_skipped(root: Path, send: FakeSend) -> None:
    _git_repo(root)
    watcher = VaultWatcher(root, quiet_seconds=0, send=send, clock=time.time, poll_seconds=30.0)
    watcher.poll_once()
    _commit(root, "topics/by-brain.md", "x\n", name="brain", email="brain@localhost")
    _commit(root, "topics/by-human.md", "x\n", name="human", email="human@localhost")
    assert watcher.poll_once() == [{"kind": "note_created", "path": "topics/by-human.md"}]


def test_a_brain_commit_drops_a_pending_change(root: Path, send: FakeSend) -> None:
    _git_repo(root)
    clock = ManualClock(time.time())
    watcher = VaultWatcher(root, quiet_seconds=QUIET, send=send, clock=clock, poll_seconds=30.0)
    watcher.poll_once()
    _write(root / "topics/note.md", "a user edit\n")
    clock.advance(1)
    watcher.poll_once()
    assert watcher.pending == ("topics/note.md",)
    _commit(root, "topics/note.md", "the brain rewrote it\n", name="brain", email="brain@localhost")
    clock.advance(1)
    watcher.poll_once()
    assert watcher.pending == ()
    clock.advance(QUIET * 2)
    assert watcher.poll_once() == []


def test_the_brain_window_is_the_last_two_polls(root: Path, send: FakeSend) -> None:
    seen: list[tuple[float, str]] = []

    def recent_commits(since: float, email: str) -> list[str]:
        seen.append((since, email))
        return []

    clock = ManualClock()
    watcher = VaultWatcher(
        root,
        quiet_seconds=0,
        send=send,
        clock=clock,
        poll_seconds=5.0,
        recent_commits=recent_commits,
    )
    watcher.poll_once()
    assert seen == [(clock.now - 10.0, "brain@localhost")]


def test_the_brain_email_is_configurable(root: Path, send: FakeSend) -> None:
    seen: list[str] = []

    def recent_commits(since: float, email: str) -> list[str]:
        seen.append(email)
        return []

    watcher = VaultWatcher(
        root,
        quiet_seconds=0,
        send=send,
        brain_email="assistant@example.invalid",
        recent_commits=recent_commits,
    )
    watcher.poll_once()
    assert seen == ["assistant@example.invalid"]


def test_git_paths_returns_nothing_outside_a_repository(root: Path) -> None:
    assert git_paths(root, "brain@localhost", time.time() - 60) == []


def test_git_paths_lists_the_paths_of_a_commit(root: Path) -> None:
    _git_repo(root)
    _commit(root, "topics/a.md", "a\n", name="brain", email="brain@localhost")
    _commit(root, "topics/b.md", "b\n", name="human", email="human@localhost")
    paths = git_paths(root, "brain@localhost", time.time() - 60)
    assert "topics/a.md" in paths
    assert "topics/b.md" not in paths


# --- delivery -------------------------------------------------------------------------------


def test_a_refused_event_is_tried_again_on_the_next_poll(
    root: Path, send: FakeSend, clock: ManualClock
) -> None:
    send.status = 503
    watcher = VaultWatcher(root, quiet_seconds=0, send=send, clock=clock)
    watcher.poll_once()
    _write(root / "topics/note.md", "x\n")
    assert watcher.poll_once() == []
    send.status = 202
    assert watcher.poll_once() == [{"kind": "note_created", "path": "topics/note.md"}]
    assert len(send.calls) == 2


def test_a_refused_change_waits_for_the_quiet_window_again(
    root: Path, send: FakeSend, clock: ManualClock
) -> None:
    send.status = 500
    watcher = VaultWatcher(root, quiet_seconds=QUIET, send=send, clock=clock)
    watcher.poll_once()
    _write(root / "topics/note.md", "x\n")
    clock.advance(QUIET)
    assert watcher.poll_once() == []
    send.status = 202
    clock.advance(QUIET)
    assert watcher.poll_once() == [{"kind": "note_created", "path": "topics/note.md"}]


def test_a_failed_delete_is_dropped_once_the_note_is_back(
    root: Path, send: FakeSend, clock: ManualClock
) -> None:
    send.status = 500
    note = root / "topics/note.md"
    _write(note, "x\n")
    watcher = VaultWatcher(root, quiet_seconds=0, send=send, clock=clock)
    watcher.poll_once()
    note.unlink()
    watcher.poll_once()
    _write(note, "back\n")
    watcher.poll_once()
    send.status = 202
    clock.advance(QUIET)
    sent = watcher.poll_once()
    assert {"kind": "note_deleted", "path": "topics/note.md"} not in sent


def test_a_failed_conflict_is_dropped_once_the_copy_is_gone(
    root: Path, send: FakeSend, clock: ManualClock
) -> None:
    watcher = VaultWatcher(root, quiet_seconds=0, send=send, clock=clock)
    watcher.poll_once()
    send.status = 500
    conflict = root / "topics/note (conflicted copy 2024-01-01).md"
    _write(conflict, "x\n")
    assert watcher.poll_once() == []
    send.status = 202
    conflict.unlink()
    watcher.poll_once()
    clock.advance(QUIET)
    watcher.poll_once()
    assert send.kinds[-1] == "note_deleted"
    assert "conflict" not in send.kinds[send.kinds.index("note_deleted") :]


def test_an_unreachable_hermes_does_not_raise(root: Path, clock: ManualClock) -> None:
    from second_brain.sidecars.webhook import WebhookError

    def send(payload: Mapping[str, Any]) -> int:
        raise WebhookError("connection refused")

    watcher = VaultWatcher(root, quiet_seconds=0, send=send, clock=clock)
    watcher.poll_once()
    _write(root / "topics/note.md", "x\n")
    assert watcher.poll_once() == []


# --- construction and the loop --------------------------------------------------------------


def test_send_is_required(root: Path) -> None:
    with pytest.raises(ValueError):
        VaultWatcher(root, quiet_seconds=0, send=None)  # type: ignore[arg-type]


def test_the_root_must_be_a_directory(tmp_path: Path, send: FakeSend) -> None:
    with pytest.raises(ValueError):
        VaultWatcher(tmp_path / "missing", send=send)


def test_a_negative_quiet_window_is_refused(root: Path, send: FakeSend) -> None:
    with pytest.raises(ValueError):
        VaultWatcher(root, quiet_seconds=-1, send=send)


def test_a_useless_poll_interval_is_refused(root: Path, send: FakeSend) -> None:
    with pytest.raises(ValueError):
        VaultWatcher(root, send=send, poll_seconds=0)


class Stopper:
    def __init__(self, after: int, clock: ManualClock) -> None:
        self.after = after
        self.clock = clock
        self.target: VaultWatcher | None = None
        self.calls: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)
        self.clock.advance(seconds)
        if len(self.calls) >= self.after and self.target is not None:
            self.target.stop()


def test_run_forever_polls_then_stops(root: Path, send: FakeSend, clock: ManualClock) -> None:
    sleep = Stopper(2, clock)
    watcher = VaultWatcher(root, quiet_seconds=0, send=send, clock=clock, poll_seconds=7.0, sleep=sleep)
    sleep.target = watcher
    _write(root / "topics/note.md", "x\n")
    watcher.run_forever()
    assert send.calls == [{"kind": "note_created", "path": "topics/note.md"}]
    assert sleep.calls == [7.0, 7.0]


def test_run_forever_survives_a_failing_poll(
    root: Path, clock: ManualClock, caplog: pytest.LogCaptureFixture
) -> None:
    calls: list[int] = []

    def send(payload: Mapping[str, Any]) -> int:
        calls.append(1)
        raise RuntimeError("the scanner broke")

    sleep = Stopper(2, clock)
    watcher = VaultWatcher(root, quiet_seconds=0, send=send, clock=clock, sleep=sleep)
    sleep.target = watcher
    _write(root / "topics/note.md", "x\n")
    watcher.run_forever()
    assert len(calls) == 2
    assert "poll failed" in caplog.text


def test_the_folder_the_call_router_owns_is_never_reported(
    root: Path, send: FakeSend, clock: ManualClock
) -> None:
    watcher = VaultWatcher(
        root, quiet_seconds=QUIET, send=send, clock=clock, poll_seconds=5.0, skip_dirs=["inbox/calls/"]
    )
    _write(root / "inbox/calls/2026-10/call.md", "---\ndate: 2026-10-21\n---\n")
    _write(root / "inbox/capture.md", "---\ntype: capture\n---\n")
    watcher.poll_once()
    clock.advance(QUIET)
    assert watcher.poll_once() == [{"kind": "note_created", "path": "inbox/capture.md"}]
    (root / "inbox/calls/2026-10/call.md").unlink()
    clock.advance(QUIET)
    assert watcher.poll_once() == []
