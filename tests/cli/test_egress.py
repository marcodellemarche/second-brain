"""How the hosts a tool declares become the egress proxy's allowlist."""

from __future__ import annotations

from second_brain.cli.config import split_egress, split_host


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
