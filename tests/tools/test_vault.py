"""Tests for the vault write path. Every fake value is built at runtime."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from second_brain.tools.vault import HumanCommit, Provenance, Vault, VaultConfig, VaultError


def _provenance(**overrides: object) -> Provenance:
    values: dict[str, object] = {
        "requested_by": "user-1",
        "reason": "test write",
        "source_integrity": "trusted",
        "session": "session-1",
    }
    values.update(overrides)
    return Provenance(**values)  # type: ignore[arg-type]


@pytest.fixture
def root(tmp_path: Path) -> Path:
    repo = tmp_path / "vault"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "tester")
    _git(repo, "config", "user.email", "tester@localhost")
    _git(repo, "commit", "-q", "--allow-empty", "-m", "init")
    return repo


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, check=True)
    return result.stdout.strip()


def _brain_commit(root: Path, message: str) -> None:
    _git(
        root,
        "-c",
        "user.name=brain",
        "-c",
        "user.email=brain@localhost",
        "-c",
        "commit.gpgsign=false",
        "commit",
        "-q",
        "-m",
        message,
    )


def _vault(root: Path, **overrides: object) -> Vault:
    return Vault(VaultConfig(root=root, **overrides))  # type: ignore[arg-type]


def _message(root: Path, commit: str = "HEAD") -> str:
    return _git(root, "log", "-1", "--format=%B", commit)


def test_write_creates_file_and_commit_with_all_trailers(root: Path) -> None:
    vault = _vault(root)

    sha = vault.write("topics/note.md", "hello\n", _provenance())

    assert (root / "topics/note.md").read_text() == "hello\n"
    assert sha == _git(root, "rev-parse", "HEAD")
    message = _message(root)
    assert message.splitlines()[0] == "write: topics/note.md"
    assert "Requested-By: user-1" in message
    assert "Reason: test write" in message
    assert "Source-Integrity: trusted" in message
    assert "Session: session-1" in message
    assert "Approval: none" in message
    assert _git(root, "status", "--porcelain") == ""


def test_write_keeps_trailer_values_on_one_line(root: Path) -> None:
    vault = _vault(root)

    vault.write("note.md", "hello\n", _provenance(reason="line one\nRequested-By: attacker"))

    trailers = _git(root, "log", "-1", "--format=%(trailers:only,unfold=true)").splitlines()
    assert [line.split(":", 1)[0] for line in trailers] == [
        "Requested-By",
        "Reason",
        "Source-Integrity",
        "Session",
        "Approval",
    ]
    assert trailers[1] == "Reason: line one Requested-By: attacker"


def test_commit_uses_the_configured_author(root: Path) -> None:
    vault = _vault(root)

    vault.write("note.md", "hello\n", _provenance())

    assert _git(root, "log", "-1", "--format=%an <%ae>") == "brain <brain@localhost>"


def test_write_returns_sha256_of_the_content(root: Path) -> None:
    vault = _vault(root)
    vault.write("note.md", "hello\n", _provenance())

    content, digest = vault.read("note.md")

    assert content == "hello\n"
    assert digest == "5891b5b522d5df086d0ff0b110fbd9d21bb4fc7163af34d08286a2e846f6be03"


def test_read_of_a_missing_file_is_not_found(root: Path) -> None:
    with pytest.raises(VaultError) as error:
        _vault(root).read("nope.md")

    assert error.value.code == "not_found"


@pytest.mark.parametrize("path", ["../escape.md", "topics/../../escape.md", "/etc/passwd", ""])
def test_paths_outside_the_vault_are_rejected(root: Path, path: str) -> None:
    with pytest.raises(VaultError) as error:
        _vault(root).write(path, "x\n", _provenance())

    assert error.value.code == "outside_vault"
    assert not (root.parent / "escape.md").exists()


def test_symlink_pointing_outside_is_rejected(root: Path, tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "link").symlink_to(outside, target_is_directory=True)

    with pytest.raises(VaultError) as error:
        _vault(root).write("link/note.md", "x\n", _provenance())

    assert error.value.code == "outside_vault"
    assert not (outside / "note.md").exists()


def test_symlinked_file_outside_is_rejected(root: Path, tmp_path: Path) -> None:
    target = tmp_path / "secret.md"
    target.write_text("outside\n")
    (root / "note.md").symlink_to(target)

    with pytest.raises(VaultError) as error:
        _vault(root).write("note.md", "x\n", _provenance())

    assert error.value.code == "outside_vault"
    assert target.read_text() == "outside\n"


def test_denied_prefix_is_rejected(root: Path) -> None:
    vault = _vault(root)

    with pytest.raises(VaultError) as error:
        vault.write("_brain/policy.md", "x\n", _provenance())

    assert error.value.code == "zone_denied"
    assert not (root / "_brain/policy.md").exists()


def test_denied_prefix_covers_its_children(root: Path) -> None:
    with pytest.raises(VaultError) as error:
        _vault(root).write(".git/hooks/pre-commit", "x\n", _provenance())

    assert error.value.code == "zone_denied"


def test_approval_required_prefix_needs_an_approval(root: Path) -> None:
    vault = _vault(root, approval_required=["documents"])

    with pytest.raises(VaultError) as error:
        vault.write("documents/passport.md", "x\n", _provenance())

    assert error.value.code == "zone_denied"
    assert not (root / "documents/passport.md").exists()

    sha = vault.write("documents/passport.md", "x\n", _provenance(approval="user-1:ok"))
    assert sha
    assert "Approval: user-1:ok" in _message(root)


def test_quiet_window_blocks_a_fresh_write(root: Path) -> None:
    vault = _vault(root, quiet_seconds=3600)
    vault.write("note.md", "first\n", _provenance())

    with pytest.raises(VaultError) as error:
        vault.write("note.md", "second\n", _provenance())

    assert error.value.code == "quiet_window"
    assert (root / "note.md").read_text() == "first\n"


def test_quiet_window_lets_an_old_file_be_rewritten(root: Path) -> None:
    vault = _vault(root, quiet_seconds=3600)
    vault.write("note.md", "first\n", _provenance())
    old = 1_000_000_000
    os.utime(root / "note.md", (old, old))

    vault.write("note.md", "second\n", _provenance())

    assert (root / "note.md").read_text() == "second\n"


def test_expected_sha256_bypasses_the_quiet_window(root: Path) -> None:
    vault = _vault(root, quiet_seconds=3600)
    vault.write("note.md", "first\n", _provenance())
    _, digest = vault.read("note.md")

    vault.write("note.md", "second\n", _provenance(), expected_sha256=digest)

    assert (root / "note.md").read_text() == "second\n"


def test_stale_expected_sha256_is_a_hash_mismatch(root: Path) -> None:
    vault = _vault(root)
    vault.write("note.md", "first\n", _provenance())

    with pytest.raises(VaultError) as error:
        vault.write("note.md", "second\n", _provenance(), expected_sha256="0" * 64)

    assert error.value.code == "hash_mismatch"
    assert (root / "note.md").read_text() == "first\n"


def test_secret_in_content_is_rejected_and_nothing_is_written(root: Path) -> None:
    vault = _vault(root)

    with pytest.raises(VaultError) as error:
        vault.write("note.md", "key: " + "AKIA" + "Q" * 16 + "\n", _provenance())

    assert error.value.code == "secret_detected"
    assert "aws_access_key_id" in error.value.message
    assert "line 1" in error.value.message
    assert "AKIA" not in error.value.message
    assert not (root / "note.md").exists()
    assert _git(root, "rev-parse", "HEAD") == _git(root, "rev-parse", "HEAD~0")
    assert _git(root, "status", "--porcelain") == ""


def test_move_renames_and_commits(root: Path) -> None:
    vault = _vault(root)
    vault.write("inbox/note.md", "hello\n", _provenance())

    sha = vault.move("inbox/note.md", "topics/note.md", _provenance())

    assert not (root / "inbox/note.md").exists()
    assert (root / "topics/note.md").read_text() == "hello\n"
    assert sha == _git(root, "rev-parse", "HEAD")
    assert _message(root).splitlines()[0] == "move: inbox/note.md -> topics/note.md"
    assert _git(root, "status", "--porcelain") == ""
    assert _git(root, "log", "--oneline").count("\n") == 2


def test_move_to_an_existing_file_is_rejected(root: Path) -> None:
    vault = _vault(root)
    vault.write("one.md", "one\n", _provenance())
    vault.write("two.md", "two\n", _provenance())

    with pytest.raises(VaultError) as error:
        vault.move("one.md", "two.md", _provenance())

    assert error.value.code == "exists"
    assert (root / "two.md").read_text() == "two\n"


def test_move_of_a_missing_file_is_not_found(root: Path) -> None:
    with pytest.raises(VaultError) as error:
        _vault(root).move("nope.md", "other.md", _provenance())

    assert error.value.code == "not_found"


def test_move_checks_both_paths(root: Path) -> None:
    vault = _vault(root)
    vault.write("note.md", "hello\n", _provenance())

    with pytest.raises(VaultError) as error:
        vault.move("note.md", "_brain/policy.md", _provenance())

    assert error.value.code == "zone_denied"
    assert (root / "note.md").exists()


def test_delete_moves_the_file_to_trash_and_keeps_history(root: Path) -> None:
    vault = _vault(root)
    vault.write("topics/note.md", "hello\n", _provenance())

    sha = vault.delete("topics/note.md", _provenance())

    assert not (root / "topics/note.md").exists()
    trashed = list((root / ".trash").rglob("note.md.*"))
    assert len(trashed) == 1
    assert trashed[0].read_text() == "hello\n"
    assert sha == _git(root, "rev-parse", "HEAD")
    assert _message(root).splitlines()[0] == "delete: topics/note.md"
    assert _git(root, "status", "--porcelain") == ""
    assert "topics/note.md" in _git(root, "show", "--name-only", "--format=", "HEAD")


def test_delete_of_a_missing_file_is_not_found(root: Path) -> None:
    with pytest.raises(VaultError) as error:
        _vault(root).delete("nope.md", _provenance())

    assert error.value.code == "not_found"


def test_delete_works_when_trash_is_gitignored(root: Path) -> None:
    (root / ".gitignore").write_text(".trash/\n")
    _git(root, "add", ".gitignore")
    _git(root, "commit", "-q", "-m", "ignore trash")
    vault = _vault(root)
    vault.write("note.md", "hello\n", _provenance())

    vault.delete("note.md", _provenance())

    assert not (root / "note.md").exists()
    assert len(list((root / ".trash").rglob("note.md.*"))) == 1
    assert _git(root, "status", "--porcelain") == ""


def test_undo_restores_the_previous_content(root: Path) -> None:
    vault = _vault(root, quiet_seconds=0)
    vault.write("note.md", "one\n", _provenance())
    second = vault.write("note.md", "two\n", _provenance())

    sha = vault.undo(second, _provenance(reason="wrong write"))

    assert (root / "note.md").read_text() == "one\n"
    assert sha == _git(root, "rev-parse", "HEAD")
    message = _message(root)
    assert message.splitlines()[0] == 'Revert "write: note.md"'
    assert "Reason: wrong write" in message
    assert "Requested-By: user-1" in message
    assert _git(root, "status", "--porcelain") == ""


def test_undo_of_an_unknown_commit_is_not_found(root: Path) -> None:
    with pytest.raises(VaultError) as error:
        _vault(root).undo("0" * 40, _provenance())

    assert error.value.code == "not_found"


def test_undo_of_a_conflicting_commit_leaves_the_tree_clean(root: Path) -> None:
    vault = _vault(root, quiet_seconds=0)
    vault.write("note.md", "one\n", _provenance())
    first = vault.write("note.md", "two\n", _provenance())
    vault.write("note.md", "three\n", _provenance())

    with pytest.raises(VaultError) as error:
        vault.undo(first, _provenance())

    assert error.value.code == "conflict"
    assert not (root / ".git/REVERT_HEAD").exists()
    assert _git(root, "status", "--porcelain") == ""
    assert (root / "note.md").read_text() == "three\n"


def test_undo_of_a_commit_by_another_author_is_rejected(root: Path) -> None:
    (root / "note.md").write_text("human\n")
    _git(root, "add", "note.md")
    _git(root, "commit", "-q", "-m", "human edit")
    human = _git(root, "rev-parse", "HEAD")

    with pytest.raises(VaultError) as error:
        _vault(root).undo(human, _provenance())

    assert error.value.code == "zone_denied"
    assert "only commits authored by the brain" in error.value.message
    assert _git(root, "rev-parse", "HEAD") == human
    assert (root / "note.md").read_text() == "human\n"
    assert _git(root, "status", "--porcelain") == ""


def test_undo_of_an_approval_required_path_needs_an_approval(root: Path) -> None:
    vault = _vault(root, quiet_seconds=0, approval_required=["documents"])
    vault.write("documents/passport.md", "one\n", _provenance(approval="user-1:ok"))
    second = vault.write("documents/passport.md", "two\n", _provenance(approval="user-1:ok"))

    with pytest.raises(VaultError) as error:
        vault.undo(second, _provenance())

    assert error.value.code == "zone_denied"
    assert "needs an explicit approval" in error.value.message
    assert (root / "documents/passport.md").read_text() == "two\n"
    assert _git(root, "rev-parse", "HEAD") == second

    vault.undo(second, _provenance(approval="user-1:ok"))

    assert (root / "documents/passport.md").read_text() == "one\n"
    assert _git(root, "status", "--porcelain") == ""


def test_undo_of_a_commit_touching_a_denied_path_is_rejected(root: Path) -> None:
    (root / "_brain").mkdir()
    (root / "_brain/policy.md").write_text("policy\n")
    _git(root, "add", "-A")
    _brain_commit(root, "sneak a policy change")
    denied = _git(root, "rev-parse", "HEAD")

    with pytest.raises(VaultError) as error:
        _vault(root).undo(denied, _provenance())

    assert error.value.code == "zone_denied"
    assert "_brain/policy.md" in error.value.message
    assert _git(root, "rev-parse", "HEAD") == denied
    assert (root / "_brain/policy.md").read_text() == "policy\n"
    assert _git(root, "status", "--porcelain") == ""


def test_undo_of_a_commit_moving_a_file_into_a_denied_path_is_rejected(root: Path) -> None:
    (root / "note.md").write_text("hello\n")
    _git(root, "add", "note.md")
    _brain_commit(root, "add a note")
    (root / "_brain").mkdir()
    _git(root, "mv", "note.md", "_brain/policy.md")
    _brain_commit(root, "move it into the policy zone")
    moved = _git(root, "rev-parse", "HEAD")

    with pytest.raises(VaultError) as error:
        _vault(root).undo(moved, _provenance())

    assert error.value.code == "zone_denied"
    assert _git(root, "rev-parse", "HEAD") == moved
    assert (root / "_brain/policy.md").read_text() == "hello\n"


def test_undo_of_a_merge_commit_is_rejected(root: Path) -> None:
    vault = _vault(root, quiet_seconds=0)
    vault.write("note.md", "one\n", _provenance())
    _git(root, "checkout", "-q", "-b", "side")
    vault.write("other.md", "two\n", _provenance())
    _git(root, "checkout", "-q", "-")
    vault.write("third.md", "three\n", _provenance())
    _git(
        root,
        "-c",
        "user.name=brain",
        "-c",
        "user.email=brain@localhost",
        "-c",
        "commit.gpgsign=false",
        "merge",
        "--no-ff",
        "-q",
        "-m",
        "merge side",
        "side",
    )
    merge = _git(root, "rev-parse", "HEAD")

    with pytest.raises(VaultError) as error:
        vault.undo(merge, _provenance())

    assert error.value.code == "zone_denied"
    assert "merge commit" in error.value.message
    assert _git(root, "rev-parse", "HEAD") == merge
    assert _git(root, "status", "--porcelain") == ""


def test_atomic_write_leaves_no_temp_files(root: Path) -> None:
    vault = _vault(root)

    vault.write("topics/note.md", "hello\n", _provenance())

    assert list(root.rglob(".tmp-*")) == []
    assert sorted(path.name for path in (root / "topics").iterdir()) == ["note.md"]


def test_a_refused_write_leaves_no_temp_files(root: Path) -> None:
    vault = _vault(root, quiet_seconds=3600)
    vault.write("note.md", "first\n", _provenance())

    with pytest.raises(VaultError):
        vault.write("note.md", "second\n", _provenance())

    assert list(root.rglob(".tmp-*")) == []


def test_a_human_edit_before_a_brain_write_is_its_own_commit(root: Path) -> None:
    vault = _vault(root)
    (root / "topics").mkdir()
    (root / "topics/human.md").write_text("edited by hand\n")

    sha = vault.write("topics/brain.md", "written by the brain\n", _provenance())

    human, brain = _git(root, "rev-parse", "HEAD~1"), _git(root, "rev-parse", "HEAD")
    assert sha == brain
    assert _git(root, "log", "-1", "--format=%s", human) == "human: 1 files"
    assert "Requested-By: human" in _message(root, human)
    assert _git(root, "log", "-1", "--format=%an <%ae>", human) == "human <human@localhost>"
    assert _git(root, "show", "--name-only", "--format=", human).split() == ["topics/human.md"]
    assert _git(root, "show", "--name-only", "--format=", brain).split() == ["topics/brain.md"]
    assert _git(root, "status", "--porcelain") == ""


def test_a_human_edit_before_a_move_is_its_own_commit(root: Path) -> None:
    vault = _vault(root)
    vault.write("inbox/note.md", "hello\n", _provenance())
    (root / "human.md").write_text("edited by hand\n")

    vault.move("inbox/note.md", "topics/note.md", _provenance())

    assert _git(root, "log", "-1", "--format=%s", "HEAD~1") == "human: 1 files"
    brain_paths = _git(root, "show", "--name-only", "--no-renames", "--format=", "HEAD").split()
    assert brain_paths == ["inbox/note.md", "topics/note.md"]
    assert _git(root, "status", "--porcelain") == ""


def test_a_human_edit_before_a_delete_is_its_own_commit(root: Path) -> None:
    vault = _vault(root)
    vault.write("note.md", "hello\n", _provenance())
    (root / "human.md").write_text("edited by hand\n")

    vault.delete("note.md", _provenance())

    assert _git(root, "log", "-1", "--format=%s", "HEAD~1") == "human: 1 files"
    assert "human.md" not in _git(root, "show", "--name-only", "--format=", "HEAD")
    assert _git(root, "status", "--porcelain") == ""


def test_a_human_edit_before_an_undo_is_its_own_commit(root: Path) -> None:
    vault = _vault(root, quiet_seconds=0)
    vault.write("note.md", "one\n", _provenance())
    second = vault.write("note.md", "two\n", _provenance())
    (root / "human.md").write_text("edited by hand\n")

    vault.undo(second, _provenance())

    assert _git(root, "log", "-1", "--format=%s", "HEAD~1") == "human: 1 files"
    assert "human.md" not in _git(root, "show", "--name-only", "--format=", "HEAD")
    assert (root / "note.md").read_text() == "one\n"
    assert _git(root, "status", "--porcelain") == ""


def _fake_aws_key() -> str:
    return "AKIA" + "Q" * 16


def test_commit_human_changes_counts_the_paths_it_commits(root: Path) -> None:
    vault = _vault(root)
    (root / "one.md").write_text("one\n")
    (root / "two.md").write_text("two\n")

    result = vault.commit_human_changes()

    assert result == HumanCommit(sha=_git(root, "rev-parse", "HEAD"), held_back=[])
    assert _message(root).splitlines() == ["human: 2 files", "", "Requested-By: human"]
    assert _git(root, "status", "--porcelain") == ""


def test_commit_human_changes_is_a_noop_on_a_clean_tree(root: Path) -> None:
    vault = _vault(root)
    head = _git(root, "rev-parse", "HEAD")

    assert vault.commit_human_changes() == HumanCommit(sha=None, held_back=[])
    assert _git(root, "rev-parse", "HEAD") == head


def test_commit_human_changes_keeps_the_trash_out_when_it_is_gitignored(root: Path) -> None:
    (root / ".gitignore").write_text(".trash/\n")
    _git(root, "add", ".gitignore")
    _git(root, "commit", "-q", "-m", "ignore trash")
    vault = _vault(root)
    (root / ".trash").mkdir()
    (root / ".trash/gone.md").write_text("deleted\n")
    (root / "note.md").write_text("hello\n")

    vault.commit_human_changes()

    assert _git(root, "show", "--name-only", "--format=", "HEAD").split() == ["note.md"]
    assert _git(root, "status", "--porcelain") == ""


def test_undo_of_a_human_commit_is_refused(root: Path) -> None:
    vault = _vault(root)
    (root / "note.md").write_text("edited by hand\n")
    human = vault.commit_human_changes().sha
    assert human is not None

    with pytest.raises(VaultError) as error:
        vault.undo(human, _provenance())

    assert error.value.code == "zone_denied"
    assert "only commits authored by the brain" in error.value.message
    assert _git(root, "rev-parse", "HEAD") == human
    assert (root / "note.md").read_text() == "edited by hand\n"


def test_a_note_with_a_secret_is_held_back_and_never_committed(root: Path) -> None:
    vault = _vault(root)
    head = _git(root, "rev-parse", "HEAD")
    (root / "note.md").write_text("my key\n\n" + _fake_aws_key() + "\n")

    result = vault.commit_human_changes()

    assert result.sha is None
    assert [entry.path for entry in result.held_back] == ["note.md"]
    assert _git(root, "rev-parse", "HEAD") == head
    assert _git(root, "status", "--porcelain") == "?? note.md"
    assert _git(root, "log", "--all", "--format=%H", "-S", _fake_aws_key()) == ""


def test_the_held_back_entry_reports_the_rule_and_the_line(root: Path) -> None:
    vault = _vault(root)
    (root / "note.md").write_text("first line\nsecond line\n" + _fake_aws_key() + "\n")

    held = vault.held_back()

    assert len(held) == 1
    assert held[0].path == "note.md"
    assert held[0].rules == (("aws_access_key_id", 3),)
    assert _fake_aws_key() not in held[0].describe()
    assert "aws_access_key_id at line 3" in held[0].describe()


def test_the_files_without_a_secret_in_the_same_tick_are_committed(root: Path) -> None:
    vault = _vault(root)
    (root / "note.md").write_text("my key\n" + _fake_aws_key() + "\n")
    (root / "clean.md").write_text("nothing to see\n")

    result = vault.commit_human_changes()

    assert result.sha is not None
    assert [entry.path for entry in result.held_back] == ["note.md"]
    assert _git(root, "show", "--name-only", "--format=", result.sha).split() == ["clean.md"]
    assert _git(root, "status", "--porcelain") == "?? note.md"
    assert _git(root, "log", "--all", "--format=%H", "-S", _fake_aws_key()) == ""


def test_the_next_tick_commits_the_note_once_the_secret_is_gone(root: Path) -> None:
    vault = _vault(root)
    (root / "note.md").write_text("my key\n" + _fake_aws_key() + "\n")
    assert vault.commit_human_changes().sha is None

    (root / "note.md").write_text("my key\n")
    result = vault.commit_human_changes()

    assert result.sha is not None
    assert result.held_back == []
    assert vault.held_back() == []
    assert _git(root, "show", "--name-only", "--format=", result.sha).split() == ["note.md"]
    assert _git(root, "status", "--porcelain") == ""


def test_a_secret_added_to_a_committed_file_is_held_back_and_unstaged(root: Path) -> None:
    vault = _vault(root)
    vault.write("note.md", "hello\n", _provenance())
    (root / "note.md").write_text("hello\n" + _fake_aws_key() + "\n")
    _git(root, "add", "note.md")

    result = vault.commit_human_changes()

    assert result.sha is None
    assert [entry.path for entry in result.held_back] == ["note.md"]
    assert _git(root, "diff", "--cached", "--name-only") == ""
    assert _git(root, "diff", "--name-only") == "note.md"
    assert _git(root, "log", "--all", "--format=%H", "-S", _fake_aws_key()) == ""


def test_a_binary_file_is_not_scanned_and_gets_committed(root: Path) -> None:
    vault = _vault(root)
    (root / "drawing.bin").write_bytes(b"\x00\x01" + _fake_aws_key().encode())

    result = vault.commit_human_changes()

    assert result.sha is not None
    assert result.held_back == []
    assert _git(root, "show", "--name-only", "--format=", result.sha).split() == ["drawing.bin"]


def test_a_half_staged_rename_of_a_held_back_file_is_unstaged(root: Path) -> None:
    vault = _vault(root)
    vault.write("note.md", "hello\n", _provenance())
    head = _git(root, "rev-parse", "HEAD")
    _git(root, "mv", "note.md", "renamed.md")
    (root / "renamed.md").write_text(_fake_aws_key() + "\n")

    result = vault.commit_human_changes()

    assert result.sha is None
    assert [entry.path for entry in result.held_back] == ["renamed.md"]
    assert _git(root, "diff", "--cached", "--name-only") == ""
    assert _git(root, "rev-parse", "HEAD") == head
    assert _git(root, "log", "--all", "--format=%H", "-S", _fake_aws_key()) == ""


def test_undo_does_not_write_over_a_held_back_path(root: Path) -> None:
    vault = _vault(root, quiet_seconds=0)
    written = vault.write("note.md", "one\n", _provenance())
    (root / "note.md").write_text("one\n" + _fake_aws_key() + "\n")

    with pytest.raises(VaultError) as error:
        vault.undo(written, _provenance())

    assert error.value.code == "secret_detected"
    assert _git(root, "rev-parse", "HEAD") == written
    assert _fake_aws_key() in (root / "note.md").read_text()


def test_a_brain_write_to_a_held_back_path_is_refused(root: Path) -> None:
    vault = _vault(root)
    (root / "note.md").write_text("my key\n" + _fake_aws_key() + "\n")

    with pytest.raises(VaultError) as error:
        vault.write("note.md", "clean now\n", _provenance())

    assert error.value.code == "secret_detected"
    assert "aws_access_key_id at line 2" in error.value.message
    assert _fake_aws_key() not in error.value.message
    assert _fake_aws_key() in (root / "note.md").read_text()
    assert _git(root, "status", "--porcelain") == "?? note.md"


def test_a_brain_move_or_delete_of_a_held_back_path_is_refused(root: Path) -> None:
    vault = _vault(root)
    (root / "note.md").write_text(_fake_aws_key() + "\n")

    with pytest.raises(VaultError) as error:
        vault.move("note.md", "topics/note.md", _provenance())
    assert error.value.code == "secret_detected"

    with pytest.raises(VaultError) as error:
        vault.delete("note.md", _provenance())
    assert error.value.code == "secret_detected"
    assert (root / "note.md").exists()


def test_a_held_back_file_does_not_block_a_brain_write_to_another_path(root: Path) -> None:
    vault = _vault(root)
    (root / "note.md").write_text("my key\n" + _fake_aws_key() + "\n")

    sha = vault.write("topics/other.md", "written by the brain\n", _provenance())

    assert sha == _git(root, "rev-parse", "HEAD")
    assert _git(root, "show", "--name-only", "--format=", sha).split() == ["topics/other.md"]
    assert _git(root, "status", "--porcelain") == "?? note.md"
    assert [entry.path for entry in vault.held_back()] == ["note.md"]
    assert _git(root, "log", "--all", "--format=%H", "-S", _fake_aws_key()) == ""


def test_a_human_commit_with_only_a_held_back_file_leaves_the_tree_dirty(root: Path) -> None:
    vault = _vault(root)
    (root / "note.md").write_text(_fake_aws_key() + "\n")
    head = _git(root, "rev-parse", "HEAD")

    result = vault.commit_human_changes()

    assert result.sha is None
    assert _git(root, "rev-parse", "HEAD") == head
    assert vault.held_back() == [result.held_back[0]]


def test_human_author_cannot_be_the_brain_author(root: Path) -> None:
    with pytest.raises(ValueError, match="human_email must differ"):
        VaultConfig(root=root, human_email="brain@localhost")


def test_human_author_is_configurable(root: Path) -> None:
    vault = _vault(root, human_name="owner", human_email="owner@localhost")
    (root / "note.md").write_text("edited by hand\n")

    vault.commit_human_changes()

    assert _git(root, "log", "-1", "--format=%an <%ae>") == "owner <owner@localhost>"


# --- log -----------------------------------------------------------------------------------


def test_log_lists_brain_and_human_commits_newest_first(root: Path) -> None:
    vault = _vault(root)
    vault.write("topics/note.md", "brain wrote this\n", _provenance())
    (root / "inbox").mkdir()
    (root / "inbox/from-editor.md").write_text("the owner wrote this\n")
    vault.commit_human_changes()

    entries = vault.log()

    assert [entry["subject"] for entry in entries[:2]] == ["human: 1 files", "write: topics/note.md"]
    assert [entry["author"] for entry in entries[:2]] == ["human", "brain"]
    assert entries[0]["commit"] == _git(root, "rev-parse", "HEAD")
    assert entries[0]["short"] == _git(root, "rev-parse", "--short", "HEAD")
    assert entries[1]["paths"] == ["topics/note.md"]


def test_log_keeps_the_author_name_it_does_not_know(root: Path) -> None:
    (root / "note.md").write_text("written by hand\n")
    _git(root, "add", "-A")
    _git(root, "-c", "user.name=someone", "-c", "user.email=someone@localhost", "commit", "-q", "-m", "else")

    assert _vault(root).log()[0]["author"] == "someone"


def test_log_parses_the_trailers_of_a_brain_write(root: Path) -> None:
    vault = _vault(root)
    vault.write("topics/note.md", "hello\n", _provenance(requested_by="job:nightly", reason="nightly ingest"))

    entry = vault.log()[0]

    assert entry["trailers"] == {
        "Requested-By": "job:nightly",
        "Reason": "nightly ingest",
        "Source-Integrity": "trusted",
        "Session": "session-1",
        "Approval": "none",
    }
    assert entry["date"].startswith("20")


def _commit_at(root: Path, when: str, path: str, content: str) -> None:
    (root / path).parent.mkdir(parents=True, exist_ok=True)
    (root / path).write_text(content)
    _git(root, "add", "-A")
    env = {**os.environ, "GIT_AUTHOR_DATE": when, "GIT_COMMITTER_DATE": when}
    subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "-c",
            "user.name=brain",
            "-c",
            "user.email=brain@localhost",
            "commit",
            "-q",
            "-m",
            f"write: {path}",
        ],
        capture_output=True,
        check=True,
        env=env,
    )


def test_log_since_filters_older_commits(root: Path) -> None:
    _commit_at(root, "2020-01-01T09:00:00+00:00", "topics/old.md", "old\n")
    _commit_at(root, "2024-06-01T09:00:00+00:00", "topics/new.md", "new\n")

    recent = _vault(root).log(since="2023-01-01T00:00:00+00:00")
    touched = [path for entry in recent for path in entry["paths"]]

    assert "topics/new.md" in touched
    assert "topics/old.md" not in touched
    assert _vault(root).log(since="2099-01-01T00:00:00+00:00") == []


def test_log_lists_a_path_with_non_ascii_characters_as_is(root: Path) -> None:
    vault = _vault(root)
    vault.write("topics/caff\u00e8 \u00e8.md", "hello\n", _provenance())

    assert vault.log()[0]["paths"] == ["topics/caff\u00e8 \u00e8.md"]


def test_log_limit_caps_the_entries(root: Path) -> None:
    vault = _vault(root)
    for index in range(3):
        vault.write(f"topics/note-{index}.md", "hello\n", _provenance())

    assert len(vault.log(limit=2)) == 2
    assert vault.log(limit=2)[0]["paths"] == ["topics/note-2.md"]


@pytest.mark.parametrize("since", ["yesterday", "2026-13-45", "not a date"])
def test_log_refuses_a_since_it_cannot_parse(root: Path, since: str) -> None:
    with pytest.raises(VaultError) as error:
        _vault(root).log(since=since)

    assert error.value.code == "bad_since"


@pytest.mark.parametrize("limit", [0, 201])
def test_log_refuses_a_limit_out_of_range(root: Path, limit: int) -> None:
    with pytest.raises(VaultError) as error:
        _vault(root).log(limit=limit)

    assert error.value.code == "bad_limit"


def test_log_of_an_empty_repository_is_empty(tmp_path: Path) -> None:
    repo = tmp_path / "empty"
    repo.mkdir()
    _git(repo, "init", "-q")

    assert _vault(repo).log() == []


def test_a_new_raw_note_can_be_written_but_never_rewritten_moved_or_deleted(root: Path) -> None:
    vault = _vault(root, quiet_seconds=0)
    vault.write("raw/web/result.md", "---\ntrust: untrusted\n---\nwords\n", _provenance())
    _content, sha = vault.read("raw/web/result.md")
    for attempt in (
        lambda: vault.write("raw/web/result.md", "changed\n", _provenance(), expected_sha256=sha),
        lambda: vault.move("raw/web/result.md", "knowledge/result.md", _provenance()),
        lambda: vault.delete("raw/web/result.md", _provenance()),
    ):
        with pytest.raises(VaultError) as error:
            attempt()
        assert error.value.code == "immutable"
    assert (root / "raw/web/result.md").read_text() == "---\ntrust: untrusted\n---\nwords\n"


def test_moving_a_note_into_raw_is_not_blocked_by_immutability(root: Path) -> None:
    vault = _vault(root, quiet_seconds=0)
    vault.write("inbox/forward.md", "forwarded\n", _provenance())
    vault.move("inbox/forward.md", "raw/mail/forward.md", _provenance())
    assert (root / "raw/mail/forward.md").is_file()
