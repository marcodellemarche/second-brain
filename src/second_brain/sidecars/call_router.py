"""Route a new call note to the team brain or keep it personal, before anything is ingested.

A call note arrives in one place only, the personal brain, because the personal brain is the only
one allowed to see everything. The decision is made here with deterministic rules, not by a model:
a wrong guess towards personal costs nothing (the note stays where it already is), a wrong guess
towards the team cannot be taken back.

The rules, in order:

1. a personal marker in the event or the domain (`event`/`domain` contains one of the configured
   strings) makes the call personal, whatever the participants say;
2. one or two participants are personal: a 1:1 is a conversation, not a meeting;
3. every participant and the organizer in a team domain, and at least `team_min_participants`
   people, make it a team call;
4. everything else is `ask`, and the brain keeps it personal and asks the user in the digest.

`participants` holds emails or names. A name carries no domain, so a call with a named participant
can never satisfy rule 3: it ends in `ask`, which is the safe side. Missing fields are `ask` too,
never an exception: no participants at all is not a 1:1, it is a note we know nothing about.

The router only notices and sends: the note is not read beyond its frontmatter, nothing is written.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

from second_brain.sidecars.webhook import SendEvent, WebhookError

log = logging.getLogger(__name__)

DEFAULT_POLL_SECONDS = 10.0
DEFAULT_TEAM_MIN_PARTICIPANTS = 3
FRONTMATTER_OPEN = "---"

Route = Literal["team", "personal", "ask"]


class RouterConfig(BaseModel):
    """What the rules need. An unknown key is refused: a typo must not silently widen the team."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    team_domains: list[str] = Field(
        default_factory=list,
        description="Email domains of the team, for example 'example.com'.",
    )
    team_min_participants: int = Field(
        default=DEFAULT_TEAM_MIN_PARTICIPANTS,
        ge=2,
        description="How many people make a call a team meeting.",
    )
    personal_markers: list[str] = Field(
        default_factory=list,
        description="Strings in the event or the domain that force a call to stay personal.",
    )

    @field_validator("team_domains")
    @classmethod
    def _check_domains(cls, value: list[str]) -> list[str]:
        domains = []
        for item in value:
            domain = item.strip().strip("@").casefold()
            if not domain or "@" in domain or "/" in domain:
                raise ValueError(f"not an email domain: {item!r}")
            domains.append(domain)
        return domains

    @field_validator("personal_markers")
    @classmethod
    def _check_markers(cls, value: list[str]) -> list[str]:
        markers = []
        for item in value:
            marker = item.strip().casefold()
            if not marker:
                raise ValueError("a personal marker must not be empty")
            markers.append(marker)
        return markers


def _participants(value: Any) -> list[str]:
    """The participants as a list of strings. A comma-separated string is accepted, a name is one."""
    if value is None:
        return []
    if isinstance(value, str):
        return [part.strip() for part in value.split(",") if part.strip()]
    if isinstance(value, list | tuple):
        return [str(part).strip() for part in value if str(part).strip()]
    return []


def _domain_of(value: str) -> str | None:
    """The email domain of a participant, or None when it is only a name."""
    candidate = value.strip().strip("<>").casefold()
    if candidate.count("@") != 1:
        return None
    local, domain = candidate.split("@")
    if not local or not domain:
        return None
    return domain


def _haystack(frontmatter: Mapping[str, Any]) -> str:
    parts = [str(frontmatter.get("event") or ""), str(frontmatter.get("domain") or "")]
    return " ".join(parts).casefold()


@dataclass(frozen=True, slots=True)
class Decision:
    route: Route
    reason: str


class CallRouter:
    """Poll the capture folder and tell the personal brain's ingest route about every new call."""

    def __init__(
        self,
        watch_dir: Path | str,
        config: RouterConfig,
        send: SendEvent | None = None,
        *,
        poll_seconds: float = DEFAULT_POLL_SECONDS,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if send is None:
            raise ValueError("send is required")
        if poll_seconds <= 0:
            raise ValueError("poll_seconds must be positive")
        resolved = Path(watch_dir).resolve()
        if not resolved.is_dir():
            raise ValueError(f"watch_dir is not a directory: {watch_dir}")
        self._watch_dir = resolved
        self._config = config
        self._send = send
        self._poll_seconds = poll_seconds
        self._clock = clock
        self._sleep = sleep
        self._seen: dict[str, tuple[int, int]] = self._scan()
        self._stopped = False

    @property
    def watch_dir(self) -> Path:
        return self._watch_dir

    @property
    def config(self) -> RouterConfig:
        return self._config

    def route(self, frontmatter: Mapping[str, Any]) -> Route:
        """The route of one call note, from its frontmatter alone. Never raises on bad input."""
        return self.decide(frontmatter).route

    def decide(self, frontmatter: Mapping[str, Any]) -> Decision:
        """The route and why, so the digest can explain the `ask` ones."""
        text = _haystack(frontmatter)
        for marker in self._config.personal_markers:
            if marker in text:
                return Decision("personal", f"personal marker {marker!r}")

        people = _participants(frontmatter.get("participants"))
        if not people:
            return Decision("ask", "no participants")
        if len(people) <= 2:
            return Decision("personal", f"{len(people)} participant(s): a 1:1 stays personal")

        organizer = str(frontmatter.get("organizer") or "").strip()
        if not organizer:
            return Decision("ask", "the organizer is missing")
        domains = [_domain_of(person) for person in people]
        if any(domain is None for domain in domains):
            return Decision("ask", "a participant is a name, not an email")
        organizer_domain = _domain_of(organizer)
        if organizer_domain is None:
            return Decision("ask", "the organizer is a name, not an email")
        if organizer_domain not in self._config.team_domains:
            return Decision("ask", f"the organizer is not in a team domain: {organizer_domain}")

        outside = sorted({domain for domain in domains if domain not in self._config.team_domains})
        if outside:
            return Decision("ask", f"participants outside the team: {', '.join(outside)}")
        if len(people) < self._config.team_min_participants:
            return Decision("ask", f"only {len(people)} participants")
        return Decision("team", f"{len(people)} people, all in team domains")

    def poll_once(self) -> list[dict[str, Any]]:
        """One pass over the capture folder. Returns the events Hermes accepted during it."""
        current = self._scan()
        sent: list[dict[str, Any]] = []
        for path in sorted(current):
            if self._seen.get(path) == current[path]:
                continue
            frontmatter = read_frontmatter(self._watch_dir / path)
            route = self.route(frontmatter)
            if self._deliver(path, route):
                self._seen[path] = current[path]
                sent.append({"kind": "call_arrived", "path": path, "route": route})
            else:
                # Still unseen, so the next poll tries again: the note is not lost to one outage.
                self._seen.pop(path, None)
        for path in sorted(set(self._seen) - set(current)):
            self._seen.pop(path, None)
        return sent

    def run_forever(self, poll_seconds: float | None = None) -> None:
        """Poll until stopped. A failing poll logs and the next one runs: the sidecar stays up."""
        interval = self._poll_seconds if poll_seconds is None else poll_seconds
        if interval <= 0:
            raise ValueError("poll_seconds must be positive")
        log.info("call router polling %s every %.0fs", self._watch_dir, interval)
        while not self._stopped:
            try:
                self.poll_once()
            except Exception:
                log.exception("call router poll failed")
            self._sleep(interval)

    def stop(self) -> None:
        self._stopped = True

    def _scan(self) -> dict[str, tuple[int, int]]:
        entries: dict[str, tuple[int, int]] = {}
        for candidate in sorted(self._watch_dir.rglob("*")):
            if not candidate.is_file() or candidate.is_symlink():
                continue
            if candidate.name.startswith(".") or not candidate.name.casefold().endswith(".md"):
                continue
            try:
                info = candidate.stat()
            except OSError:
                continue
            entries[candidate.relative_to(self._watch_dir).as_posix()] = (info.st_size, info.st_mtime_ns)
        return entries

    def _deliver(self, path: str, route: Route) -> bool:
        payload = {"kind": "call_arrived", "path": path, "route": route}
        try:
            status = self._send(payload)
        except (WebhookError, OSError, ValueError) as exc:
            log.warning("call %s not sent: %s", path, exc)
            return False
        if not 200 <= status < 300:
            log.warning("call %s refused with %d", path, status)
            return False
        log.info("call %s routed to %s", path, route)
        return True


def read_frontmatter(path: Path) -> dict[str, Any]:
    """The frontmatter of a note, or an empty mapping when it has none or cannot be read.

    Only the block at the top is read, never the body: the router routes, it does not ingest.
    """
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            first = handle.readline().rstrip("\r\n")
            if first != FRONTMATTER_OPEN:
                return {}
            lines: list[str] = []
            for line in handle:
                if line.rstrip("\r\n") == FRONTMATTER_OPEN:
                    break
                lines.append(line)
                if len(lines) > 200:
                    return {}
            else:
                return {}
    except OSError as exc:
        log.warning("cannot read %s: %s", path, exc)
        return {}
    try:
        parsed = yaml.safe_load("".join(lines))
    except yaml.YAMLError as exc:
        log.warning("bad frontmatter in %s: %s", path, exc)
        return {}
    return dict(parsed) if isinstance(parsed, dict) else {}


__all__ = [
    "DEFAULT_POLL_SECONDS",
    "DEFAULT_TEAM_MIN_PARTICIPANTS",
    "CallRouter",
    "Decision",
    "Route",
    "RouterConfig",
    "read_frontmatter",
]
