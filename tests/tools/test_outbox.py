"""Tests for the outbox: the signed delivery from Hermes and the note it becomes."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

from second_brain.core.schema import VaultSchema, load_schema
from second_brain.tools.jobs import JobKind, JobPayload, JobStore
from second_brain.tools.outbox import (
    DONE_DIR,
    FAILED_DIR,
    OUTBOX_PATH,
    OutboxImporter,
    OutboxReceiver,
    Receipt,
    job_id_from_prompt,
    sources_in,
)
from second_brain.tools.registry import Registry
from second_brain.tools.server import OutboxEndpoint, build_app
from second_brain.tools.vault import Vault, VaultConfig

SECRET = "outbox-test-secret-not-a-real-one"
TOKEN = "tools-test-token-8b21"
KINDS = {
    "research": JobKind(route="background-research", description="Research on the web.", web=True),
    "develop_idea": JobKind(route="background-idea", description="Develop an idea.", web=False),
}
FOLLOWUP = "develop_idea"

SCHEMA = """# Schema

```yaml
language: en
zones:
  inbox: inbox
  raw: raw
  calls: calls
  topics: topics
  knowledge: knowledge
  ideas: ideas
  projects: projects
  people: people
  work: work
  documents: documents
  answers: answers
  drawings: drawings
  archive: archive
  _brain: _brain
  _meta: _meta
types:
  capture: something captured and not processed yet
  idea: an idea, with its state
```
"""


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, check=True)
    return result.stdout.strip()


@pytest.fixture
def vault_root(tmp_path: Path) -> Path:
    root = tmp_path / "vault"
    (root / "_meta").mkdir(parents=True)
    (root / "_meta" / "SCHEMA.md").write_text(SCHEMA, encoding="utf-8")
    (root / "raw" / "web").mkdir(parents=True)
    _git(root, "init", "-q")
    _git(root, "config", "user.name", "tester")
    _git(root, "config", "user.email", "tester@localhost")
    _git(root, "commit", "-q", "--allow-empty", "-m", "init")
    return root


@pytest.fixture
def vault(vault_root: Path) -> Vault:
    return Vault(VaultConfig(root=vault_root, quiet_seconds=0))


@pytest.fixture
def schema(vault_root: Path) -> VaultSchema:
    return load_schema(vault_root)


@pytest.fixture
def store(tmp_path: Path) -> JobStore:
    return JobStore(tmp_path / "jobs.sqlite3")


@pytest.fixture
def outbox_dir(tmp_path: Path) -> Path:
    return tmp_path / "outbox"


@pytest.fixture
def secret_file(tmp_path: Path) -> Path:
    path = tmp_path / "outbox.secret"
    path.write_text(SECRET + "\n", encoding="utf-8")
    return path


def _research_job(store: JobStore, title: str = "Tidal energy in the North Sea") -> str:
    return store.enqueue(
        "research",
        JobPayload(
            title=title, question="How does tidal energy work in the North Sea?", note_path="ideas/tidal.md"
        ),
        "user-1",
        "session-1",
    )


def _dispatched_job(store: JobStore, title: str = "Tidal energy in the North Sea") -> str:
    job_id = _research_job(store, title)
    store.claim()
    store.dispatch(job_id)
    return job_id


def _delivery(
    job_id: str,
    reply: str = "Tidal stream turbines use the current, not a dam. See https://example.org/tidal.",
    *,
    event: str = "post_llm_call",
    platform: str = "webhook",
    delivery_id: str = "d1e2f3a4b5c6",
    user_message: str | None = None,
) -> dict[str, Any]:
    return {
        "hook_event_name": event,
        "profile": "default",
        "tool_name": None,
        "tool_input": None,
        "session_id": "session-1",
        "cwd": "/workspace",
        "extra": {
            "platform": platform,
            "assistant_response": reply,
            "user_message": user_message
            or f"Background research job {job_id}. Tidal energy\n\nHow does tidal energy work?",
            "session_id": "session-1",
        },
        "delivery_id": delivery_id,
        "timestamp": "2026-02-19T09:00:00+00:00",
    }


def _body(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


def _signed(body: bytes, secret: str = SECRET) -> dict[str, str]:
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return {
        "X-Hermes-Signature-256": f"sha256={digest}",
        "X-Hermes-Event": "post_llm_call",
        "Content-Type": "application/json",
    }


def _receiver(outbox_dir: Path, secret_file: Path, store: JobStore) -> OutboxReceiver:
    return OutboxReceiver(outbox_dir, secret_file, store, KINDS)


def _importer(outbox_dir: Path, vault: Vault, schema: VaultSchema, store: JobStore) -> OutboxImporter:
    return OutboxImporter(outbox_dir, vault, schema, store, KINDS, FOLLOWUP)


def _stored(outbox_dir: Path) -> list[Path]:
    return sorted(outbox_dir.glob("*.json"))


def _frontmatter(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    assert text.startswith("---\n")
    head = text.split("---\n", 2)[1]
    return yaml.safe_load(head) or {}


def test_a_signed_delivery_from_a_research_route_is_stored(
    outbox_dir: Path, secret_file: Path, store: JobStore
) -> None:
    job_id = _dispatched_job(store)
    body = _body(_delivery(job_id))
    receipt = _receiver(outbox_dir, secret_file, store).handle(_signed(body), body)
    assert receipt.status == 202
    assert receipt.code == "stored"
    stored = _stored(outbox_dir)
    assert [path.name for path in stored] == ["d1e2f3a4b5c6.json"]
    assert json.loads(stored[0].read_bytes())["hook_event_name"] == "post_llm_call"


def test_the_same_delivery_twice_is_stored_once(outbox_dir: Path, secret_file: Path, store: JobStore) -> None:
    job_id = _dispatched_job(store)
    body = _body(_delivery(job_id))
    receiver = _receiver(outbox_dir, secret_file, store)
    assert receiver.handle(_signed(body), body).status == 202
    assert receiver.handle(_signed(body), body).code == "duplicate"
    assert len(_stored(outbox_dir)) == 1


@pytest.mark.parametrize(
    ("headers", "status", "code"),
    [
        ({}, 401, "unsigned"),
        ({"X-Hermes-Signature-256": "sha256=00"}, 401, "bad_signature"),
    ],
)
def test_a_missing_or_wrong_signature_is_refused(
    outbox_dir: Path,
    secret_file: Path,
    store: JobStore,
    headers: dict[str, str],
    status: int,
    code: str,
) -> None:
    job_id = _dispatched_job(store)
    body = _body(_delivery(job_id))
    receipt = _receiver(outbox_dir, secret_file, store).handle(headers, body)
    assert (receipt.status, receipt.code) == (status, code)
    assert _stored(outbox_dir) == []


def test_a_delivery_signed_with_another_secret_is_refused(
    outbox_dir: Path, secret_file: Path, store: JobStore
) -> None:
    job_id = _dispatched_job(store)
    body = _body(_delivery(job_id))
    receipt = _receiver(outbox_dir, secret_file, store).handle(_signed(body, "another-secret"), body)
    assert receipt.status == 401
    assert _stored(outbox_dir) == []


def test_an_unreadable_secret_refuses_instead_of_accepting(
    outbox_dir: Path, tmp_path: Path, store: JobStore
) -> None:
    job_id = _dispatched_job(store)
    body = _body(_delivery(job_id))
    missing = tmp_path / "not-there.secret"
    receipt = _receiver(outbox_dir, missing, store).handle(_signed(body), body)
    assert receipt.status == 503
    assert _stored(outbox_dir) == []


@pytest.mark.parametrize(
    "overrides",
    [
        {"platform": "telegram"},
        {"event": "on_session_end"},
        {"user_message": "What is the weather?"},
    ],
)
def test_a_signed_delivery_that_is_not_a_research_result_is_ignored(
    outbox_dir: Path, secret_file: Path, store: JobStore, overrides: dict[str, str]
) -> None:
    """The outbound hook fires on every turn: a signed non-research delivery is normal, not an error."""
    job_id = _dispatched_job(store)
    body = _body(_delivery(job_id, **overrides))
    receipt = _receiver(outbox_dir, secret_file, store).handle(_signed(body), body)
    assert receipt.status == 200
    assert receipt.code == "ignored"
    assert receipt.message
    assert _stored(outbox_dir) == []


def test_a_wrong_event_header_is_still_refused(outbox_dir: Path, secret_file: Path, store: JobStore) -> None:
    job_id = _dispatched_job(store)
    body = _body(_delivery(job_id))
    headers = _signed(body) | {"X-Hermes-Event": "on_session_end"}
    receipt = _receiver(outbox_dir, secret_file, store).handle(headers, body)
    assert (receipt.status, receipt.code) == (403, "wrong_event")
    assert _stored(outbox_dir) == []


def test_a_job_kind_without_the_web_is_ignored(outbox_dir: Path, secret_file: Path, store: JobStore) -> None:
    job_id = store.enqueue(
        "develop_idea", JobPayload(title="An idea", question="Develop it."), "user-1", "s1"
    )
    store.claim()
    store.dispatch(job_id)
    body = _body(_delivery(job_id))
    receipt = _receiver(outbox_dir, secret_file, store).handle(_signed(body), body)
    assert receipt.status == 200
    assert "not a research job" in receipt.message
    assert _stored(outbox_dir) == []


def test_a_delivery_for_an_unknown_job_is_ignored(
    outbox_dir: Path, secret_file: Path, store: JobStore
) -> None:
    body = _body(_delivery("0" * 32))
    receipt = _receiver(outbox_dir, secret_file, store).handle(_signed(body), body)
    assert receipt.status == 200
    assert "no job" in receipt.message
    assert _stored(outbox_dir) == []


def test_a_reply_that_is_not_ours_is_ignored(outbox_dir: Path, secret_file: Path, store: JobStore) -> None:
    job_id = _dispatched_job(store)
    body = _body(_delivery(job_id, user_message="What is the weather?"))
    receipt = _receiver(outbox_dir, secret_file, store).handle(_signed(body), body)
    assert (receipt.status, receipt.code) == (200, "ignored")
    assert _stored(outbox_dir) == []


def test_the_job_id_is_read_from_the_first_line_of_the_prompt() -> None:
    job_id = "a" * 32
    assert job_id_from_prompt(f"Background research job {job_id}. Title\n\nQuestion") == job_id
    assert job_id_from_prompt("Background research job nope. Title") is None
    assert job_id_from_prompt(None) is None


def test_import_creates_an_untrusted_raw_note_with_sources_and_the_job_id(
    outbox_dir: Path, secret_file: Path, store: JobStore, vault: Vault, schema: VaultSchema
) -> None:
    job_id = _dispatched_job(store)
    reply = (
        "Tidal stream turbines use the current. Sources: https://example.org/tidal, https://example.org/x."
    )
    body = _body(_delivery(job_id, reply))
    assert _receiver(outbox_dir, secret_file, store).handle(_signed(body), body).status == 202
    results = _importer(outbox_dir, vault, schema, store).sweep()
    assert len(results) == 1 and results[0].ok
    note = vault.root / str(results[0].note)
    assert note.parent == vault.root / "raw" / "web"
    assert note.name.endswith("-tidal-energy-in-the-north-sea.md")
    assert " " not in note.name
    front = _frontmatter(note)
    assert front["type"] == "capture"
    assert front["trust"] == "untrusted"
    assert front["job"] == job_id
    assert front["created"]
    assert front["sources"] == ["https://example.org/tidal", "https://example.org/x"]
    assert "tidal stream turbines" in note.read_text(encoding="utf-8").lower()
    assert _stored(outbox_dir) == []
    assert (outbox_dir / DONE_DIR / "d1e2f3a4b5c6.json").is_file()


def test_the_write_is_committed_with_the_job_provenance(
    outbox_dir: Path, secret_file: Path, store: JobStore, vault: Vault, schema: VaultSchema
) -> None:
    job_id = _dispatched_job(store)
    body = _body(_delivery(job_id))
    _receiver(outbox_dir, secret_file, store).handle(_signed(body), body)
    result = _importer(outbox_dir, vault, schema, store).sweep()[0]
    message = _git(vault.root, "log", "-1", "--format=%B")
    assert f"Requested-By: job:{job_id}" in message
    assert "Source-Integrity: untrusted" in message
    assert result.note is not None


def test_importing_finishes_the_research_job_and_queues_the_follow_up(
    outbox_dir: Path, secret_file: Path, store: JobStore, vault: Vault, schema: VaultSchema
) -> None:
    job_id = _dispatched_job(store)
    body = _body(_delivery(job_id))
    _receiver(outbox_dir, secret_file, store).handle(_signed(body), body)
    result = _importer(outbox_dir, vault, schema, store).sweep()[0]
    finished = store.get(job_id)
    assert finished is not None and finished.status == "finished"
    followups = store.list("queued")
    assert len(followups) == 1
    followup = followups[0]
    assert followup.kind == FOLLOWUP
    assert result.note in followup.payload.question
    assert "How does tidal energy work in the North Sea?" in followup.payload.question
    assert followup.payload.note_path == "ideas/tidal.md"
    # The vault route serves other jobs too: the follow-up is told where to write and which skill part.
    assert "Write the final note at ideas/tidal.md" in followup.payload.question
    assert "research skill, side three" in followup.payload.question
    assert followup.requested_by == f"job:{job_id}"


def test_the_follow_up_question_stays_within_the_limit(
    outbox_dir: Path, secret_file: Path, store: JobStore, vault: Vault, schema: VaultSchema
) -> None:
    job_id = store.enqueue(
        "research",
        JobPayload(title="Long one", question="q" * 2000, note_path="ideas/long.md"),
        "user-1",
        "session-1",
    )
    store.claim()
    store.dispatch(job_id)
    body = _body(_delivery(job_id))
    _receiver(outbox_dir, secret_file, store).handle(_signed(body), body)
    _importer(outbox_dir, vault, schema, store).sweep()
    followup = store.list("queued")[0]
    assert len(followup.payload.question) <= 2000
    assert "raw/web/" in followup.payload.question


def test_a_secret_in_the_result_goes_to_failed_and_never_reaches_the_vault(
    outbox_dir: Path, secret_file: Path, store: JobStore, vault: Vault, schema: VaultSchema
) -> None:
    job_id = _dispatched_job(store)
    fake_key = "sk-" + "A1b2C3d4" * 6
    body = _body(_delivery(job_id, f"The key is {fake_key} apparently."))
    _receiver(outbox_dir, secret_file, store).handle(_signed(body), body)
    results = _importer(outbox_dir, vault, schema, store).sweep()
    assert len(results) == 1 and not results[0].ok
    assert "secret_detected" in (results[0].error or "")
    assert list((vault.root / "raw" / "web").glob("*.md")) == []
    failed = outbox_dir / FAILED_DIR
    assert (failed / "d1e2f3a4b5c6.json").is_file()
    reason = (failed / "d1e2f3a4b5c6.reason.txt").read_text(encoding="utf-8")
    assert "secret_detected" in reason
    assert fake_key not in reason
    assert _stored(outbox_dir) == []
    # The job did not produce a note, so it is not finished.
    assert store.get(job_id).status == "dispatched"  # type: ignore[union-attr]


def test_broken_json_goes_to_failed_without_stopping_the_sweep(
    outbox_dir: Path, store: JobStore, vault: Vault, schema: VaultSchema
) -> None:
    outbox_dir.mkdir(parents=True, exist_ok=True)
    (outbox_dir / "broken.json").write_text("{not json", encoding="utf-8")
    results = _importer(outbox_dir, vault, schema, store).sweep()
    assert len(results) == 1 and not results[0].ok
    assert (outbox_dir / FAILED_DIR / "broken.json").is_file()
    assert (outbox_dir / FAILED_DIR / "broken.reason.txt").is_file()


def test_a_second_result_for_the_same_title_does_not_overwrite_the_first(
    outbox_dir: Path, secret_file: Path, store: JobStore, vault: Vault, schema: VaultSchema
) -> None:
    first = _dispatched_job(store, "Same title")
    body = _body(_delivery(first, "first reply"))
    _receiver(outbox_dir, secret_file, store).handle(_signed(body), body)
    _importer(outbox_dir, vault, schema, store).sweep()
    second = store.enqueue(
        "research", JobPayload(title="Same title", question="Again?"), "user-1", "session-1"
    )
    store.claim()
    store.dispatch(second)
    body = _body(_delivery(second, "second reply", delivery_id="ffffffffffff"))
    _receiver(outbox_dir, secret_file, store).handle(_signed(body), body)
    results = _importer(outbox_dir, vault, schema, store).sweep()
    assert results[0].ok
    notes = sorted((vault.root / "raw" / "web").glob("*.md"))
    assert len(notes) == 2
    texts = [note.read_text(encoding="utf-8") for note in notes]
    assert any("first reply" in text for text in texts)
    assert any("second reply" in text for text in texts)
    assert notes[0].stem.endswith(f"-{second[:8]}")


def test_a_title_with_accents_and_punctuation_becomes_a_slug(
    outbox_dir: Path, secret_file: Path, store: JobStore, vault: Vault, schema: VaultSchema
) -> None:
    job_id = _dispatched_job(store, "Vantaggi della pacciamatura in un orto sul balcone — perché?")
    body = _body(_delivery(job_id))
    _receiver(outbox_dir, secret_file, store).handle(_signed(body), body)
    result = _importer(outbox_dir, vault, schema, store).sweep()[0]
    assert result.ok
    assert result.note is not None
    folder, name = result.note.rsplit("/", 1)
    assert folder == "raw/web"
    assert name.startswith("2026-") and name[10] == "-"
    assert name.endswith("-vantaggi-della-pacciamatura-in-un-orto-sul-balcone-perche.md")


def test_a_title_without_letters_falls_back_to_research(
    outbox_dir: Path, secret_file: Path, store: JobStore, vault: Vault, schema: VaultSchema
) -> None:
    job_id = _dispatched_job(store, "!!! ???")
    body = _body(_delivery(job_id))
    _receiver(outbox_dir, secret_file, store).handle(_signed(body), body)
    result = _importer(outbox_dir, vault, schema, store).sweep()[0]
    assert result.note is not None and result.note.endswith("-research.md")


def test_a_title_with_a_separator_cannot_escape_the_web_folder(
    outbox_dir: Path, secret_file: Path, store: JobStore, vault: Vault, schema: VaultSchema
) -> None:
    job_id = _dispatched_job(store, "../../etc/passwd")
    body = _body(_delivery(job_id))
    _receiver(outbox_dir, secret_file, store).handle(_signed(body), body)
    result = _importer(outbox_dir, vault, schema, store).sweep()[0]
    assert result.ok
    assert result.note is not None and result.note.startswith("raw/web/")
    assert ".." not in result.note


def test_sources_keeps_order_and_drops_trailing_punctuation() -> None:
    text = "See (https://a.example/x), then https://b.example/y. And https://a.example/x again."
    assert sources_in(text) == ["https://a.example/x", "https://b.example/y"]
    assert sources_in("no links here") == []


def test_bare_domains_land_after_the_urls() -> None:
    text = "Sources: compo-hobby.it, extension.umn.edu. See https://example.org/tidal too."
    assert sources_in(text) == ["https://example.org/tidal", "compo-hobby.it", "extension.umn.edu"]


def test_a_vault_file_name_is_not_a_source() -> None:
    text = "I read note.md and report.pdf, then example.org."
    assert sources_in(text) == ["example.org"]


def test_a_domain_already_inside_a_url_is_not_repeated() -> None:
    text = "See https://example.org/tidal and example.org again."
    assert sources_in(text) == ["https://example.org/tidal"]
    assert sources_in("https://docs.example.org/x then example.org") == ["https://docs.example.org/x"]
    # A different host that only contains the domain is still a source.
    assert sources_in("https://myexample.org/x then example.org") == [
        "https://myexample.org/x",
        "example.org",
    ]


def test_sources_stop_at_the_cap() -> None:
    text = " ".join(f"site{i}.example" for i in range(40))
    assert len(sources_in(text)) == 20


class _Sink:
    """A downstream ASGI app that records that it was reached and answers 200."""

    def __init__(self) -> None:
        self.calls = 0

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        self.calls += 1
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"{}"})


def _call(
    app: Any, path: str, body: bytes, headers: dict[str, str], method: str = "POST"
) -> tuple[int, dict[str, Any]]:
    sent: list[dict[str, Any]] = []

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    read = {"done": False}

    async def receive() -> dict[str, Any]:
        if read["done"]:
            return {"type": "http.disconnect"}
        read["done"] = True
        return {"type": "http.request", "body": body, "more_body": False}

    scope = {
        "type": "http",
        "path": path,
        "method": method,
        "headers": [(key.lower().encode(), value.encode()) for key, value in headers.items()],
    }
    asyncio.run(app(scope, receive, send))
    return sent[0]["status"], json.loads(sent[1]["body"])


def test_the_endpoint_stores_a_signed_post_and_leaves_the_rest_to_the_app(
    outbox_dir: Path, secret_file: Path, store: JobStore
) -> None:
    job_id = _dispatched_job(store)
    sink = _Sink()
    endpoint = OutboxEndpoint(sink, _receiver(outbox_dir, secret_file, store))
    body = _body(_delivery(job_id))
    status, answer = _call(endpoint, OUTBOX_PATH, body, _signed(body))
    assert status == 202 and answer["ok"] is True
    assert sink.calls == 0
    # Another path is not ours: it goes to the app behind, which is where the MCP server lives.
    _call(endpoint, "/mcp", b"{}", {})
    assert sink.calls == 1


def test_the_endpoint_refuses_a_wrong_signature_with_401(
    outbox_dir: Path, secret_file: Path, store: JobStore
) -> None:
    job_id = _dispatched_job(store)
    sink = _Sink()
    endpoint = OutboxEndpoint(sink, _receiver(outbox_dir, secret_file, store))
    body = _body(_delivery(job_id))
    status, answer = _call(endpoint, OUTBOX_PATH, body, {"X-Hermes-Signature-256": "sha256=00"})
    assert status == 401
    assert answer["code"] == "bad_signature"
    assert sink.calls == 0


def test_the_endpoint_refuses_a_get(outbox_dir: Path, secret_file: Path, store: JobStore) -> None:
    endpoint = OutboxEndpoint(_Sink(), _receiver(outbox_dir, secret_file, store))
    status, answer = _call(endpoint, OUTBOX_PATH, b"", {}, method="GET")
    assert status == 405
    assert answer["code"] == "method_not_allowed"


def test_the_outbox_is_not_behind_the_mcp_bearer_token(
    outbox_dir: Path, secret_file: Path, store: JobStore
) -> None:
    """Hermes cannot send the MCP token: the signature is what protects this route."""
    job_id = _dispatched_job(store)
    app = build_app(Registry(), TOKEN, _receiver(outbox_dir, secret_file, store))
    body = _body(_delivery(job_id))
    status, _ = _call(app, OUTBOX_PATH, body, _signed(body))
    assert status == 202
    # And everything else on the port still wants the token.
    status, answer = _call(app, "/mcp", b"{}", {})
    assert status == 401 and answer["code"] == "unauthorized"


def test_a_receipt_reports_its_own_success() -> None:
    assert Receipt(202, "stored", "ok").status == 202


def test_the_endpoint_answers_200_to_a_turn_that_is_not_a_research_result(
    outbox_dir: Path, secret_file: Path, store: JobStore
) -> None:
    """Every finished turn reaches the hook, so the app must not see a failure for the normal ones."""
    job_id = _dispatched_job(store)
    sink = _Sink()
    endpoint = OutboxEndpoint(sink, _receiver(outbox_dir, secret_file, store))
    body = _body(_delivery(job_id, platform="telegram"))
    status, answer = _call(endpoint, OUTBOX_PATH, body, _signed(body))
    assert status == 200
    assert answer["ok"] is True and answer["code"] == "ignored"
    assert _stored(outbox_dir) == []
