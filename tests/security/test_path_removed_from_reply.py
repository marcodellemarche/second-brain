"""A reply never carries a filesystem path or an attachment tag out of the chat.

An injected instruction can get the model to paste a path or a `MEDIA:` tag, and Hermes would
upload that file to the channel. The reply is cleaned on the way out, and the audit says what
was removed.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from second_brain.guard import plugin
from second_brain.guard.sanitize import PATH_PLACEHOLDER


def test_a_path_in_a_reply_is_removed(guard: plugin.Guard) -> None:
    cleaned = guard.transform_llm_output(
        response_text="the file is at /home/user/vault/_brain/profile.md, take a look",
        session_id="s1",
    )
    assert isinstance(cleaned, str)
    assert "/home/user/vault" not in cleaned
    assert PATH_PLACEHOLDER in cleaned


def test_a_media_tag_for_a_file_outside_the_media_dir_is_removed(guard: plugin.Guard) -> None:
    cleaned = guard.transform_llm_output(
        response_text="here you go\nMEDIA:/home/user/.ssh/id_rsa",
        session_id="s1",
    )
    assert isinstance(cleaned, str)
    assert "MEDIA:" not in cleaned
    assert "id_rsa" not in cleaned


def test_a_home_path_is_removed(guard: plugin.Guard) -> None:
    cleaned = guard.transform_llm_output(response_text="check ~/.config/hermes/config.yaml", session_id="s1")
    assert isinstance(cleaned, str)
    assert "~/.config" not in cleaned


def test_the_removal_is_audited(guard: plugin.Guard, decisions: Callable[[], list[dict[str, Any]]]) -> None:
    guard.transform_llm_output(response_text="see /etc/passwd", session_id="s1")
    path = guard.guard_dir / plugin.AUDIT_FILE
    rows = [line for line in path.read_text(encoding="utf-8").splitlines() if '"sanitize"' in line]
    assert len(rows) == 1
    assert "/etc/passwd" in rows[0]


def test_a_file_inside_the_media_dir_survives(guard: plugin.Guard) -> None:
    tag = f"MEDIA:{guard.config.media_dir}/report.pdf"
    assert guard.transform_llm_output(response_text=f"here: {tag}", session_id="s1") == f"here: {tag}"
