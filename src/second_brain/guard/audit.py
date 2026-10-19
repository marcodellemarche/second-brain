"""Append-only audit log of guard decisions, chained with sha256.

Every line carries the hash of the previous line, so removing, reordering or editing a line
shows up in `verify`. There is no external anchor: truncating the tail is not detectable here,
and the chain assumes one writer process owns the file.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

GENESIS = "0" * 64
MAX_VALUE_CHARS = 500
_HASH_KEY = "hash"
_TAIL_BYTES = 64 * 1024


def verify(path: Path) -> tuple[bool, int]:
    """Check the whole chain: (ok, lines checked; on failure the 1-based line that breaks)."""
    prev = GENESIS
    checked = 0
    try:
        handle = open(path, "rb")
    except FileNotFoundError:
        return True, 0
    with handle:
        for lineno, raw in enumerate(handle, start=1):
            text = raw.decode("utf-8", "replace").strip()
            if not text:
                continue
            try:
                row = json.loads(text)
            except ValueError:
                return False, lineno
            if not isinstance(row, dict):
                return False, lineno
            digest = row.pop(_HASH_KEY, None)
            if not _is_digest(digest):
                return False, lineno
            if row.get("prev") != prev or _hash(row) != digest:
                return False, lineno
            prev = digest
            checked += 1
    return True, checked


class AuditLog:
    """One JSON line per event, hash-chained to the previous line."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._prev = self._read_prev()

    def append(self, event: dict[str, Any]) -> str:
        """Write one line and return its hash."""
        with self._lock:
            row = _clean({**event, "ts": _utc_now(), "prev": self._prev})
            row.pop(_HASH_KEY, None)
            digest = _hash(row)
            data = (_dumps({**row, _HASH_KEY: digest}) + "\n").encode("utf-8")
            fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            try:
                os.write(fd, data)
            finally:
                os.close(fd)
            self._prev = digest
        return digest

    def _read_prev(self) -> str:
        line = _last_line(self.path)
        if line is None:
            return GENESIS
        try:
            row = json.loads(line)
        except ValueError as exc:
            raise ValueError(f"audit log tail is not valid JSON: {self.path}") from exc
        digest = row.get(_HASH_KEY) if isinstance(row, dict) else None
        if not _is_digest(digest):
            raise ValueError(f"audit log tail has no hash: {self.path}")
        return digest


def _last_line(path: Path) -> str | None:
    try:
        with open(path, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            if size == 0:
                return None
            handle.seek(max(0, size - _TAIL_BYTES))
            data = handle.read()
    except FileNotFoundError:
        return None
    lines = [line for line in data.decode("utf-8", "replace").splitlines() if line.strip()]
    return lines[-1] if lines else None


def _clean(value: Any) -> Any:
    if isinstance(value, str):
        return value[:MAX_VALUE_CHARS]
    if isinstance(value, dict):
        return {str(key): _clean(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_clean(item) for item in value]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)[:MAX_VALUE_CHARS]


def _dumps(row: dict[str, Any]) -> str:
    return json.dumps(row, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _hash(row: dict[str, Any]) -> str:
    return hashlib.sha256(_dumps(row).encode("utf-8")).hexdigest()


def _is_digest(value: Any) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(c in "0123456789abcdef" for c in value)


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()
