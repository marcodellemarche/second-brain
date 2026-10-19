"""Vault operations: the only write path into the vault.

Every change is an atomic file operation followed by one git commit whose trailers record who
asked, why, from which session, whether the session had seen untrusted content and whether a
human approved. Paths are vault-relative and resolved inside the root, so a symlink that leaves
the root is refused. A write whose content looks like a credential is refused before it touches
the disk.

The user also edits the vault in an editor, and file sync drops those changes in on its own. They
are committed separately, authored by `human`, before the brain touches anything, so a brain
commit only ever contains the paths the brain changed. A user file that looks like it holds a
credential is left out of that commit and stays in the working tree until the secret is gone, and
the brain then refuses to touch it.

Error codes: `outside_vault`, `zone_denied`, `quiet_window`, `hash_mismatch`, `secret_detected`,
`not_found`, `exists`, `conflict` when a later change makes an undo impossible to apply cleanly,
and `git_failed` when git itself refuses the operation.
"""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
import tempfile
import threading
import time
import weakref
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from second_brain.tools.secrets import scan

_SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")
_PATH_SEPARATORS = re.compile(r"[\\/]+")
_BINARY_SNIFF_BYTES = 8000


class VaultError(Exception):
    """A refused or failed vault operation. `code` is stable and machine-readable."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


@dataclass(frozen=True, slots=True)
class Provenance:
    requested_by: str
    reason: str
    source_integrity: Literal["trusted", "untrusted"]
    session: str
    approval: str = "none"

    def __post_init__(self) -> None:
        if self.source_integrity not in ("trusted", "untrusted"):
            raise ValueError(f"source_integrity must be trusted or untrusted, got {self.source_integrity!r}")


@dataclass(frozen=True, slots=True)
class HeldBack:
    """A user file left uncommitted because it looks like it holds a credential."""

    path: str
    rules: tuple[tuple[str, int], ...]

    def describe(self) -> str:
        found = ", ".join(f"{rule} at line {line}" for rule, line in self.rules)
        return f"{self.path} contains what looks like a secret ({found})"


@dataclass(frozen=True, slots=True)
class HumanCommit:
    """What one commit of the user's own edits did, and what it had to leave out."""

    sha: str | None
    held_back: list[HeldBack]


@dataclass(frozen=True, slots=True)
class _Change:
    """One path with an uncommitted change, plus the source path of a rename or a copy."""

    code: str
    path: str
    origin: str | None = None


class VaultConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    root: Path
    quiet_seconds: float = 120
    denied: list[str] = Field(default_factory=lambda: ["_brain/policy.md", ".git", ".obsidian"])
    approval_required: list[str] = Field(default_factory=list)
    trash_dir: str = ".trash"
    author_name: str = "brain"
    author_email: str = "brain@localhost"
    human_name: str = "human"
    human_email: str = "human@localhost"

    @model_validator(mode="after")
    def _distinct_authors(self) -> VaultConfig:
        """Undo refuses any commit not authored by the brain, so the two emails cannot be one."""
        if self.human_email.strip().lower() == self.author_email.strip().lower():
            raise ValueError("human_email must differ from author_email, or undo would revert human commits")
        return self


_locks_guard = threading.Lock()
_locks: weakref.WeakValueDictionary[Path, threading.Lock] = weakref.WeakValueDictionary()


def _lock_for(root: Path) -> threading.Lock:
    """One lock per vault root, so the background timer and a tool call cannot interleave git."""
    with _locks_guard:
        lock = _locks.get(root)
        if lock is None:
            lock = threading.Lock()
            _locks[root] = lock
        return lock


def _single_line(value: str) -> str:
    """A trailer value cannot smuggle a newline, so it cannot forge another trailer."""
    return " ".join(str(value).split())


def _segments(path: str) -> list[str]:
    return [part for part in _PATH_SEPARATORS.split(path.strip()) if part not in ("", ".")]


def _change_paths(change: _Change) -> tuple[str, ...]:
    """Both sides of a rename or a copy, so the index sees the change the way git status does."""
    return (change.origin, change.path) if change.origin else (change.path,)


class Vault:
    """Read and write one vault. All writes are serialized and committed one per operation."""

    def __init__(self, config: VaultConfig) -> None:
        self._config = config
        self._root = config.root.resolve()
        self._lock = _lock_for(self._root)
        if not self._root.is_dir():
            raise VaultError("outside_vault", f"vault root is not a directory: {self._root}")
        self._trash = self._resolve(self._config.trash_dir)[0]
        self._git("rev-parse", "--git-dir")

    @property
    def config(self) -> VaultConfig:
        return self._config

    @property
    def root(self) -> Path:
        return self._root

    def read(self, path: str) -> tuple[str, str]:
        rel, target = self._resolve(path)
        if not target.is_file():
            raise VaultError("not_found", f"{rel} does not exist")
        payload = target.read_bytes()
        return payload.decode("utf-8"), hashlib.sha256(payload).hexdigest()

    def write(
        self,
        path: str,
        content: str,
        provenance: Provenance,
        expected_sha256: str | None = None,
    ) -> str:
        with self._lock:
            held = self._commit_human_changes_locked().held_back
            rel, target = self._resolve(path)
            self._check_zone(rel, provenance)
            self._check_held_back(rel, held)
            if target.exists() and not target.is_file():
                raise VaultError("exists", f"{rel} is not a regular file")
            self._check_concurrency(rel, target, expected_sha256)
            self._check_secrets(rel, content)
            self._atomic_write(target, content.encode("utf-8"))
            return self._commit(f"write: {rel}", provenance, [rel])

    def move(self, src: str, dst: str, provenance: Provenance) -> str:
        with self._lock:
            held = self._commit_human_changes_locked().held_back
            src_rel, src_path = self._resolve(src)
            dst_rel, dst_path = self._resolve(dst)
            self._check_zone(src_rel, provenance)
            self._check_zone(dst_rel, provenance)
            self._check_held_back(src_rel, held)
            self._check_held_back(dst_rel, held)
            if not src_path.is_file():
                raise VaultError("not_found", f"{src_rel} does not exist")
            if dst_path.exists():
                raise VaultError("exists", f"{dst_rel} already exists")
            dst_path.parent.mkdir(parents=True, exist_ok=True)
            self._git("mv", "--", src_rel, dst_rel)
            return self._commit(f"move: {src_rel} -> {dst_rel}", provenance, [src_rel, dst_rel], staged=True)

    def delete(self, path: str, provenance: Provenance) -> str:
        with self._lock:
            held = self._commit_human_changes_locked().held_back
            rel, target = self._resolve(path)
            self._check_zone(rel, provenance)
            self._check_held_back(rel, held)
            if not target.is_file():
                raise VaultError("not_found", f"{rel} does not exist")
            stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%f")
            trash_rel = f"{self._trash}/{rel}.{stamp}"
            (self._root / trash_rel).parent.mkdir(parents=True, exist_ok=True)
            self._git("mv", "--", rel, trash_rel)
            return self._commit(f"delete: {rel}", provenance, [rel, trash_rel], staged=True)

    def undo(self, commit_sha: str, provenance: Provenance) -> str:
        with self._lock:
            held = self._commit_human_changes_locked().held_back
            try:
                commit = self._git("rev-parse", "--verify", "--quiet", f"{commit_sha}^{{commit}}")
            except VaultError as exc:
                raise VaultError("not_found", f"unknown commit: {commit_sha}") from exc
            self._check_undo(commit, provenance, held)
            try:
                self._git("revert", "--no-edit", commit)
            except VaultError as exc:
                conflicted = (self._root / ".git" / "REVERT_HEAD").exists()
                # A conflicted revert would block every later operation, so put the tree back.
                self._abort_revert()
                if conflicted:
                    raise VaultError(
                        "conflict",
                        f"{commit_sha} cannot be undone cleanly: a later change touched the same lines",
                    ) from exc
                raise exc
            body = self._git("log", "-1", "--format=%B").rstrip()
            self._git("commit", "--amend", "-q", "-m", f"{body}\n\n{self._trailers(provenance)}")
            return self._git("rev-parse", "HEAD")

    def commit_human_changes(self) -> HumanCommit:
        """Commit what the user changed in the vault, as `human`, minus the files that hold a secret."""
        with self._lock:
            return self._commit_human_changes_locked()

    def held_back(self) -> list[HeldBack]:
        """The user files a commit would leave out right now, because they look like a credential."""
        with self._lock:
            return self._scan_changes(self._status_changes())

    def _commit_human_changes_locked(self) -> HumanCommit:
        """The commit holds everything it safely can, so a brain commit after it cannot carry a user edit."""
        changes = self._status_changes()
        held = self._scan_changes(changes)
        held_paths = {entry.path for entry in held}
        files = [change for change in changes if change.path not in held_paths]
        if held_paths:
            # A file the user staged before pasting a secret into it must not sit in the index either,
            # and a half-staged rename of it must not leave a bare deletion behind.
            withheld = [
                path for change in changes if change.path in held_paths for path in _change_paths(change)
            ]
            self._git("reset", "-q", "--", *sorted(set(withheld)))
        if not files:
            return HumanCommit(sha=None, held_back=held)
        paths = [path for change in files for path in _change_paths(change)]
        self._git("add", "-A", "--", *paths)
        self._git(
            "commit",
            "-q",
            "-m",
            f"human: {len(files)} files",
            "-m",
            "Requested-By: human",
            author=(self._config.human_name, self._config.human_email),
        )
        return HumanCommit(sha=self._git("rev-parse", "HEAD"), held_back=held)

    def _status_changes(self) -> list[_Change]:
        """Every path with an uncommitted change, untracked files listed one by one."""
        raw = self._git("status", "--porcelain", "-z", "-uall", strip=False)
        fields = [field for field in raw.split("\0") if field]
        changes: list[_Change] = []
        index = 0
        while index < len(fields):
            code, path = fields[index][:2], fields[index][3:]
            index += 1
            origin = None
            if code[:1] in ("R", "C"):
                origin = fields[index]
                index += 1
            changes.append(_Change(code=code, path=path, origin=origin))
        return changes

    def _scan_changes(self, changes: Iterable[_Change]) -> list[HeldBack]:
        held: list[HeldBack] = []
        for change in changes:
            if "D" in change.code:
                continue
            content = self._read_text(self._root / change.path)
            if content is None:
                continue
            findings = scan(content)
            if findings:
                held.append(
                    HeldBack(
                        path=change.path,
                        rules=tuple((finding.rule, finding.line) for finding in findings),
                    )
                )
        return held

    @staticmethod
    def _read_text(target: Path) -> str | None:
        """The file as text, or None when it is binary or unreadable, where a text secret cannot hide."""
        if not target.is_file():
            return None
        try:
            payload = target.read_bytes()
        except OSError:
            return None
        if b"\0" in payload[:_BINARY_SNIFF_BYTES]:
            return None
        try:
            return payload.decode("utf-8")
        except UnicodeDecodeError:
            return None

    @staticmethod
    def _check_held_back(rel: str, held: Iterable[HeldBack]) -> None:
        """A path the user left dirty with a secret is theirs to clean, so the brain keeps out of it."""
        for entry in held:
            if entry.path == rel:
                raise VaultError(
                    "secret_detected",
                    f"refusing to write {rel}: it already holds what looks like a secret "
                    f"({', '.join(f'{rule} at line {line}' for rule, line in entry.rules)}), remove it first",
                )

    def _check_undo(self, commit: str, provenance: Provenance, held: Iterable[HeldBack]) -> None:
        """Undoing a commit writes its paths back, so it needs the same zone and secret checks as a write."""
        if self._git("log", "-1", "--format=%ae", commit) != self._config.author_email:
            raise VaultError("zone_denied", f"only commits authored by the brain can be undone: {commit}")
        if len(self._git("rev-list", "--parents", "-n", "1", commit).split()) > 2:
            raise VaultError("zone_denied", f"{commit} is a merge commit, which cannot be undone")
        for path in self._commit_paths(commit):
            self._check_zone(path, provenance)
            self._check_held_back(path, held)

    def _commit_paths(self, commit: str) -> list[str]:
        """Every path the commit touches, both sides of a rename, since a revert writes both."""
        fields = self._git(
            "diff-tree", "--no-commit-id", "-r", "-M", "--name-status", "-z", commit, strip=False
        ).split("\0")
        paths: list[str] = []
        index = 0
        while index < len(fields):
            status = fields[index]
            index += 1
            width = 2 if status[:1] in ("R", "C") else 1
            paths.extend(fields[index : index + width])
            index += width
        return [path for path in paths if path]

    def _abort_revert(self) -> None:
        subprocess.run(
            ["git", "-C", str(self._root), "revert", "--abort"],
            capture_output=True,
            check=False,
        )

    def _resolve(self, path: str) -> tuple[str, Path]:
        """Resolve a vault-relative path, refusing anything that lands outside the root."""
        segments = _segments(path)
        if not segments:
            raise VaultError("outside_vault", f"empty path: {path!r}")
        if ".." in segments:
            raise VaultError("outside_vault", f"parent segment in path: {path!r}")
        if Path(path.strip()).is_absolute() or path.strip().startswith(("/", "\\")):
            raise VaultError("outside_vault", f"absolute path: {path!r}")
        target = (self._root / Path(*segments)).resolve()
        if self._root not in target.parents:
            raise VaultError("outside_vault", f"path leaves the vault: {path!r}")
        return target.relative_to(self._root).as_posix(), target

    def _check_zone(self, rel: str, provenance: Provenance) -> None:
        """Denied prefixes refuse the write; approval-required prefixes refuse it without approval."""
        if self._matches(rel, self._config.denied):
            raise VaultError("zone_denied", f"{rel} is not writable by the agent")
        if self._matches(rel, self._config.approval_required) and provenance.approval == "none":
            raise VaultError("zone_denied", f"{rel} needs an explicit approval")

    @staticmethod
    def _matches(rel: str, prefixes: Iterable[str]) -> bool:
        for prefix in prefixes:
            candidate = "/".join(_segments(prefix))
            if candidate and (rel == candidate or rel.startswith(candidate + "/")):
                return True
        return False

    def _check_secrets(self, rel: str, content: str) -> None:
        findings = scan(content)
        if findings:
            # Rule and line only: the matched value never reaches a log or an error message.
            found = ", ".join(f"{finding.rule} at line {finding.line}" for finding in findings)
            raise VaultError("secret_detected", f"refusing to write {rel}: {found}")

    def _check_concurrency(self, rel: str, target: Path, expected_sha256: str | None) -> None:
        if not target.is_file():
            if expected_sha256 is not None:
                raise VaultError("hash_mismatch", f"{rel} does not exist, so it has no expected content")
            return
        current = hashlib.sha256(target.read_bytes()).hexdigest()
        if expected_sha256 is not None:
            if not _SHA256_HEX.match(expected_sha256) or expected_sha256 != current:
                raise VaultError("hash_mismatch", f"{rel} changed since it was read, read it again")
            return
        age = time.time() - target.stat().st_mtime
        if age < self._config.quiet_seconds:
            raise VaultError(
                "quiet_window",
                f"{rel} was modified {age:.0f}s ago (quiet window {self._config.quiet_seconds:.0f}s)",
            )

    def _atomic_write(self, target: Path, payload: bytes) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        handle_fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=".tmp-")
        try:
            with os.fdopen(handle_fd, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(tmp, 0o644)
            os.replace(tmp, target)
            self._fsync_dir(target.parent)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    @staticmethod
    def _fsync_dir(directory: Path) -> None:
        fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def _commit(
        self,
        subject: str,
        provenance: Provenance,
        paths: list[str],
        *,
        staged: bool = False,
    ) -> str:
        if not staged:
            self._git("add", "-A", "--", *paths)
        self._git("commit", "-q", "-m", subject, "-m", self._trailers(provenance), "--", *paths)
        return self._git("rev-parse", "HEAD")

    @staticmethod
    def _trailers(provenance: Provenance) -> str:
        return "\n".join(
            [
                f"Requested-By: {_single_line(provenance.requested_by)}",
                f"Reason: {_single_line(provenance.reason)}",
                f"Source-Integrity: {_single_line(provenance.source_integrity)}",
                f"Session: {_single_line(provenance.session)}",
                f"Approval: {_single_line(provenance.approval)}",
            ]
        )

    def _git(
        self,
        *args: str,
        strip: bool = True,
        author: tuple[str, str] | None = None,
    ) -> str:
        name, email = author if author is not None else (self._config.author_name, self._config.author_email)
        command = [
            "git",
            "-C",
            str(self._root),
            "-c",
            f"user.name={name}",
            "-c",
            f"user.email={email}",
            "-c",
            "commit.gpgsign=false",
            *args,
        ]
        result = subprocess.run(command, capture_output=True, text=True, check=False)
        if result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip() or "no output"
            raise VaultError("git_failed", f"git {args[0]} failed: {detail}")
        return result.stdout.strip() if strip else result.stdout
