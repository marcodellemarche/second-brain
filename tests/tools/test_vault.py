"""Tests for the vault write path. Every fake value is built at runtime."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from second_brain.tools.vault import Provenance, Vault, VaultConfig, VaultError


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

    assert error.value.code == "git_failed"
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


def test_commit_human_changes_counts_the_paths_it_commits(root: Path) -> None:
    vault = _vault(root)
    (root / "one.md").write_text("one\n")
    (root / "two.md").write_text("two\n")

    sha = vault.commit_human_changes()

    assert sha == _git(root, "rev-parse", "HEAD")
    assert _message(root).splitlines() == ["human: 2 files", "", "Requested-By: human"]
    assert _git(root, "status", "--porcelain") == ""


def test_commit_human_changes_is_a_noop_on_a_clean_tree(root: Path) -> None:
    vault = _vault(root)
    head = _git(root, "rev-parse", "HEAD")

    assert vault.commit_human_changes() is None
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
    human = vault.commit_human_changes()
    assert human is not None

    with pytest.raises(VaultError) as error:
        vault.undo(human, _provenance())

    assert error.value.code == "zone_denied"
    assert "only commits authored by the brain" in error.value.message
    assert _git(root, "rev-parse", "HEAD") == human
    assert (root / "note.md").read_text() == "edited by hand\n"


def test_human_author_cannot_be_the_brain_author(root: Path) -> None:
    with pytest.raises(ValueError, match="human_email must differ"):
        VaultConfig(root=root, human_email="brain@localhost")


def test_human_author_is_configurable(root: Path) -> None:
    vault = _vault(root, human_name="owner", human_email="owner@localhost")
    (root / "note.md").write_text("edited by hand\n")

    vault.commit_human_changes()

    assert _git(root, "log", "-1", "--format=%an <%ae>") == "owner <owner@localhost>"
