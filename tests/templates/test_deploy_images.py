"""The deploy images: what the entry point is called, and what each Dockerfile must keep.

These are static checks on the files, not a build: building the images needs the network and the
upstream Hermes image, so what is worth asserting here is that the constraints we care about
(the pinned av, the sealed venv, no password login) are still written down.
"""

from __future__ import annotations

import importlib
import tomllib
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
DEPLOY = ROOT / "templates" / "deploy"
GROUP = "hermes_agent.plugins"


def pyproject() -> dict[str, Any]:
    with (ROOT / "pyproject.toml").open("rb") as handle:
        return tomllib.load(handle)


def dockerfile(name: str) -> str:
    path = DEPLOY / name
    assert path.is_file(), f"{path} is missing"
    return path.read_text(encoding="utf-8")


def test_plugin_entry_point_is_declared() -> None:
    entry_points = pyproject()["project"]["entry-points"][GROUP]
    assert entry_points["brain-guard"] == "second_brain.guard.plugin:register"


def test_plugin_entry_point_resolves_to_a_callable() -> None:
    target = pyproject()["project"]["entry-points"][GROUP]["brain-guard"]
    module_name, _, attribute = target.partition(":")
    resolved = getattr(importlib.import_module(module_name), attribute)
    assert callable(resolved)


@pytest.mark.parametrize("name", ["Dockerfile.hermes", "Dockerfile.brain", "Dockerfile.terminal"])
def test_every_dockerfile_exists(name: str) -> None:
    assert dockerfile(name).strip()


def test_hermes_image_pins_the_hermes_version() -> None:
    text = dockerfile("Dockerfile.hermes")
    assert "FROM nousresearch/hermes-agent:v0.22.2" in text


def test_hermes_image_installs_into_the_sealed_venv() -> None:
    text = dockerfile("Dockerfile.hermes")
    assert "/opt/hermes/.venv/bin/pip install" in text
    for package in ("firecrawl-anydoc", "faster-whisper", "poppler-utils"):
        assert package in text


def test_hermes_image_pins_av_below_19() -> None:
    assert '"av==18.1.0"' in dockerfile("Dockerfile.hermes")


def test_hermes_image_points_the_model_cache_at_the_baked_model() -> None:
    text = dockerfile("Dockerfile.hermes")
    assert "HF_HOME=/opt/models/hf" in text
    assert "COPY models/ /opt/models/hf/" in text


def test_terminal_image_is_key_only() -> None:
    text = dockerfile("Dockerfile.terminal")
    assert "PasswordAuthentication no" in text
    assert "PermitRootLogin no" in text
    assert "Port 2222" in text


def test_brain_image_serves_the_tools_by_default() -> None:
    text = dockerfile("Dockerfile.brain")
    assert "FROM python:3.12-slim" in text
    assert 'CMD ["python", "-m", "second_brain.tools.server"]' in text
    assert "git" in text
    assert "USER brain" in text
