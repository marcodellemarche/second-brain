import pytest

from second_brain.guard.sanitize import PATH_PLACEHOLDER, sanitize_reply


def test_media_tag_removed() -> None:
    text = "Here you go MEDIA:/etc/secrets.txt"
    cleaned, removed = sanitize_reply(text, None)
    assert cleaned == "Here you go"
    assert removed == ["MEDIA:/etc/secrets.txt"]


def test_media_tag_on_its_own_line_removed() -> None:
    cleaned, removed = sanitize_reply("Done.\n\nMEDIA:/var/tmp/report.pdf\n", None)
    assert cleaned == "Done.\n\n\n"
    assert removed == ["MEDIA:/var/tmp/report.pdf"]


def test_media_inside_allowed_dir_kept(tmp_path) -> None:
    media = tmp_path / "media"
    media.mkdir()
    tag = f"MEDIA:{media}/note.pdf"
    cleaned, removed = sanitize_reply(f"Take this {tag}", str(media))
    assert cleaned == f"Take this {tag}"
    assert removed == []


def test_media_outside_allowed_dir_removed(tmp_path) -> None:
    media = tmp_path / "media"
    media.mkdir()
    outside = tmp_path / "other" / "note.pdf"
    cleaned, removed = sanitize_reply(f"Take this MEDIA:{outside}", str(media))
    assert cleaned == "Take this"
    assert removed == [f"MEDIA:{outside}"]


def test_media_escape_with_dotdot_removed(tmp_path) -> None:
    media = tmp_path / "media"
    media.mkdir()
    escape = f"MEDIA:{media}/../secret.txt"
    cleaned, removed = sanitize_reply(escape, str(media))
    assert cleaned == ""
    assert removed == [escape]


def test_media_with_spaces_and_quotes(tmp_path) -> None:
    media = tmp_path / "media"
    media.mkdir()
    inside = media / "my report.pdf"
    cleaned, _ = sanitize_reply(f'MEDIA:"{inside}"', str(media))
    assert cleaned == f'MEDIA:"{inside}"'
    cleaned, removed = sanitize_reply('MEDIA:"/etc/my report.pdf"', str(media))
    assert cleaned == ""
    assert removed == ['MEDIA:"/etc/my report.pdf"']


def test_media_with_hermes_marker_removed(tmp_path) -> None:
    media = tmp_path / "media"
    media.mkdir()
    cleaned, removed = sanitize_reply("Here [[as_document]]MEDIA:/etc/a.pdf", str(media))
    assert cleaned == "Here [[as_document]]"
    assert removed == ["MEDIA:/etc/a.pdf"]


def test_media_with_relative_path_removed(tmp_path) -> None:
    media = tmp_path / "media"
    media.mkdir()
    cleaned, removed = sanitize_reply("MEDIA:media/report.pdf", str(media))
    assert cleaned == ""
    assert removed == ["MEDIA:media/report.pdf"]


def test_bare_absolute_paths_removed() -> None:
    cleaned, removed = sanitize_reply("Saved to /home/user/vault/note.md, done.", None)
    assert cleaned == f"Saved to {PATH_PLACEHOLDER}, done."
    assert removed == ["/home/user/vault/note.md"]

    cleaned, removed = sanitize_reply("Look at ~/notes/ideas.md", None)
    assert cleaned == f"Look at {PATH_PLACEHOLDER}"
    assert removed == ["~/notes/ideas.md"]


def test_bare_path_inside_allowed_dir_kept(tmp_path) -> None:
    media = tmp_path / "media"
    media.mkdir()
    inside = f"{media}/photo.jpg"
    cleaned, removed = sanitize_reply(f"See {inside}", str(media))
    assert cleaned == f"See {inside}"
    assert removed == []


def test_bare_path_escape_with_dotdot_removed(tmp_path) -> None:
    media = tmp_path / "media"
    media.mkdir()
    cleaned, removed = sanitize_reply(f"See {media}/../../etc/passwd", str(media))
    assert cleaned == f"See {PATH_PLACEHOLDER}"
    assert removed == [f"{media}/../../etc/passwd"]


def test_urls_untouched() -> None:
    text = "Read https://example.com/docs/page and http://host/a/b?c=1#d"
    cleaned, removed = sanitize_reply(text, None)
    assert cleaned == text
    assert removed == []


def test_url_followed_by_a_path() -> None:
    cleaned, removed = sanitize_reply("https://example.com/a then /etc/hosts", None)
    assert cleaned == f"https://example.com/a then {PATH_PLACEHOLDER}"
    assert removed == ["/etc/hosts"]


def test_markdown_links_and_wikilinks_untouched() -> None:
    text = "See [[Note]] and [text](note.md) and [other](../people/anna.md)"
    cleaned, removed = sanitize_reply(text, None)
    assert cleaned == text
    assert removed == []


def test_plain_text_untouched() -> None:
    text = "Nothing to strip here. Ratio 1/2, 50% off, a/b testing."
    cleaned, removed = sanitize_reply(text, None)
    assert cleaned == text
    assert removed == []


def test_both_kinds_removed_and_reported() -> None:
    text = "Path /tmp/x.txt and tag MEDIA:/tmp/y.png"
    cleaned, removed = sanitize_reply(text, None)
    assert cleaned == f"Path {PATH_PLACEHOLDER} and tag"
    assert removed == ["/tmp/x.txt", "MEDIA:/tmp/y.png"]


@pytest.mark.parametrize("allowed", [None, ""])
def test_no_allowed_dir_means_nothing_is_kept(allowed: str | None) -> None:
    cleaned, removed = sanitize_reply("MEDIA:/tmp/a.png", allowed)
    assert cleaned == ""
    assert removed == ["MEDIA:/tmp/a.png"]


@pytest.mark.parametrize("command", ["/new", "/reset", "/undo", "/sethome", "/start"])
def test_chat_commands_untouched(command: str) -> None:
    cleaned, removed = sanitize_reply(f"Scrivi {command} per ripartire pulito", None)
    assert cleaned == f"Scrivi {command} per ripartire pulito"
    assert removed == []


def test_chat_command_at_end_of_sentence_untouched() -> None:
    cleaned, removed = sanitize_reply("Usa /new.", None)
    assert cleaned == "Usa /new."
    assert removed == []


def test_several_chat_commands_untouched() -> None:
    text = "usa /new e poi /undo"
    cleaned, removed = sanitize_reply(text, None)
    assert cleaned == text
    assert removed == []


@pytest.mark.parametrize(
    "path",
    ["/vault/idee/x.md", "/etc/passwd", "/config.yaml", "~/secret.txt"],
)
def test_paths_still_removed(path: str) -> None:
    cleaned, removed = sanitize_reply(f"Apri {path} ora", None)
    assert cleaned == f"Apri {PATH_PLACEHOLDER} ora"
    assert removed == [path]


def test_commands_kept_but_real_paths_removed() -> None:
    text = "Scrivi /new, non /vault/idee/x.md ne /config.yaml"
    cleaned, removed = sanitize_reply(text, None)
    assert cleaned == f"Scrivi /new, non {PATH_PLACEHOLDER} ne {PATH_PLACEHOLDER}"
    assert removed == ["/vault/idee/x.md", "/config.yaml"]
