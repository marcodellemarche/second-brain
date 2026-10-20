"""Secret scanner for vault content.

Finds credentials in text so a caller can refuse a write or report a migration.
The matched value is never returned or logged: a finding carries a masked
preview only.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass
from re import Pattern

PREVIEW_VISIBLE_CHARS = 4
PREVIEW_MAX_CHARS = 12
ENTROPY_THRESHOLD = 4.0
ENTROPY_MIN_LENGTH = 32
PLACEHOLDER_MARKERS = ("changeme", "example")


@dataclass(frozen=True, slots=True)
class Finding:
    rule: str
    line: int
    column: int
    preview: str


@dataclass(frozen=True, slots=True)
class _Match:
    rule: str
    start: int
    end: int
    text: str


_RULES: tuple[tuple[str, Pattern[str]], ...] = (
    ("aws_access_key_id", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("github_token", re.compile(r"gh[pousr]_[A-Za-z0-9]{36,}")),
    ("gitlab_token", re.compile(r"glpat-[A-Za-z0-9_-]{20,}")),
    ("slack_token", re.compile(r"xox[abpors]-[A-Za-z0-9-]{10,}")),
    ("openai_key", re.compile(r"sk-[A-Za-z0-9_-]{20,}")),
    ("private_key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("jwt", re.compile(r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}")),
    (
        "assignment",
        re.compile(r"(?i)\b(?:password|passwd|secret|api[_-]?key|token)\b\s*[:=]\s*(?P<value>\S{8,})"),
    ),
    # URLs are exempt from the entropy rule, so a credential in the query string needs its own.
    (
        "url_query_secret",
        re.compile(
            r"(?i)[?&](?:access_token|refresh_token|id_token|token|api[_-]?key|apikey|key|secret|"
            r"client_secret|password|passwd|sig|signature|auth)=(?P<value>[^&#\s]{8,})"
        ),
    ),
    # `scheme://user:password@host`, and `curl -u user:password`, carry one too.
    (
        "url_userinfo_password",
        re.compile(r"\b[a-zA-Z][a-zA-Z0-9+.\-]*://[^\s/:@]+:(?P<value>[^\s/@]{6,})@"),
    ),
    (
        "curl_basic_auth",
        re.compile(r"\bcurl\b[^\n]*?\s(?:-u|--user)[ =]['\"]?[^\s:'\"]+:(?P<value>[^\s'\"]{6,})"),
    ),
)

_VALUE_RULES = frozenset({"assignment", "url_query_secret", "url_userinfo_password", "curl_basic_auth"})
_ENTROPY_RE = re.compile(rf"[A-Za-z0-9+/=_-]{{{ENTROPY_MIN_LENGTH},}}")
_URL_RE = re.compile(r"\b[a-zA-Z][a-zA-Z0-9+.\-]*://\S+")
_WIKILINK_RE = re.compile(r"\[\[[^\]]*\]\]")
# A path is structure, like a URL: its entropy comes from long readable segments, not from a key.
_PATH_RE = re.compile(r"(?:^|[\s`'\"(\[])(?:~?/|\.{1,2}/)[^\s`'\"()\[\]]+", re.MULTILINE)
_RELATIVE_PATH_RE = re.compile(r"(?<![\w/])[\w.-]+(?:/[\w.-]+){2,}/?", re.MULTILINE)


def _is_placeholder(value: str) -> bool:
    value = value.strip("\"'`").lower()
    if not value or value.startswith("<") or value.startswith("$"):
        return True
    if not value.strip("*x"):
        return True
    return any(marker in value for marker in PLACEHOLDER_MARKERS)


def _looks_like_digest(text: str, start: int) -> bool:
    return "sha256" in text[max(0, start - 24) : start].lower()


def _in_exempt_span(spans: list[tuple[int, int]], start: int) -> bool:
    return any(begin <= start < end for begin, end in spans)


def _exempt_spans(text: str) -> list[tuple[int, int]]:
    """A wikilink, a URL without credentials or a filesystem path is structure, not a secret."""
    spans = [(span.start(), span.end()) for span in _WIKILINK_RE.finditer(text)]
    for span in _URL_RE.finditer(text):
        netloc = span.group(0).split("://", 1)[1].split("/", 1)[0]
        if "@" not in netloc:
            spans.append((span.start(), span.end()))
    spans.extend((span.start(), span.end()) for span in _PATH_RE.finditer(text))
    spans.extend((span.start(), span.end()) for span in _RELATIVE_PATH_RE.finditer(text))
    return spans


def _shannon_entropy(value: str) -> float:
    total = len(value)
    return -sum((count / total) * math.log2(count / total) for count in Counter(value).values())


def _candidates(text: str) -> Iterator[_Match]:
    for rule, pattern in _RULES:
        for match in pattern.finditer(text):
            if rule in _VALUE_RULES and _is_placeholder(match.group("value")):
                continue
            yield _Match(rule, match.start(), match.end(), match.group(0))

    spans = _exempt_spans(text)
    for match in _ENTROPY_RE.finditer(text):
        if _looks_like_digest(text, match.start()):
            continue
        if _in_exempt_span(spans, match.start()):
            continue
        if _shannon_entropy(match.group(0)) <= ENTROPY_THRESHOLD:
            continue
        yield _Match("high_entropy", match.start(), match.end(), match.group(0))


def _matches(text: str) -> list[_Match]:
    accepted: list[_Match] = []
    for candidate in _candidates(text):
        if any(candidate.start < other.end and other.start < candidate.end for other in accepted):
            continue
        accepted.append(candidate)
    accepted.sort(key=lambda match: match.start)
    return accepted


def _preview(matched: str) -> str:
    visible = matched[:PREVIEW_VISIBLE_CHARS]
    masked = min(len(matched), PREVIEW_MAX_CHARS) - len(visible)
    return visible + "*" * max(masked, 0)


def _position(text: str, offset: int) -> tuple[int, int]:
    line = text.count("\n", 0, offset) + 1
    line_start = text.rfind("\n", 0, offset) + 1
    return line, offset - line_start + 1


def scan(text: str) -> list[Finding]:
    """Return every secret-looking match in text, with a masked preview."""
    findings: list[Finding] = []
    for match in _matches(text):
        line, column = _position(text, match.start)
        findings.append(
            Finding(
                rule=match.rule,
                line=line,
                column=column,
                preview=_preview(match.text),
            )
        )
    return findings


def redact(text: str) -> str:
    """Replace every match with a [REDACTED:<rule>] marker."""
    parts: list[str] = []
    cursor = 0
    for match in _matches(text):
        parts.append(text[cursor : match.start])
        parts.append(f"[REDACTED:{match.rule}]")
        cursor = match.end
    parts.append(text[cursor:])
    return "".join(parts)
