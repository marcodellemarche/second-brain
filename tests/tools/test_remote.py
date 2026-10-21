"""Tests for publishing the vault into a folder of the GitHub repo, and pulling it back.

The remote is a local bare repository reached through `file://`, which the constructor only accepts
when the test asks for it. Every fake value is built at runtime.
"""

from __future__ import annotations

import base64
import subprocess
from pathlib import Path

import pytest

from second_brain.tools.remote import RemoteError, VaultRemote
from second_brain.tools.vault import Provenance, Vault, VaultConfig

TOKEN = "ghp_" + "fake" * 9


def _provenance(**overrides: object) -> Provenance:
    values: dict[str, object] = {
        "requested_by": "user-1",
        "reason": "test write",
        "source_integrity": "trusted",
        "session": "session-1",
    }
    values.update(overrides)
    return Provenance(**values)  # type: ignore[arg-type]


@pytest.fixture(autouse=True)
def _git_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the developer's own git config out: a signed commit or a hook would fail the test."""
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", "/dev/null")
    monkeypatch.setenv("GIT_AUTHOR_NAME", "tester")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "tester@localhost")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "tester")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "tester@localhost")


@pytest.fixture
def root(tmp_path: Path) -> Path:
    repo = tmp_path / "vault"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "commit", "-q", "--allow-empty", "-m", "init")
    return repo


@pytest.fixture
def vault(root: Path) -> Vault:
    # A sync writes over what the brain just wrote, so the quiet window the editor gets is not in play.
    return Vault(VaultConfig(root=root, quiet_seconds=0))


@pytest.fixture
def remote(tmp_path: Path) -> Path:
    bare = tmp_path / "remote.git"
    _git(tmp_path, "init", "-q", "--bare", str(bare))
    return bare


@pytest.fixture
def token_file(tmp_path: Path) -> Path:
    path = tmp_path / "token"
    path.write_text(TOKEN, encoding="utf-8")
    return path


def _sync(
    vault: Vault,
    tmp_path: Path,
    remote: Path,
    token_file: Path,
    **overrides: object,
) -> VaultRemote:
    values: dict[str, object] = {
        "workdir": tmp_path / "clone",
        "url": f"file://{remote}",
        "token_file": token_file,
        "state_file": tmp_path / "state.json",
        "allow_file_urls": True,
    }
    values.update(overrides)
    return VaultRemote(vault, **values)  # type: ignore[arg-type]


@pytest.fixture
def sync(
    vault: Vault,
    tmp_path: Path,
    remote: Path,
    token_file: Path,
) -> VaultRemote:
    return _sync(vault, tmp_path, remote, token_file)


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, check=True)
    return result.stdout.strip()


def _colleague(
    remote: Path,
    repo: Path,
    message: str,
    *,
    write: dict[str, str] | None = None,
    remove: list[str] | None = None,
) -> str:
    """Change the remote the way a colleague would: a checkout, a commit, a push."""
    if not (repo / ".git").exists():
        _git(repo.parent, "init", "-q", "-b", "main", str(repo))
    has_branch = subprocess.run(
        ["git", "-C", str(repo), "ls-remote", "--exit-code", "--heads", str(remote), "main"],
        capture_output=True,
        check=False,
    )
    if has_branch.returncode == 0:
        _git(repo, "fetch", "-q", str(remote), "main")
        _git(repo, "reset", "-q", "--hard", "FETCH_HEAD")
    for path, content in (write or {}).items():
        target = repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    for path in remove or []:
        _git(repo, "rm", "-q", "--", path)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message)
    _git(repo, "push", "-q", str(remote), "main")
    return _git(repo, "rev-parse", "HEAD")


def _remote_paths(remote: Path) -> list[str]:
    return _git(remote, "ls-tree", "-r", "--name-only", "main").splitlines()


def test_first_sync_puts_the_vault_under_the_prefix(vault: Vault, sync: VaultRemote, remote: Path) -> None:
    vault.write("notes/a.md", "hello\n", _provenance())
    vault.write("topics/b.md", "world\n", _provenance())

    result = sync.sync_once()

    assert result.pushed is not None
    assert result.conflicts == []
    assert sorted(_remote_paths(remote)) == ["vault/notes/a.md", "vault/topics/b.md"]
    assert _git(remote, "log", "--format=%s", "main").splitlines() == [
        "Merge the vault into vault/",
        "write: topics/b.md",
        "write: notes/a.md",
        "init",
    ]


def test_first_sync_leaves_existing_code_alone(vault: Vault, sync: VaultRemote, remote: Path) -> None:
    _colleague(remote, remote.parent / "code", "code", write={"src/app.py": "print(1)\n"})
    vault.write("notes/a.md", "hello\n", _provenance())

    sync.sync_once()

    assert sorted(_remote_paths(remote)) == ["src/app.py", "vault/notes/a.md"]
    assert _git(remote, "show", "main:src/app.py") == "print(1)"


def test_later_vault_write_is_pushed_on_the_next_sync(vault: Vault, sync: VaultRemote, remote: Path) -> None:
    vault.write("notes/a.md", "hello\n", _provenance())
    first = sync.sync_once()
    vault.write("notes/a.md", "hello again\n", _provenance())

    second = sync.sync_once()

    assert first.pushed != second.pushed
    assert _git(remote, "show", "main:vault/notes/a.md") == "hello again"
    assert second.pushed == _git(remote, "rev-parse", "main")


def test_pull_writes_the_remote_change_into_the_vault(
    vault: Vault, sync: VaultRemote, remote: Path, root: Path, token_file: Path
) -> None:
    vault.write("notes/a.md", "hello\n", _provenance())
    sync.sync_once()
    _colleague(
        remote, remote.parent / "code", "edit from the team", write={"vault/notes/a.md": "hello team\n"}
    )

    result = _sync(vault, root.parent, remote, token_file, pull=True).sync_once()

    assert result.pulled == ["notes/a.md"]
    assert (root / "notes/a.md").read_text() == "hello team\n"
    assert "Requested-By: remote" in _git(root, "log", "-1", "--format=%B")


def test_pull_deletes_the_file_the_remote_deleted(
    vault: Vault, sync: VaultRemote, remote: Path, root: Path, token_file: Path
) -> None:
    vault.write("notes/a.md", "hello\n", _provenance())
    vault.write("notes/b.md", "keep\n", _provenance())
    sync.sync_once()
    _colleague(remote, remote.parent / "code", "delete a", remove=["vault/notes/a.md"])

    result = _sync(vault, root.parent, remote, token_file, pull=True).sync_once()

    assert result.pulled == ["notes/a.md"]
    assert not (root / "notes/a.md").exists()
    assert (root / "notes/b.md").exists()


def test_a_delete_pulled_from_the_remote_is_pushed_back_without_a_conflict(
    vault: Vault, sync: VaultRemote, remote: Path, root: Path, token_file: Path
) -> None:
    # Long enough for git to see the trash move as a rename, the way a real note is.
    vault.write(
        "notes/a.md", "---\ntype: capture\n---\n\nA note long enough to look renamed.\n", _provenance()
    )
    sync.sync_once()
    _colleague(remote, remote.parent / "code", "delete a", remove=["vault/notes/a.md"])
    pulling = _sync(vault, root.parent, remote, token_file, pull=True)

    first = pulling.sync_once()
    second = pulling.sync_once()

    assert first.pulled == ["notes/a.md"]
    assert first.conflicts == []
    assert first.pushed is not None
    assert second.conflicts == []
    assert second.pulled == []


def test_a_file_changed_on_both_sides_is_a_conflict(
    vault: Vault, sync: VaultRemote, remote: Path, root: Path, token_file: Path
) -> None:
    vault.write("notes/a.md", "hello\n", _provenance())
    sync.sync_once()
    _colleague(
        remote, remote.parent / "code", "edit from the team", write={"vault/notes/a.md": "hello team\n"}
    )
    vault.write("notes/a.md", "hello brain\n", _provenance())

    result = _sync(vault, root.parent, remote, token_file, pull=True).sync_once()

    assert result.conflicts[0] == "notes/a.md"
    assert result.pulled == []
    assert (root / "notes/a.md").read_text() == "hello brain\n"
    assert _git(remote, "show", "main:vault/notes/a.md") == "hello team"


def test_a_conflict_blocks_the_push_until_it_is_settled(
    vault: Vault, sync: VaultRemote, remote: Path, root: Path, token_file: Path
) -> None:
    vault.write("notes/a.md", "hello\n", _provenance())
    sync.sync_once()
    _colleague(
        remote, remote.parent / "code", "edit from the team", write={"vault/notes/a.md": "hello team\n"}
    )
    vault.write("notes/a.md", "hello brain\n", _provenance())
    vault.write("notes/b.md", "unrelated\n", _provenance())
    pulling = _sync(vault, root.parent, remote, token_file, pull=True)

    first = pulling.sync_once()
    second = pulling.sync_once()

    # Publishing now would put the brain's version over the team's: nothing moves, and the same
    # conflict is found again on the next run instead of being forgotten.
    assert first.pushed is None
    assert second.conflicts == ["notes/a.md"]
    assert _git(remote, "show", "main:vault/notes/a.md") == "hello team"

    vault.write("notes/a.md", "hello team\n", _provenance())
    settled = pulling.sync_once()
    assert settled.conflicts == []
    assert settled.pushed is not None
    assert _git(remote, "show", "main:vault/notes/b.md") == "unrelated"


def test_the_guard_refuses_a_merge_that_touches_the_code(
    vault: Vault, sync: VaultRemote, remote: Path, root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault.write("notes/a.md", "hello\n", _provenance())
    sync.sync_once()
    vault.write("notes/a.md", "hello again\n", _provenance())
    original = VaultRemote._merge_vault

    def merge_then_touch_code(self: VaultRemote, token: str, conflicts: list[str]) -> bool:
        merged = original(self, token, conflicts)
        (self._workdir / "src").mkdir(exist_ok=True)
        (self._workdir / "src/app.py").write_text("print(1)\n", encoding="utf-8")
        _git(self._workdir, "add", "-A")
        _git(self._workdir, "commit", "-q", "-m", "code sneaks in")
        return merged

    monkeypatch.setattr(VaultRemote, "_merge_vault", merge_then_touch_code)

    with pytest.raises(RemoteError) as error:
        sync.sync_once()

    assert "outside vault/" in str(error.value)
    assert _git(remote, "show", "main:vault/notes/a.md") == "hello"


def test_a_rejected_push_is_not_an_error(
    vault: Vault, sync: VaultRemote, remote: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault.write("notes/a.md", "hello\n", _provenance())
    sync.sync_once()
    vault.write("notes/a.md", "hello again\n", _provenance())
    original = VaultRemote._check_guard

    def colleague_pushes_first(self: VaultRemote, token: str, has_branch: bool) -> None:
        _colleague(remote, remote.parent / "other", "code", write={"src/app.py": "print(1)\n"})
        original(self, token, has_branch)

    monkeypatch.setattr(VaultRemote, "_check_guard", colleague_pushes_first)

    result = sync.sync_once()

    assert result.pushed is None
    assert _git(remote, "show", "main:vault/notes/a.md") == "hello"

    monkeypatch.setattr(VaultRemote, "_check_guard", original)
    second = sync.sync_once()

    assert second.pushed is not None
    assert _git(remote, "show", "main:vault/notes/a.md") == "hello again"
    assert _git(remote, "show", "main:src/app.py") == "print(1)"


def test_a_pulled_note_that_holds_a_secret_is_refused(
    vault: Vault, sync: VaultRemote, remote: Path, root: Path, token_file: Path
) -> None:
    vault.write("notes/a.md", "hello\n", _provenance())
    sync.sync_once()
    fake_key = "AKIA" + "Q" * 16
    _colleague(remote, remote.parent / "code", "leak", write={"vault/notes/b.md": f"key {fake_key}\n"})

    result = _sync(vault, root.parent, remote, token_file, pull=True).sync_once()

    assert result.refused == ["notes/b.md: secret_detected"]
    assert not (root / "notes/b.md").exists()


def test_the_token_never_lands_in_the_clone_config(vault: Vault, sync: VaultRemote, remote: Path) -> None:
    vault.write("notes/a.md", "hello\n", _provenance())
    sync.sync_once()

    config = (sync._workdir / ".git/config").read_text(encoding="utf-8")

    assert TOKEN not in config
    assert base64.b64encode(f"x-access-token:{TOKEN}".encode()).decode() not in config


def test_the_token_never_lands_in_an_error_message(vault: Vault, tmp_path: Path, token_file: Path) -> None:
    sync = VaultRemote(
        vault,
        tmp_path / "clone",
        "https://127.0.0.1:1/me/repo.git",
        token_file,
        state_file=tmp_path / "state.json",
    )

    with pytest.raises(RemoteError) as error:
        sync.sync_once()

    assert TOKEN not in str(error.value)
    assert base64.b64encode(f"x-access-token:{TOKEN}".encode()).decode() not in str(error.value)


def test_a_url_that_is_not_https_is_refused(vault: Vault, tmp_path: Path, remote: Path) -> None:
    with pytest.raises(ValueError):
        VaultRemote(
            vault,
            tmp_path / "clone",
            f"file://{remote}",
            tmp_path / "token",
            state_file=tmp_path / "state.json",
        )
    with pytest.raises(ValueError):
        VaultRemote(
            vault,
            tmp_path / "clone",
            "git@example.invalid:me/repo.git",
            tmp_path / "token",
            state_file=tmp_path / "state.json",
        )


def test_a_missing_token_file_is_refused(vault: Vault, tmp_path: Path, remote: Path) -> None:
    sync = VaultRemote(
        vault,
        tmp_path / "clone",
        f"file://{remote}",
        tmp_path / "nope",
        state_file=tmp_path / "state.json",
        allow_file_urls=True,
    )

    with pytest.raises(RemoteError):
        sync.sync_once()
