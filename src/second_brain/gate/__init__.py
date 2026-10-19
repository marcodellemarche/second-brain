"""Model gate and watchdog."""

from __future__ import annotations


def parse_listen(value: str) -> tuple[str, int]:
    """Split a `host:port` listen address into its parts."""
    host, sep, port = value.strip().rpartition(":")
    if not sep or not host:
        raise ValueError(f"expected host:port, got {value!r}")
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
    try:
        number = int(port)
    except ValueError as exc:
        raise ValueError(f"expected host:port, got {value!r}") from exc
    if not 0 <= number <= 65535:
        raise ValueError(f"port out of range in {value!r}")
    return host, number
