"""The Hermes catalog overlay: labels of the approval card, without touching Hermes."""

from __future__ import annotations

import yaml

from second_brain.cli.locales import LOCALES_DIR, render_locale


def test_the_overlay_is_written_for_the_brain_language() -> None:
    files = render_locale("en")
    assert list(files) == [f"{LOCALES_DIR}/en.yaml"]
    catalog = yaml.safe_load(files[f"{LOCALES_DIR}/en.yaml"])
    approval = catalog["gateway"]["exec_approval"]
    assert approval["header"] == "Approval needed"
    assert approval["action_once"] == "Approve"
    # Every approval covers one call, so the other two buttons say they are the same thing.
    assert approval["action_session"] == approval["action_always"] == "Same as Approve"
    assert "{window}" in approval["deadline"]
    slack = catalog["platform"]["slack"]["approval"]
    assert slack["resolved_session"] == slack["resolved_always"] == slack["resolved_once"]
    assert "{user}" in slack["resolved_once"]


def test_an_italian_brain_gets_italian_labels_and_an_unknown_one_english() -> None:
    italian = yaml.safe_load(render_locale("it")[f"{LOCALES_DIR}/it.yaml"])
    assert italian["gateway"]["exec_approval"]["action_once"] == "Approva"
    assert list(render_locale("xx")) == [f"{LOCALES_DIR}/en.yaml"]
