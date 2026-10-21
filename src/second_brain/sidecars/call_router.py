"""Route a new call note to the team brain or keep it personal, before anything is ingested.

A call note arrives in one place only, the personal brain, because the personal brain is the only
one allowed to see everything. The decision is made here with deterministic rules, not by a model:
a wrong guess towards personal costs nothing (the note stays where it already is), a wrong guess
towards the team cannot be taken back.

The rules, in order:

1. a personal marker in the event or the domain (`event`/`domain` contains one of the configured
   strings) makes the call personal, whatever the participants say;
2. no participants at all is `ask`: the recorder matched no calendar event, so we know nothing;
3. a participant known only by name is `ask`: a name carries no domain;
4. one participant, or two with someone outside the team domains, is personal: a 1:1 is a
   conversation;
5. everyone in a team domain, and either a team marker in the event or at least
   `team_min_participants` people (the organizer in a team domain too), make it a team call;
6. everything else is `ask`, a 1:1 with a colleague included: it can be work or a personal talk,
   and only the owner knows. The brain keeps it personal and asks in the digest.

The emails come from `participant_emails` and `organizer_email` when the recorder wrote them, and
from `participants` and `organizer` otherwise.

A note is sent once it has not changed for `settle_seconds`: the recorder writes it in more than
one step, and every write would otherwise start an ingest run of its own.

The router only notices and sends: the note is not read beyond its frontmatter, nothing is written.
"""

from __future__ import annotations

import logging
import signal
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
DEFAULT_SETTLE_SECONDS = 30.0
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
    team_markers: list[str] = Field(
        default_factory=list,
        description="Strings in the event that make a call among colleagues team work, of any size.",
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

    @field_validator("personal_markers", "team_markers")
    @classmethod
    def _check_markers(cls, value: list[str]) -> list[str]:
        markers = []
        for item in value:
            marker = item.strip().casefold()
            if not marker:
                raise ValueError("a marker must not be empty")
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
    """Poll the capture folder and tell the personal brain's ingest route about every new call.

    A note still in the capture folder is one that was never ingested, so the first poll sends it.
    A missing capture folder is not an error: it is empty until it appears.
    """

    def __init__(
        self,
        watch_dir: Path | str,
        config: RouterConfig,
        send: SendEvent | None = None,
        *,
        poll_seconds: float = DEFAULT_POLL_SECONDS,
        settle_seconds: float = 0.0,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if send is None:
            raise ValueError("send is required")
        if poll_seconds <= 0:
            raise ValueError("poll_seconds must be positive")
        resolved = Path(watch_dir).resolve()
        if resolved.exists() and not resolved.is_dir():
            raise ValueError(f"watch_dir is not a directory: {watch_dir}")
        if not resolved.is_dir():
            log.warning("capture folder %s does not exist yet, waiting for it", resolved)
        self._watch_dir = resolved
        self._config = config
        self._send = send
        self._poll_seconds = poll_seconds
        self._clock = clock
        self._sleep = sleep
        self._settle_seconds = settle_seconds
        self._seen: dict[str, tuple[int, int]] = {}
        self._settling: dict[str, tuple[tuple[int, int], float]] = {}
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

        people = _participants(frontmatter.get("participant_emails")) or _participants(
            frontmatter.get("participants")
        )
        if not people:
            return Decision("ask", "no participants: the recorder matched no calendar event")
        if len(people) == 1:
            return Decision("personal", "one participant: nobody else was there")
        domains = [_domain_of(person) for person in people]
        names = [person for person, domain in zip(people, domains, strict=True) if domain is None]
        if names:
            return Decision("ask", f"only a name, not an email: {', '.join(names)}")

        team = self._config.team_domains
        outside = sorted({domain for domain in domains if domain not in team})
        if outside and len(people) <= 2:
            return Decision("personal", f"a 1:1 with someone outside the team: {', '.join(outside)}")
        if outside:
            return Decision("ask", f"participants outside the team: {', '.join(outside)}")

        event = str(frontmatter.get("event") or "").casefold()
        marker = next((marker for marker in self._config.team_markers if marker in event), None)
        if marker is not None:
            return Decision("team", f"team marker {marker!r}, everyone in team domains")
        if len(people) < self._config.team_min_participants:
            if len(people) <= 2:
                return Decision("ask", "a 1:1 with a colleague: work or personal?")
            return Decision("ask", f"only {len(people)} participants")

        organizer = str(frontmatter.get("organizer_email") or frontmatter.get("organizer") or "").strip()
        if not organizer:
            return Decision("ask", "the organizer is missing")
        organizer_domain = _domain_of(organizer)
        if organizer_domain is None:
            return Decision("ask", "the organizer is a name, not an email")
        if organizer_domain not in team:
            return Decision("ask", f"the organizer is not in a team domain: {organizer_domain}")
        return Decision("team", f"{len(people)} people, all in team domains")

    def poll_once(self) -> list[dict[str, Any]]:
        """One pass over the capture folder. Returns the events Hermes accepted during it."""
        current = self._scan()
        now = self._clock()
        sent: list[dict[str, Any]] = []
        for path in sorted(current):
            if self._seen.get(path) == current[path]:
                continue
            if not self._settled(path, current[path], now):
                continue
            decision = self.decide(read_frontmatter(self._watch_dir / path))
            payload = {
                "kind": "call_arrived",
                "path": path,
                "route": decision.route,
                "reason": decision.reason,
            }
            if self._deliver(payload):
                self._seen[path] = current[path]
                sent.append(payload)
            else:
                # Still unseen, so the next poll tries again: the note is not lost to one outage.
                self._seen.pop(path, None)
        for path in sorted(set(self._seen) - set(current)):
            self._seen.pop(path, None)
        for path in sorted(set(self._settling) - set(current)):
            self._settling.pop(path, None)
        return sent

    def _settled(self, path: str, state: tuple[int, int], now: float) -> bool:
        """Whether the note has kept the same size and mtime for `settle_seconds`."""
        if self._settle_seconds <= 0:
            return True
        previous = self._settling.get(path)
        if previous is None or previous[0] != state:
            self._settling[path] = (state, now)
            return False
        return now - previous[1] >= self._settle_seconds

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
        if not self._watch_dir.is_dir():
            return {}
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

    def _deliver(self, payload: dict[str, Any]) -> bool:
        path, route = payload["path"], payload["route"]
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


def main() -> None:  # pragma: no cover - process entry point
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    from second_brain.sidecars.config import SidecarsConfig

    config = SidecarsConfig.load()
    section = config.require("call_router")
    router = CallRouter(
        section.resolved_watch_dir(config.vault_dir),
        section.rules,
        send=config.sender(section.route),
        poll_seconds=section.poll_seconds,
        settle_seconds=section.settle_seconds,
    )
    log.info("call router on %s, route %s", router.watch_dir, section.route)

    def _stop(*_args: object) -> None:
        router.stop()

    signal.signal(signal.SIGTERM, _stop)
    router.run_forever()


if __name__ == "__main__":
    main()


__all__ = [
    "DEFAULT_POLL_SECONDS",
    "DEFAULT_SETTLE_SECONDS",
    "DEFAULT_TEAM_MIN_PARTICIPANTS",
    "CallRouter",
    "Decision",
    "Route",
    "RouterConfig",
    "read_frontmatter",
]
