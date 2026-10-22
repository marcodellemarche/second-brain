"""Who a team brain answers on Slack: one role per person, and colleagues only on a team brain."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from second_brain.cli.answers import Answers, AnswersError
from second_brain.cli.config import guard_config, template_values
from second_brain.cli.secrets import SEARXNG_KEY
from second_brain.tools.registry import Registry

SAMPLE = Path(__file__).parent / "answers.sample.yaml"
OWNER = "U0000000001"


def _answers(tmp_path: Path, slack: dict[str, object], shape: str = "team") -> Answers:
    raw = yaml.safe_load(SAMPLE.read_text(encoding="utf-8"))
    raw.pop("owner_telegram_id", None)
    raw["shape"] = shape
    raw["slack"] = {"owner_id": OWNER, "home_channel": "C0000000001", **slack}
    path = tmp_path / "answers.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    return Answers.from_yaml(path)


def test_members_and_approvers_are_read(tmp_path: Path) -> None:
    answers = _answers(tmp_path, {"members": ["U0000000002"], "approvers": ["U0000000003"]})
    assert answers.member_ids == ("U0000000002",)
    assert answers.approver_ids == ("U0000000003",)


@pytest.mark.parametrize(
    "slack",
    [
        {"members": ["U0000000002"], "approvers": ["U0000000002"]},
        {"approvers": [OWNER]},
        {"approvers": ["U0000000003", "U0000000003"]},
        {"approvers": ["not-an-id"]},
    ],
)
def test_one_role_per_person(tmp_path: Path, slack: dict[str, object]) -> None:
    with pytest.raises(AnswersError):
        _answers(tmp_path, slack)


def test_a_personal_brain_has_no_approvers(tmp_path: Path) -> None:
    with pytest.raises(AnswersError, match="team brain"):
        _answers(tmp_path, {"approvers": ["U0000000003"]}, shape="personal")


def test_an_approver_is_not_a_member_for_the_guard(tmp_path: Path) -> None:
    answers = _answers(tmp_path, {"members": ["U0000000002"], "approvers": ["U0000000003"]})
    guard = guard_config(answers, Registry())
    assert guard.members == ("U0000000002",)
    assert guard.admin_senders == (OWNER,)


def test_the_prompt_says_who_approves(tmp_path: Path) -> None:
    alone = template_values(_answers(tmp_path, {"members": ["U0000000002"]}), Registry(), {SEARXNG_KEY: "x"})
    assert "only one who can approve" in alone["who_approves"]
    shared = template_values(
        _answers(tmp_path, {"approvers": ["U0000000003"]}), Registry(), {SEARXNG_KEY: "x"}
    )
    assert "approvers listed in USER.md" in shared["who_approves"]
    assert shared["has_members"] is True
