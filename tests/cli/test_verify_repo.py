"""`sb verify-repo` against real temporary repositories, one history per test.

Every case builds a tiny repository with the commits it needs, so the check is exercised against
what git actually prints rather than against a canned log.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from second_brain.cli.main import main
from second_brain.cli.verify_repo import verify_repo

BRAIN = "brain@localhost"
OWNER = "owner@example.com"


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
        env=_env(),
    )


def _env() -> dict[str, str]:
    env = dict(os.environ)
    env.update(
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_CONFIG_SYSTEM=os.devnull,
        GIT_AUTHOR_NAME="test",
        GIT_COMMITTER_NAME="test",
    )
    return env


def _commit(repo: Path, files: dict[str, str], message: str, email: str) -> None:
    for name, content in files.items():
        target = repo / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "-c", f"user.email={email}", "-c", "user.name=test", "commit", "-q", "-m", message)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    directory = tmp_path / "brain"
    directory.mkdir()
    _git(directory, "init", "-q", "-b", "main")
    _git(directory, "config", "core.hooksPath", str(tmp_path / "no-hooks"))
    _commit(directory, {"vault/README.md": "vault\n"}, "start the vault", OWNER)
    return directory


def _vault_repo(parent: Path) -> Path:
    vault = parent / "vault-source"
    vault.mkdir()
    _git(vault, "init", "-q", "-b", "main")
    _git(vault, "config", "core.hooksPath", str(parent / "no-hooks"))
    _commit(vault, {"notes/a.md": "a\n"}, "capture a note", BRAIN)
    return vault


def _merge_vault(repo: Path, vault: Path, extra: str | None) -> None:
    _git(repo, "remote", "add", "vault", str(vault))
    _git(repo, "fetch", "-q", "vault")
    head = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "vault/main"],
        capture_output=True,
        text=True,
        check=True,
        env=_env(),
    ).stdout.strip()
    _git(
        repo,
        "-c",
        f"user.email={BRAIN}",
        "-c",
        "user.name=test",
        "merge",
        "-s",
        "ours",
        "--no-commit",
        "--allow-unrelated-histories",
        head,
    )
    _git(repo, "read-tree", "--prefix=vault/", "-u", head)
    if extra is not None:
        target = repo / extra
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("evil\n", encoding="utf-8")
        _git(repo, "add", "-A")
    _git(repo, "-c", f"user.email={BRAIN}", "-c", "user.name=test", "commit", "-q", "-m", "merge the vault")


def test_clean_history_passes(repo: Path) -> None:
    _commit(repo, {"vault/topics/one.md": "one\n"}, "capture a topic", BRAIN)
    _commit(repo, {"src/brain.py": "code\n"}, "instance code", OWNER)

    assert verify_repo(repo) == []


def test_brain_commit_outside_the_vault_fails(repo: Path) -> None:
    _commit(repo, {"src/x.py": "x\n"}, "escape", BRAIN)

    offending = verify_repo(repo)

    assert len(offending) == 1
    short_sha, path = offending[0].split(" ")
    assert path == "src/x.py"
    assert len(short_sha) == 8


def test_commit_by_someone_else_outside_the_vault_passes(repo: Path) -> None:
    _commit(repo, {"src/x.py": "x\n"}, "operator edit", OWNER)

    assert verify_repo(repo) == []


def test_merge_commit_bringing_src_in_is_caught(repo: Path) -> None:
    _commit(repo, {"vault/topics/two.md": "two\n"}, "capture", OWNER)
    _git(repo, "checkout", "-q", "-b", "escape")
    _commit(repo, {"src/x.py": "x\n"}, "add src", BRAIN)
    _git(repo, "checkout", "-q", "main")
    _commit(repo, {"vault/topics/three.md": "three\n"}, "capture", OWNER)
    _git(repo, "-c", f"user.email={BRAIN}", "-c", "user.name=test", "merge", "-q", "--no-ff", "escape")

    offending = verify_repo(repo)

    # Only the main line is walked, so the side commit is not listed on its own.
    assert [line.split(" ", 1)[1] for line in offending] == ["src/x.py"]


def test_vault_merged_as_a_subtree_passes(repo: Path) -> None:
    vault = _vault_repo(repo.parent)
    _commit(repo, {"src/app.py": "app\n"}, "instance code", OWNER)
    _merge_vault(repo, vault, extra=None)

    assert verify_repo(repo) == []


def test_subtree_merge_that_also_brings_src_is_caught(repo: Path) -> None:
    vault = _vault_repo(repo.parent)
    _commit(repo, {"src/app.py": "app\n"}, "instance code", OWNER)
    _merge_vault(repo, vault, extra="src/evil.py")

    offending = verify_repo(repo)

    assert [line.split(" ", 1)[1] for line in offending] == ["src/evil.py"]


def test_custom_prefix(repo: Path) -> None:
    _commit(repo, {"notes/one.md": "one\n"}, "capture", BRAIN)

    assert verify_repo(repo, prefix="notes") == []
    assert len(verify_repo(repo, prefix="brain")) == 1


def test_not_a_repository_raises(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        verify_repo(tmp_path / "nowhere")


def test_main_prints_ok(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _commit(repo, {"vault/topics/one.md": "one\n"}, "capture", BRAIN)

    assert main(["verify-repo", str(repo)]) == 0
    assert capsys.readouterr().out.strip() == f"ok: no commit by {BRAIN} outside vault/"


def test_main_prints_each_offending_line(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _commit(repo, {"src/x.py": "x\n"}, "escape", BRAIN)

    assert main(["verify-repo", str(repo)]) == 1
    out = capsys.readouterr().out
    assert "src/x.py" in out
    assert out.strip().count("\n") == 0


def test_main_reports_a_missing_repository(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["verify-repo", str(tmp_path)]) == 2
    assert "not a git repository" in capsys.readouterr().err


def test_accented_and_emoji_names_inside_the_vault_pass(repo: Path) -> None:
    _commit(repo, {"vault/daily/2026-10-19-lunedì.md": "x", "vault/call/🚲 💃🏻.md": "y"}, "notes", BRAIN)
    assert verify_repo(repo) == []
