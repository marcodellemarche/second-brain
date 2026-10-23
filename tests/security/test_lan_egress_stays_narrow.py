"""An `egress_lan` entry lifts the private-address deny for one name at one address, nothing more.

The proxy refuses every private address so that a CONNECT cannot reach the gate, brain_tools or
anything else on the host's networks. A self-hosted service on the LAN needs a hole in that, and
the hole must not become a way to reach an address under another name, a name at another address,
or a host nobody allowlisted.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from second_brain.cli.answers import Answers, AnswersError
from second_brain.cli.config import export_registry, template_values, tools_config
from second_brain.cli.render import render_template
from second_brain.cli.secrets import SEARXNG_KEY

SAMPLE = Path(__file__).resolve().parents[1] / "cli" / "answers.sample.yaml"
SQUID = Path(__file__).resolve().parents[2] / "templates" / "deploy" / "squid.conf.j2"
# The weather tool is on in the sample answers, so this host is on the allowlist.
ALLOWLISTED = "api.open-meteo.com"


def _answers(egress_lan: list[dict[str, str]]) -> Answers:
    raw = yaml.safe_load(SAMPLE.read_text(encoding="utf-8"))
    raw["egress_lan"] = egress_lan
    return Answers.model_validate(raw)


def _squid(answers: Answers) -> str:
    return render_template(
        SQUID, template_values(answers, export_registry(tools_config(answers)), {SEARXNG_KEY: "x"})
    )


def _access_lines(squid: str) -> list[str]:
    return [line for line in squid.splitlines() if line.startswith("http_access")]


def test_the_hole_needs_the_name_and_the_address_on_the_same_line() -> None:
    lines = _access_lines(_squid(_answers([{"host": ALLOWLISTED, "address": "192.168.1.14"}])))
    lan = [line for line in lines if "lan_egress" in line]
    assert lan == ["http_access allow CONNECT lan_egress_1_host lan_egress_1_ip"]


def test_the_hole_sits_after_the_registry_deny_and_before_the_private_deny() -> None:
    lines = _access_lines(_squid(_answers([{"host": ALLOWLISTED, "address": "192.168.1.14"}])))
    lan = lines.index("http_access allow CONNECT lan_egress_1_host lan_egress_1_ip")
    assert lines.index("http_access deny !CONNECT") < lan
    assert lines.index("http_access deny CONNECT !SSL_ports") < lan
    assert lines.index("http_access deny package_registries") < lan
    assert lan < lines.index("http_access deny private_dst")
    assert lines[-1] == "http_access deny all"


def test_two_hosts_do_not_share_their_addresses() -> None:
    squid = _squid(
        _answers(
            [
                {"host": ALLOWLISTED, "address": "192.168.1.14"},
                {"host": "geocoding-api.open-meteo.com", "address": "192.168.1.15"},
            ]
        )
    )
    assert "acl lan_egress_1_ip dst 192.168.1.14" in squid
    assert "acl lan_egress_2_ip dst 192.168.1.15" in squid
    assert "http_access allow CONNECT lan_egress_1_host lan_egress_2_ip" not in squid
    assert "http_access allow CONNECT lan_egress_2_host lan_egress_1_ip" not in squid


def test_a_host_nobody_allowlisted_is_refused() -> None:
    with pytest.raises(AnswersError, match="nothing enabled declares"):
        _squid(_answers([{"host": "gate.home.test", "address": "192.168.1.14"}]))


@pytest.mark.parametrize(
    "address", ["127.0.0.11", "169.254.169.254", "0.0.0.0", "224.0.0.1", "::1", "fe80::1"]
)
def test_loopback_metadata_and_multicast_are_never_a_lan_service(address: str) -> None:
    with pytest.raises(ValueError, match="not a LAN address"):
        _answers([{"host": ALLOWLISTED, "address": address}])


def test_without_entries_the_private_deny_is_untouched() -> None:
    squid = _squid(_answers([]))
    assert "lan_egress" not in squid
    assert "http_access deny private_dst" in squid
