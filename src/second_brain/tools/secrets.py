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
        re.compile(
            r"(?i)\b(?:password|passwd|pwd|secret|api[_-]?key|apikey|token|access[_-]?key)\b"
            r"\s*[:=]\s*(?P<value>\S{8,})"
        ),
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
# A slug or a path: lowercase words joined by `/` or `-`. Random keys mix case or carry no separator.
_LOWERCASE_SEGMENT = re.compile(r"[a-z0-9_.]+")
_UUID_RE = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
# A file name can be long and mixed case; the extension at the end of the token gives it away.
_DATA_BLOCK_RE = re.compile(r"^```(?:compressed-json|json)\r?\n.*?^```[ \t]*$", re.DOTALL | re.MULTILINE)
_CAMEL_CASE_RE = re.compile(r"[A-Za-z][a-z0-9]+(?:[A-Z][a-z0-9]+){2,}")
_DIGITS_IN_SEGMENT = re.compile(r"[A-Za-z]+[0-9]|[0-9]+[A-Za-z]")


def _is_wordlike(value: str) -> bool:
    """True when every CamelCase segment is a plain word: identifiers read like this, keys do not."""
    return not _DIGITS_IN_SEGMENT.search(value)


_FILE_NAME_RE = re.compile(
    r"^\S+\.(?:pdf|md|markdown|txt|json|ya?ml|toml|csv|tsv|png|jpe?g|gif|webp|heic|svg|mp4|mov|wav|mp3|"
    r"excalidraw|base|sh|py|go|rs|ts|tsx|js|jsx|html|css|sql|patch|diff|docx?|xlsx?|pptx?)$",
    re.IGNORECASE,
)


def _is_placeholder(value: str) -> bool:
    value = value.strip("\"'`").lower()
    if not value or value.startswith("<") or value.startswith("$"):
        return True
    if not value.strip("*x"):
        return True
    return any(marker in value for marker in PLACEHOLDER_MARKERS)


def _looks_like_value(value: str) -> bool:
    """A credential is one opaque token: prose after the key means the key was a word in a sentence."""
    if _is_placeholder(value):
        return False
    if value.startswith(("http://", "https://", "/", "./", "~/", "@")):
        return False
    # `Secret: the keys stay in the gateway` and `token: "" for the first page` are prose, not values.
    if not re.fullmatch(r"[\w+/=.~:@-]+", value):
        return False
    if value.isalpha():
        return False
    # A word with a digit glued on (`remoto_1`, `shard2`, `first-page`) is prose; a key mixes case,
    # carries symbols, or is a long run with no separator at all.
    lowered = value.lower()
    if re.fullmatch(r"[a-z0-9_.-]+", lowered):
        if re.fullmatch(r"[0-9a-f]{16,}", lowered):
            return True
        return "_" not in lowered and len(value) >= 12
    return True


def _looks_like_digest(text: str, start: int) -> bool:
    return "sha256" in text[max(0, start - 24) : start].lower()


def _looks_like_path(value: str, text: str = "", start: int = 0, end: int = 0) -> bool:
    """A lowercase slug, a relative path, a UUID or a file name reads as high entropy but is structure."""
    if _UUID_RE.fullmatch(value):
        return True
    if _CAMEL_CASE_RE.fullmatch(value) and _is_wordlike(value):
        # `TestBucketsHardDeletionOnProjectHardDeletion` is an identifier, not a key: a key mixes
        # digits into its segments.
        return True
    if text:
        # The match may be one segment of a longer file name: look at the token around it.
        delimiters = " \t\n\r[]()|`'\"<>"
        token_start = start
        while token_start > 0 and text[token_start - 1] not in delimiters:
            token_start -= 1
        token_end = end
        while token_end < len(text) and text[token_end] not in delimiters:
            token_end += 1
        if _FILE_NAME_RE.fullmatch(text[token_start:token_end]):
            return True
    if "/" not in value and "-" not in value:
        return False
    if "/" in value and " " not in value:
        return True
    segments = [segment for segment in re.split(r"[/-]", value) if segment]
    return bool(segments) and all(_LOWERCASE_SEGMENT.fullmatch(segment) for segment in segments)


def _in_exempt_span(spans: list[tuple[int, int]], start: int) -> bool:
    return any(begin <= start < end for begin, end in spans)


def _exempt_spans(text: str) -> list[tuple[int, int]]:
    """A wikilink, a URL without credentials, a path or a data block is structure, not a secret."""
    spans = [(span.start(), span.end()) for span in _WIKILINK_RE.finditer(text)]
    # An Excalidraw note carries the drawing as base64 in a fenced `compressed-json` block. It is
    # data the editor writes, not something a person pasted, so it is not scanned for keys.
    spans.extend((span.start(), span.end()) for span in _DATA_BLOCK_RE.finditer(text))
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
            if rule in _VALUE_RULES and not _looks_like_value(match.group("value")):
                continue
            yield _Match(rule, match.start(), match.end(), match.group(0))

    spans = _exempt_spans(text)
    for match in _ENTROPY_RE.finditer(text):
        if _looks_like_digest(text, match.start()):
            continue
        if _looks_like_path(match.group(0), text, match.start(), match.end()):
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
