"""An instance sidecar is instance code: it cannot replace a product service or reach its paths."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from second_brain.sidecars.extension import PRODUCT_SERVICES, SidecarSpec


@pytest.mark.parametrize("name", sorted(PRODUCT_SERVICES))
def test_a_sidecar_cannot_take_the_name_of_a_product_service(name: str) -> None:
    # Same name, same compose key: the sidecar would replace the watchdog, the gate or the guard.
    with pytest.raises(ValidationError, match="service of the product"):
        SidecarSpec(name=name, command=["python", "-m", "example_instance.worker"])


@pytest.mark.parametrize(
    "target",
    [
        "/vault",
        "/vault/_brain/profile.md",
        "/data/jobs.sqlite3",
        "/run/secrets/model_api_key",
        "/etc/second-brain/sidecars.yaml",
        "/etc/example/../../vault/x",
    ],
)
def test_a_sidecar_file_cannot_land_on_a_product_path(target: str) -> None:
    with pytest.raises(ValidationError):
        SidecarSpec(
            name="worker",
            command=["python", "-m", "example_instance.worker"],
            files={target: Path("/srv/brain/rules.json")},
        )


def test_a_sidecar_file_cannot_smuggle_mount_options() -> None:
    # A colon would end the host path and start another field of the compose mount.
    with pytest.raises(ValidationError):
        SidecarSpec(
            name="worker",
            command=["python", "-m", "example_instance.worker"],
            files={"/etc/example/rules.json": Path("/srv/brain/rules.json:rw")},
        )
