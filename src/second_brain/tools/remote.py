"""Publish the vault into a folder of the brain's own GitHub repo, and bring the team's changes back.

The vault is its own local git repository, and it stays exactly as it is. This module keeps a
separate clone of the GitHub repo and moves history between the two:

- push: the vault's commits are fetched into the clone and merged under `prefix/` as a subtree, so
  they keep their subjects and their trailers. The vault and the clone share no history, so the first
  merge goes through `-s ours` plus `read-tree --prefix` and later ones use `-X subtree=<prefix>`.
  When the remote branch does not exist yet the vault's commits become the parents of its root, so
  the history is there from the first push.
- pull: what changed under `prefix/` on the remote since the last sync is written into the vault
  through `Vault.write` and `Vault.delete`, so it lands as a normal brain commit with its own
  provenance. A path both sides changed is skipped and reported, never overwritten.

The guard in `_check_guard` is what keeps a brain away from the code that lives in the same repo: a
merge that touches anything outside `prefix/` is refused before the push, not after.

The GitHub token is read from a file on every sync and reaches git only through the environment
(`GIT_CONFIG_COUNT` / `http.extraHeader`), so it never lands in `.git/config`, in a URL, in argv or
in an error message.

Error codes are not used here: a refusal that must not be retried silently (a URL that is not
https, the guard, a missing token file, a failed push) raises `RemoteError`.
"""

from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, field_validator

from second_brain.tools.vault import Provenance, Vault, VaultError

# A push the remote refuses because it moved on is normal: the next sync starts again from the
# remote branch, so nothing is lost by giving up quietly here.
_REJECTION_MARKERS = ("rejected", "non-fast-forward", "fetch first", "stale info")
_COMMIT_SHA = re.compile(r"^[0-9a-f]{40,64}$")


class RemoteError(Exception):
    """A refusal that must not be retried silently."""


@dataclass(frozen=True)
class SyncResult:
    """What one sync did: the remote commit after it, the paths moved, and what it left alone."""

    pushed: str | None
    pulled: list[str]
    conflicts: list[str]
    refused: list[str]


def _prefix_segments(prefix: str) -> list[str]:
    segments = [part for part in prefix.strip().strip("/").split("/") if part not in ("", ".")]
    if not segments:
        raise ValueError(f"prefix must name a folder, got {prefix!r}")
    if ".." in segments:
        raise ValueError(f"prefix must not climb out of the repo, got {prefix!r}")
    return segments


def _detail(result: subprocess.CompletedProcess[str]) -> str:
    """A one-line reason from a failed git command, without the token that never goes in argv."""
    text = (result.stdout or "") + "\n" + (result.stderr or "")
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("CONFLICT"):
            return stripped
    return result.stderr.strip() or result.stdout.strip() or "no output"


class RemoteConfig(BaseModel):
    """The repo the vault is published into: the token is a file, and the clone lives under /data."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    url: str = Field(min_length=1)
    branch: str = Field(default="main", min_length=1)
    prefix: str = Field(default="vault", min_length=1)
    pull: bool = False
    token_file: Path
    workdir: Path = Field(default=Path("/data/remote"))
    state_file: Path = Field(default=Path("/data/remote.json"))
    poll_seconds: float = Field(default=300, gt=0)

    @field_validator("url")
    @classmethod
    def _https(cls, value: str) -> str:
        if not value.startswith("https://") or len(value) == len("https://"):
            raise ValueError(f"the remote URL must be https, got {value!r}")
        return value

    def remote(self, vault: Vault) -> VaultRemote:
        return VaultRemote(
            vault,
            self.workdir,
            self.url,
            self.token_file,
            branch=self.branch,
            prefix=self.prefix,
            pull=self.pull,
            state_file=self.state_file,
        )


class VaultRemote:
    """One vault, one clone of the GitHub repo, one folder of it. `sync_once` does one round."""

    def __init__(
        self,
        vault: Vault,
        workdir: Path,
        url: str,
        token_file: Path,
        *,
        branch: str = "main",
        prefix: str = "vault",
        pull: bool = False,
        state_file: Path,
        allow_file_urls: bool = False,
    ) -> None:
        self._vault = vault
        self._workdir = Path(workdir)
        self._url = url
        self._token_file = Path(token_file)
        self._branch = branch
        self._prefix = "/".join(_prefix_segments(prefix))
        self._pull = pull
        self._state_file = Path(state_file)
        self._allow_file_urls = allow_file_urls
        self._check_url()

    @property
    def prefix(self) -> str:
        return self._prefix

    def sync_once(self) -> SyncResult:
        token = self._read_token()
        self._ensure_clone(token)
        has_branch = self._reset_to_remote(token)
        pulled: list[str] = []
        conflicts: list[str] = []
        refused: list[str] = []
        if self._pull and has_branch:
            last = self._last_synced()
            head = self._rev(token, f"origin/{self._branch}")
            if last is not None and head is not None and last != head:
                self._pull_remote(token, last, head, pulled, conflicts, refused)
        pushed = self._push(token, has_branch, conflicts)
        remote = pushed if pushed is not None else self._rev(token, f"origin/{self._branch}")
        self._write_state(remote)
        return SyncResult(pushed=pushed, pulled=pulled, conflicts=conflicts, refused=refused)

    def _check_url(self) -> None:
        parsed = urlparse(self._url)
        if parsed.scheme == "file" and self._allow_file_urls:
            return
        if parsed.scheme != "https":
            raise ValueError(f"the remote URL must be https, got {parsed.scheme or 'no scheme'}")
        if not parsed.hostname:
            raise ValueError("the remote URL has no host")
        if parsed.username or parsed.password:
            raise ValueError("the remote URL must not carry credentials, the token comes from a file")

    def _read_token(self) -> str:
        """Read the token now, so a rotated one is used on the next sync without a restart."""
        try:
            token = self._token_file.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise RemoteError(
                f"cannot read the token file {self._token_file}: {exc.strerror or exc}"
            ) from exc
        if not token:
            raise RemoteError(f"the token file {self._token_file} is empty")
        return token

    def _env(self, token: str) -> dict[str, str]:
        """The header goes in through the environment: never in argv, a URL or `.git/config`."""
        env = dict(os.environ)
        env["GIT_TERMINAL_PROMPT"] = "0"
        encoded = base64.b64encode(f"x-access-token:{token}".encode()).decode("ascii")
        env["GIT_CONFIG_COUNT"] = "1"
        env["GIT_CONFIG_KEY_0"] = "http.extraHeader"
        env["GIT_CONFIG_VALUE_0"] = f"Authorization: Basic {encoded}"
        return env

    def _run(
        self,
        token: str,
        *args: str,
        cwd: Path | None = None,
        text: bool = True,
    ) -> subprocess.CompletedProcess:
        command = ["git", "-C", str(cwd or self._workdir), *args]
        return subprocess.run(
            command,
            capture_output=True,
            text=text,
            check=False,
            env=self._env(token),
        )

    def _git(self, token: str, *args: str, cwd: Path | None = None, strip: bool = True) -> str:
        result = self._run(token, *args, cwd=cwd)
        if result.returncode != 0:
            raise RemoteError(f"git {args[0]} failed: {_detail(result)}")
        return result.stdout.strip() if strip else result.stdout

    def _rev(self, token: str, rev: str, cwd: Path | None = None) -> str | None:
        result = self._run(token, "rev-parse", "--verify", "--quiet", rev, cwd=cwd)
        if result.returncode != 0:
            return None
        return result.stdout.strip() or None

    def _ensure_clone(self, token: str) -> None:
        if not (self._workdir / ".git").exists():
            self._workdir.parent.mkdir(parents=True, exist_ok=True)
            self._git(
                token,
                "clone",
                "--quiet",
                self._url,
                str(self._workdir),
                cwd=self._workdir.parent,
            )
        config = self._vault.config
        origin = self._run(token, "config", "--get", "remote.origin.url")
        if origin.returncode != 0:
            self._git(token, "remote", "add", "origin", self._url)
        elif origin.stdout.strip() != self._url:
            self._git(token, "remote", "set-url", "origin", self._url)
        self._git(token, "config", "user.name", config.author_name)
        self._git(token, "config", "user.email", config.author_email)
        self._git(token, "config", "commit.gpgsign", "false")

    def _reset_to_remote(self, token: str) -> bool:
        """Drop whatever a previous run left behind, so a failed push loses nothing."""
        exists = self._remote_branch_exists(token)
        if exists:
            self._git(token, "checkout", "--force", "-B", self._branch, f"origin/{self._branch}")
        else:
            self._start_branch(token)
        self._git(token, "clean", "-ffd", "--quiet")
        return exists

    def _remote_branch_exists(self, token: str) -> bool:
        result = self._run(token, "ls-remote", "--exit-code", "--heads", "origin", self._branch)
        if result.returncode == 0:
            self._git(token, "fetch", "--quiet", "origin", self._branch)
            return True
        if result.returncode == 2:
            return False
        raise RemoteError(f"git ls-remote failed: {_detail(result)}")

    def _start_branch(self, token: str) -> None:
        """An unborn branch: the remote has nothing on it yet, and the first merge makes its root."""
        self._run(token, "branch", "-D", self._branch)
        self._git(token, "checkout", "--force", "--orphan", self._branch)
        self._git(token, "reset", "--quiet")

    def _last_synced(self) -> str | None:
        try:
            data = json.loads(self._state_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(data, dict):
            return None
        value = data.get("last_synced")
        if isinstance(value, str) and _COMMIT_SHA.match(value):
            return value
        return None

    def _write_state(self, last_synced: str | None) -> None:
        self._state_file.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps({"last_synced": last_synced}).encode("utf-8")
        handle_fd, tmp = tempfile.mkstemp(dir=self._state_file.parent, prefix=".tmp-")
        try:
            with os.fdopen(handle_fd, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(tmp, 0o600)
            os.replace(tmp, self._state_file)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    def _pull_remote(
        self,
        token: str,
        last: str,
        head: str,
        pulled: list[str],
        conflicts: list[str],
        refused: list[str],
    ) -> None:
        if self._rev(token, f"{last}^{{commit}}") is None:
            conflicts.append(f"{last[:12]} is gone from the remote history, nothing pulled")
            return
        provenance = Provenance(
            requested_by="remote",
            reason=f"merged on {self._branch} as {head[:12]}",
            source_integrity="trusted",
            session="remote",
        )
        raw = self._git(
            token,
            "diff",
            "--name-status",
            "--no-renames",
            "-z",
            last,
            head,
            "--",
            f"{self._prefix}/",
            strip=False,
        )
        fields = [field for field in raw.split("\0") if field]
        for status, path in zip(fields[0::2], fields[1::2], strict=False):
            if not path.startswith(f"{self._prefix}/"):
                continue
            rel = path[len(self._prefix) + 1 :]
            if status.startswith("D"):
                self._pull_delete(rel, provenance, pulled, refused)
            else:
                self._pull_write(token, last, head, path, rel, provenance, pulled, conflicts, refused)

    def _pull_write(
        self,
        token: str,
        last: str,
        head: str,
        path: str,
        rel: str,
        provenance: Provenance,
        pulled: list[str],
        conflicts: list[str],
        refused: list[str],
    ) -> None:
        new = self._blob(token, head, path)
        if new is None:
            return
        old = self._blob(token, last, path)
        local, binary = self._local_state(rel, refused)
        if binary:
            conflicts.append(f"{rel}: binary, not pulled")
            return
        if local is not None and local != old and local != new:
            conflicts.append(rel)
            return
        if local == new:
            return
        try:
            text = new.decode("utf-8")
        except UnicodeDecodeError:
            conflicts.append(f"{rel}: binary, not pulled")
            return
        try:
            self._vault.write(rel, text, provenance)
        except VaultError as exc:
            refused.append(f"{rel}: {exc.code}")
            return
        pulled.append(rel)

    def _pull_delete(self, rel: str, provenance: Provenance, pulled: list[str], refused: list[str]) -> None:
        local, binary = self._local_state(rel, refused)
        if local is None and not binary:
            return
        try:
            self._vault.delete(rel, provenance)
        except VaultError as exc:
            refused.append(f"{rel}: {exc.code}")
            return
        pulled.append(rel)

    def _blob(self, token: str, rev: str, path: str) -> bytes | None:
        result = self._run(token, "cat-file", "-p", f"{rev}:{path}", text=False)
        if result.returncode != 0:
            return None
        return result.stdout

    def _local_state(self, rel: str, refused: list[str]) -> tuple[bytes | None, bool]:
        """The local file and whether it is binary, where there is nothing to compare as text."""
        try:
            text, _ = self._vault.read(rel)
        except VaultError as exc:
            if exc.code != "not_found":
                refused.append(f"{rel}: {exc.code}")
            return None, False
        except UnicodeDecodeError:
            return None, True
        return text.encode("utf-8"), False

    def _push(self, token: str, has_branch: bool, conflicts: list[str]) -> str | None:
        if self._rev(token, "HEAD", cwd=self._vault.root) is None:
            return None
        self._git(token, "fetch", "--quiet", str(self._vault.root), "HEAD")
        if not self._merge_vault(token, conflicts):
            return None
        self._check_guard(token, has_branch)
        if self._rev(token, "HEAD") == self._rev(token, f"origin/{self._branch}"):
            return None
        result = self._run(token, "push", "--quiet", "origin", f"HEAD:refs/heads/{self._branch}")
        if result.returncode != 0:
            detail = _detail(result)
            if any(marker in detail for marker in _REJECTION_MARKERS):
                return None
            raise RemoteError(f"git push failed: {detail}")
        return self._git(token, "rev-parse", "HEAD")

    def _prefix_in_head(self, token: str) -> bool:
        return self._run(token, "cat-file", "-e", f"HEAD:{self._prefix}").returncode == 0

    def _merge_vault(self, token: str, conflicts: list[str]) -> bool:
        """Merge the vault under the prefix. Returns False when the merge stopped on a conflict."""
        message = f"Merge the vault into {self._prefix}/"
        if self._rev(token, "HEAD") is None:
            return self._merge_into_unborn(token, message, conflicts)
        if not self._prefix_in_head(token):
            result = self._run(
                token,
                "merge",
                "-s",
                "ours",
                "--no-commit",
                "--allow-unrelated-histories",
                "FETCH_HEAD",
            )
            if result.returncode == 0:
                result = self._run(token, "read-tree", "--prefix", f"{self._prefix}/", "-u", "FETCH_HEAD")
            if result.returncode != 0:
                self._stop_merge(token, result, conflicts)
                return False
            self._git(token, "commit", "--quiet", "-m", message)
            return True
        result = self._run(
            token,
            "merge",
            "--no-commit",
            "--no-ff",
            "-X",
            f"subtree={self._prefix}",
            "FETCH_HEAD",
        )
        if result.returncode != 0:
            self._stop_merge(token, result, conflicts)
            return False
        if self._rev(token, "MERGE_HEAD") is None:
            return True
        self._git(token, "commit", "--quiet", "-m", message)
        return True

    def _merge_into_unborn(
        self,
        token: str,
        message: str,
        conflicts: list[str],
    ) -> bool:
        """No branch to merge into yet, so the vault's own commits become the root's parents."""
        result = self._run(token, "read-tree", "--prefix", f"{self._prefix}/", "-u", "FETCH_HEAD")
        if result.returncode != 0:
            self._stop_merge(token, result, conflicts)
            return False
        tree = self._git(token, "write-tree")
        commit = self._git(token, "commit-tree", tree, "-p", "FETCH_HEAD", "-m", message)
        self._git(token, "update-ref", f"refs/heads/{self._branch}", commit)
        return True

    def _stop_merge(
        self,
        token: str,
        result: subprocess.CompletedProcess[str],
        conflicts: list[str],
    ) -> None:
        conflicts.append(f"merge with the vault stopped: {_detail(result)}")
        self._run(token, "merge", "--abort")
        self._run(token, "read-tree", "--reset", "-u", "HEAD")
        self._run(token, "clean", "-ffd", "--quiet")

    def _check_guard(self, token: str, has_branch: bool) -> None:
        """Everything the merge brings in has to live under the prefix, or the code is not safe."""
        if has_branch:
            raw = self._git(token, "diff", "--name-only", "-z", f"origin/{self._branch}", "HEAD", strip=False)
        else:
            raw = self._git(token, "ls-tree", "-r", "--name-only", "-z", "HEAD", strip=False)
        outside = [
            path
            for path in (field.strip() for field in raw.split("\0"))
            if path and not path.startswith(f"{self._prefix}/")
        ]
        if outside:
            listed = ", ".join(sorted(outside)[:10])
            raise RemoteError(f"the merge touches files outside {self._prefix}/: {listed}")
