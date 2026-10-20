"""Tests for the secret scanner. Every fake value is built at runtime."""

from __future__ import annotations

import string

import pytest

from second_brain.tools.secrets import Finding, redact, scan


def _aws() -> str:
    return "AKIA" + "A" * 16


def _github() -> str:
    return "ghp_" + "a1B2c3D4e5" * 4


def _gitlab() -> str:
    return "glpat-" + "a1B2c3D4e5f6G7h8I9j0"


def _slack() -> str:
    return "xoxb-" + "1234567890-abcdefghij"


def _openai() -> str:
    return "sk-" + "a1B2c3D4e5f6G7h8I9j0K1l2"


def _private_key() -> str:
    return "-----BEGIN RSA PRIVATE KEY-----"


def _jwt() -> str:
    return ".".join(["eyJ" + "A" * 9, "B" * 12, "C" * 12])


def _entropy() -> str:
    return "Zx9Kq2Lm8Rt4Vw7Yb1Nc6Hd3Fg5Jp0Qs"


def _random_hex(length: int = 64) -> str:
    return (string.hexdigits[:16] * 8)[:length]


@pytest.mark.parametrize(
    ("rule", "value"),
    [
        ("aws_access_key_id", _aws()),
        ("github_token", _github()),
        ("gitlab_token", _gitlab()),
        ("slack_token", _slack()),
        ("openai_key", _openai()),
        ("private_key", _private_key()),
        ("jwt", _jwt()),
        ("assignment", "password = " + "hunter2hunter2"),
        ("high_entropy", _entropy()),
    ],
)
def test_each_rule_flags_its_value(rule: str, value: str) -> None:
    findings = scan(f"Some note text.\n{value}\nMore text.\n")

    assert [finding.rule for finding in findings] == [rule]
    assert findings[0].line == 2


def test_assignment_rule_covers_the_documented_keywords() -> None:
    for key in ("password", "passwd", "pwd", "secret", "api_key", "api-key", "token", "PASSWORD"):
        findings = scan(f"{key}: {'hunter2' * 2}")
        assert [finding.rule for finding in findings] == ["assignment"], key


def test_normal_markdown_is_not_flagged() -> None:
    note = "\n".join(
        [
            "---",
            "type: note",
            "created: 2024-05-01",
            "updated: 2024-05-01T10:00:00",
            "original_sha256: " + _random_hex(),
            "tags: [one, two]",
            "---",
            "",
            "See [[Some Topic]] and [[2024-05-01 planning]].",
            "Link: https://example.com/a/long/path/to/some/resource?ref=notes",
            "Digest sha256:" + _random_hex(),
            "Date 2024-05-01 and 2024-05-01T10:00:00Z.",
        ]
    )

    assert scan(note) == []


def test_placeholder_values_are_not_flagged() -> None:
    for value in ("<your-token-here>", "***", "xxxxxxxx", "changeme", "example-value", "${TOKEN}"):
        assert scan(f"token = {value}") == [], value


def test_short_values_are_not_flagged() -> None:
    assert scan("token = abc123") == []


def test_preview_keeps_at_most_four_real_characters() -> None:
    finding = scan(_aws())[0]

    assert finding.preview == "AKIA" + "*" * 8
    assert len(finding.preview) <= 12
    assert set(finding.preview[4:]) == {"*"}


def test_preview_masks_short_matches_entirely_after_four_chars() -> None:
    finding = scan(_private_key())[0]

    assert finding.preview.startswith("----")
    assert set(finding.preview[4:]) == {"*"}


def test_line_and_column_are_one_based() -> None:
    text = "first line\nsecond line " + _aws() + "\n"

    finding = scan(text)[0]

    assert (finding.line, finding.column) == (2, 13)


def test_finding_is_frozen() -> None:
    finding = Finding(rule="rule", line=1, column=1, preview="****")

    with pytest.raises(AttributeError):
        finding.rule = "other"  # type: ignore[misc]


def test_redact_removes_every_finding() -> None:
    text = f"aws {_aws()} and gh {_github()} and password = {'hunter2hunter2'}\n"

    redacted = redact(text)

    assert scan(redacted) == []
    assert _aws() not in redacted
    assert _github() not in redacted
    assert "hunter2hunter2" not in redacted
    assert redacted.count("[REDACTED:") == 3


def test_redact_keeps_the_rest_of_the_text() -> None:
    text = f"line one\nkey: {_aws()}\nline three\n"

    assert redact(text) == "line one\nkey: [REDACTED:aws_access_key_id]\nline three\n"


def test_scan_of_plain_text_returns_nothing() -> None:
    assert scan("") == []
    assert scan("just a normal sentence about the weather") == []


@pytest.mark.parametrize(
    "url",
    [
        "https://api.example.com/v1/items?access_token=fakeTokenValue12345",
        "https://maps.example.com/tile?x=1&key=FAKEkeyVALUE987654",
        "https://storage.example.com/obj?X-Amz-Date=1&sig=FakeSignatureValue42",
    ],
)
def test_a_credential_in_a_url_query_is_flagged(url: str) -> None:
    assert [finding.rule for finding in scan(f"See {url} for details.")] == ["url_query_secret"]


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/search?q=something&page=2",
        "https://example.com/docs?key=short",
        "https://example.com/api?token=<your-token>",
        "https://example.com/keyboard?monkey=bananas123456",
    ],
)
def test_an_ordinary_url_query_is_not_flagged(url: str) -> None:
    assert scan(f"See {url} for details.") == []


def _db_password() -> str:
    return "".join(["Qw3", "Er7", "Ty1", "Ui9", "Op5", "As2"])


@pytest.mark.parametrize(
    "note",
    [
        "source_audio_remote: /home/someone/recordings/call-20261019-165257-remote.wav",
        "Photos are in raw/sources/house/35330419/video/frames, see them there.",
        "Original: ~/documents/2026-01_contract-signed-by-both-parties.pdf",
        "Wrote it in ./notes/2026-10-19-standup-with-the-team.md today.",
    ],
)
def test_a_filesystem_path_is_not_a_secret(note: str) -> None:
    assert scan(note) == []


@pytest.mark.parametrize(
    "note",
    [
        f"postgres://reader:{_db_password()}@db.internal:5432/app",
        f"curl -u admin:{_db_password()} https://api.internal/v1/items",
        f"curl --user admin:{_db_password()} https://api.internal/v1/items",
    ],
)
def test_a_password_inside_a_connection_string_is_flagged(note: str) -> None:
    assert len(scan(note)) == 1


@pytest.mark.parametrize(
    "note",
    [
        "postgres://reader:$DB_PASSWORD@db.internal:5432/app",
        "postgres://reader:${DB_PASSWORD}@db.internal:5432/app",
        "curl -u admin:$API_TOKEN https://api.internal/v1/items",
    ],
)
def test_a_connection_string_pointing_at_an_env_var_is_not_flagged(note: str) -> None:
    assert scan(note) == []


@pytest.mark.parametrize(
    "note",
    [
        "| **File** | [10 Identity/2024_id-card-portugal.pdf](https://cloud.example.test/f/52738) |",
        "source: nc:20 Company/20.01 Documents/2026-01_framework-agreement-signed-by-both.pdf",
        "| **Authentication code** | 19DF5633-FE2B-4334-8759-CC1A88B3FC0F |",
        "Report id bf06b4e9-e802-f111-84ab-005056a2c9a7.",
        "Wrote it in notes/2026-10-19-standup-with-the-team.md today.",
    ],
)
def test_a_lowercase_slug_or_a_uuid_is_not_a_secret(note: str) -> None:
    assert scan(note) == []


@pytest.mark.parametrize(
    "value",
    [
        "Zx9Kq2Lm8Rt4Vw7Yb1Nc6Hd3Fg5Jp0Qs",
        "ak_live_51H8xQ2eZvKYlo2C0abcdefghijklmnop",
        "sk_live_4eC39HqLyjWDarjtT1zdp7dc",
    ],
)
def test_a_mixed_case_key_is_still_a_secret(value: str) -> None:
    assert len(scan(value)) == 1


@pytest.mark.parametrize(
    "note",
    [
        "| **File** | [11 Home/Registration_Confirmation_AIRE_1742973614.2977.pdf](https://x/f/1) |",
        "See the scan at scans/2026-01_Contract-Signed-By-Both-Parties-Final-v2.pdf today.",
    ],
)
def test_a_file_name_is_not_a_secret(note: str) -> None:
    assert scan(note) == []


@pytest.mark.parametrize(
    "note",
    [
        "Secret: remoto_1 does not want the source keys in the coordinator database.",
        'The token: "" for the first page, and the next one for the rest.',
        "Password: first-page is the label of that field in the form.",
    ],
)
def test_a_keyword_followed_by_prose_is_not_an_assignment(note: str) -> None:
    assert scan(note) == []


def test_a_camel_case_identifier_is_not_a_secret() -> None:
    assert scan("`TestBucketsHardDeletionOnProjectHardDeletion` keeps covering the case.") == []
