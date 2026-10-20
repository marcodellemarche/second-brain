"""Per-session guard state: taint flags, the parent chain, known URLs and call counts.

State is in memory and shared by all hook threads. Every lookup that spans sessions walks the
parent chain, so a flag set on a parent after a child started still applies to the child.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from dataclasses import dataclass, field
from typing import Any


def call_key(tool: str, args: dict[str, Any]) -> str:
    """Stable key for a tool call: same tool and same args in any key order hash the same."""
    payload = json.dumps([tool, args], sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass
class SessionState:
    sid: str
    sender: str = ""
    platform: str = ""
    parent: str | None = None
    untrusted_seen: bool = False
    private_seen: bool = False
    known_urls: set[str] = field(default_factory=set)
    steps: int = 0
    started: float = field(default_factory=time.monotonic)
    call_counts: dict[str, int] = field(default_factory=dict)
    empty_searches: int = 0
    # Searches allowed whose result has not come back: parallel calls are all decided first.
    pending_searches: int = 0


class SessionStore:
    """Thread-safe registry of session state."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._sessions: dict[str, SessionState] = {}

    def get(self, sid: str) -> SessionState:
        with self._lock:
            state = self._sessions.get(sid)
            if state is None:
                state = SessionState(sid=sid)
                self._sessions[sid] = state
            return state

    def set_identity(self, sid: str, sender: str = "", platform: str = "") -> None:
        """Remember who is talking. An empty value never clears what is already known."""
        with self._lock:
            state = self.get(sid)
            if sender:
                state.sender = sender
            if platform:
                state.platform = platform

    def link_child(self, child_sid: str, parent_sid: str) -> None:
        with self._lock:
            self.get(child_sid).parent = parent_sid
            self.get(parent_sid)

    def root(self, sid: str) -> SessionState:
        """Last session of the chain. A cycle stops the walk at its last distinct member."""
        with self._lock:
            state = self.get(sid)
            seen = {state.sid}
            while state.parent is not None and state.parent not in seen:
                seen.add(state.parent)
                state = self.get(state.parent)
            return state

    def sender(self, sid: str) -> str:
        """First non-empty sender walking up the chain."""
        with self._lock:
            for state in self._chain(sid):
                if state.sender:
                    return state.sender
            return ""

    def flags(self, sid: str) -> tuple[bool, bool]:
        """(untrusted_seen, private_seen) OR-ed over the whole chain."""
        with self._lock:
            chain = self._chain(sid)
            return (
                any(state.untrusted_seen for state in chain),
                any(state.private_seen for state in chain),
            )

    def mark_untrusted(self, sid: str) -> None:
        with self._lock:
            self.get(sid).untrusted_seen = True

    def mark_private(self, sid: str) -> None:
        with self._lock:
            self.get(sid).private_seen = True

    def add_known_urls(self, sid: str, urls: set[str] | list[str]) -> None:
        with self._lock:
            self.get(sid).known_urls.update(urls)

    def knows_url(self, sid: str, url: str) -> bool:
        with self._lock:
            return any(url in state.known_urls for state in self._chain(sid))

    def record_call(self, sid: str, tool: str, args: dict[str, Any]) -> int:
        """Count this exact call in this session, including the one being recorded."""
        key = call_key(tool, args)
        with self._lock:
            counts = self.get(sid).call_counts
            counts[key] = counts.get(key, 0) + 1
            return counts[key]

    def next_step(self, sid: str) -> tuple[int, float]:
        """Count one more step and return (steps, started) atomically."""
        with self._lock:
            state = self.get(sid)
            state.steps += 1
            return state.steps, state.started

    def note_search(self, sid: str, new_items: int) -> int:
        """Reset the empty-search streak on new results, extend it otherwise. Returns the streak."""
        with self._lock:
            state = self.get(sid)
            state.empty_searches = 0 if new_items > 0 else state.empty_searches + 1
            state.pending_searches = max(0, state.pending_searches - 1)
            return state.empty_searches

    def start_search(self, sid: str) -> None:
        with self._lock:
            self.get(sid).pending_searches += 1

    def pending_searches(self, sid: str) -> int:
        with self._lock:
            return self.get(sid).pending_searches

    def reset_pending_searches(self, sid: str) -> None:
        with self._lock:
            self.get(sid).pending_searches = 0

    def empty_searches(self, sid: str) -> int:
        with self._lock:
            return self.get(sid).empty_searches

    def forget(self, sid: str) -> None:
        with self._lock:
            self._sessions.pop(sid, None)

    def _chain(self, sid: str) -> list[SessionState]:
        """Sessions from `sid` up to the root, each at most once. Caller holds the lock."""
        chain: list[SessionState] = []
        seen: set[str] = set()
        state: SessionState | None = self.get(sid)
        while state is not None and state.sid not in seen:
            seen.add(state.sid)
            chain.append(state)
            state = self._sessions.get(state.parent) if state.parent is not None else None
        return chain
