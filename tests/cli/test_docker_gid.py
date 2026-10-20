"""The values `sb init` derives for the templates, where the machine they run on is not enough.

`docker_gid` is the one that bites: `sb init` in a container has no socket to look at, and a
watchdog in the wrong group cannot reach Docker at all.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from second_brain.cli import config
from second_brain.cli.answers import Answers, AnswersError

SAMPLE = Path(__file__).with_name("answers.sample.yaml")


def _answers(tmp_path: Path, **changes: object) -> Answers:
    """The sample answers, with `docker_gid` removed unless the caller sets it."""
    raw = yaml.safe_load(SAMPLE.read_text(encoding="utf-8"))
    raw.pop("docker_gid", None)
    raw.update(changes)
    path = tmp_path / "answers.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    return Answers.from_yaml(path)


def test_the_answer_wins_over_the_socket(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "DOCKER_SOCKET", tmp_path / "no-socket-here")
    assert config.docker_gid(_answers(tmp_path, docker_gid=999)) == 999


def test_without_an_answer_the_socket_group_is_used(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    socket = tmp_path / "docker.sock"
    socket.touch()
    monkeypatch.setattr(config, "DOCKER_SOCKET", socket)
    assert config.docker_gid(_answers(tmp_path)) == socket.stat().st_gid


def test_without_an_answer_nor_a_socket_the_answers_are_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(config, "DOCKER_SOCKET", tmp_path / "no-socket-here")
    with pytest.raises(AnswersError) as refused:
        config.docker_gid(_answers(tmp_path))
    message = str(refused.value)
    assert "cannot find the docker socket here" in message
    assert "docker_gid" in message
    assert "stat -c %g /var/run/docker.sock" in message
