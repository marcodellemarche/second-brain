"""How the hosts a tool declares become the egress proxy's allowlist."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from second_brain.cli.answers import Answers, AnswersError
from second_brain.cli.config import egress_lan_rules, split_egress, split_host

SAMPLE = Path(__file__).with_name("answers.sample.yaml")


def test_a_host_without_a_port_is_https() -> None:
    assert split_host("api.github.com") == ("api.github.com", 443)


def test_a_host_with_a_port_keeps_it() -> None:
    assert split_host("k8s.example.test:6443") == ("k8s.example.test", 6443)
    assert split_host("203.0.113.7:6443") == ("203.0.113.7", 6443)


def test_a_colon_without_a_number_is_not_a_port() -> None:
    assert split_host("odd:name") == ("odd:name", 443)


def test_names_and_addresses_are_split_for_squid() -> None:
    names, ips = split_egress(["api.github.com", "203.0.113.7", "k8s.example.test"])
    assert names == ["api.github.com", "k8s.example.test"]
    assert ips == ["203.0.113.7"]


def _answers(tmp_path: Path, egress_lan: list[dict[str, str]]) -> Answers:
    raw = yaml.safe_load(SAMPLE.read_text(encoding="utf-8"))
    raw["egress_lan"] = egress_lan
    path = tmp_path / "answers.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    return Answers.from_yaml(path)


def test_no_lan_hosts_render_no_rules(tmp_path: Path) -> None:
    answers = _answers(tmp_path, [])
    assert egress_lan_rules(answers, ["api.open-meteo.com"]) == []


def test_a_lan_host_is_allowed_by_name_and_address_together(tmp_path: Path) -> None:
    answers = _answers(tmp_path, [{"host": "media.home.test", "address": "192.168.1.14"}])
    assert egress_lan_rules(answers, ["media.home.test"]) == [
        "acl lan_egress_1_host dstdomain media.home.test",
        "acl lan_egress_1_ip dst 192.168.1.14",
        "http_access allow CONNECT lan_egress_1_host lan_egress_1_ip",
    ]


def test_a_mesh_vpn_address_counts_as_lan(tmp_path: Path) -> None:
    answers = _answers(tmp_path, [{"host": "media.home.test", "address": "100.101.102.103"}])
    assert answers.egress_lan[0].address.exploded == "100.101.102.103"


@pytest.mark.parametrize("address", ["1.1.1.1", "8.8.8.8"])
def test_a_public_address_is_refused(tmp_path: Path, address: str) -> None:
    with pytest.raises(AnswersError, match="public"):
        _answers(tmp_path, [{"host": "media.home.test", "address": address}])


def test_a_network_is_not_an_address(tmp_path: Path) -> None:
    with pytest.raises(AnswersError):
        _answers(tmp_path, [{"host": "media.home.test", "address": "192.168.0.0/16"}])
