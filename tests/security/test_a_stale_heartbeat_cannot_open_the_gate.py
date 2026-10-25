"""A heartbeat left by the previous Hermes run must not keep the model gate open.

The watchdog cannot tell runs apart, so the Hermes entrypoint deletes the heartbeat before Hermes
starts: a run whose guard never loads then has no heartbeat, and the watchdog says no.
"""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

from second_brain.gate.watchdog import Watchdog

ENTRYPOINT = Path(__file__).resolve().parents[2] / "templates/deploy/hermes-entrypoint.sh"
UPSTREAM = "/opt/hermes/docker/entrypoint-dispatch.sh"


def _run_entrypoint(tmp_path: Path, heartbeat: Path) -> subprocess.CompletedProcess[str]:
    upstream = tmp_path / "upstream.sh"
    upstream.write_text('#!/bin/sh\necho "upstream $*"\n', encoding="utf-8")
    upstream.chmod(0o755)
    script = tmp_path / "entrypoint.sh"
    script.write_text(
        ENTRYPOINT.read_text(encoding="utf-8").replace(UPSTREAM, str(upstream)), encoding="utf-8"
    )
    return subprocess.run(
        ["sh", str(script), "gateway", "run"],
        env={"BRAIN_GUARD_HEARTBEAT": str(heartbeat), "PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
        check=True,
    )


def test_the_entrypoint_hands_over_to_the_upstream_one() -> None:
    assert f'exec {UPSTREAM} "$@"' in ENTRYPOINT.read_text(encoding="utf-8")


def test_a_fresh_heartbeat_from_the_last_run_is_gone_before_hermes_starts(tmp_path: Path) -> None:
    heartbeat = tmp_path / "heartbeat.json"
    heartbeat.write_text(json.dumps({"ts": time.time()}), encoding="utf-8")
    assert Watchdog(heartbeat).check() is True

    result = _run_entrypoint(tmp_path, heartbeat)

    assert result.stdout.strip() == "upstream gateway run"
    assert not heartbeat.exists()
    assert Watchdog(heartbeat).check() is False


def test_the_hermes_image_starts_through_the_entrypoint() -> None:
    dockerfile = (ENTRYPOINT.parent / "Dockerfile.hermes").read_text(encoding="utf-8")
    assert 'ENTRYPOINT ["/usr/local/bin/brain-hermes-entrypoint"]' in dockerfile
