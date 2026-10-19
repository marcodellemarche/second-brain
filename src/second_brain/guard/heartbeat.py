"""Heartbeat file the watchdog reads to know the guard is loaded and alive."""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any


def write_heartbeat(path: Path, info: dict[str, Any]) -> None:
    """Replace the heartbeat atomically, so a reader never sees a half-written file."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {**info, "ts": time.time()}
    tmp = target.with_name(f".{target.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        os.replace(tmp, target)
    finally:
        tmp.unlink(missing_ok=True)


def read_heartbeat(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def is_fresh(path: Path, max_age: float, now: float) -> bool:
    beat = read_heartbeat(path)
    if beat is None:
        return False
    ts = beat.get("ts")
    if isinstance(ts, bool) or not isinstance(ts, (int, float)):
        return False
    # A timestamp far in the future is as wrong as a stale one.
    return abs(now - ts) <= max_age
