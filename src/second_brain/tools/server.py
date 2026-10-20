"""The MCP server (brain_tools) over streamable HTTP, plus the outbox endpoint beside it.

Every registered tool becomes an MCP tool whose input schema is the tool's pydantic params plus
one extra optional `guard` object. brain_guard fills `guard` before the call leaves Hermes; a call
that arrives without it is refused, so a caller that skipped the guard cannot write. The HTTP layer
refuses a request without the bearer token before it reaches the MCP handler.

The one route outside the bearer token is `POST /outbox`, where Hermes posts the reply of a research
run: Hermes cannot send a bearer token, so that route verifies the outbound-hook signature instead
(`tools/outbox.py`). Everything else on the port still needs the token.
"""

from __future__ import annotations

import hmac
import inspect
import json
import logging
import threading
import time
from collections.abc import Awaitable, Callable, Mapping
from typing import Annotated, Any, Literal, cast

import uvicorn
from mcp.server.fastmcp import Context, FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import BaseModel, Field, ValidationError, WithJsonSchema

from second_brain.core.schema import load_schema
from second_brain.tools.config import ToolsConfig, build_registry
from second_brain.tools.outbox import (
    MAX_BODY_BYTES,
    OUTBOX_PATH,
    OutboxImporter,
    OutboxReceiver,
    Receipt,
)
from second_brain.tools.registry import Registry
from second_brain.tools.staging import StagingImporter
from second_brain.tools.vault import Provenance, Vault, VaultError

log = logging.getLogger(__name__)

INTEGRITY_VALUES: tuple[str, ...] = ("trusted", "untrusted")

OUTBOX_IMPORT_SECONDS = 30.0
STAGING_IMPORT_SECONDS = 60.0

ASGIApp = Callable[..., Awaitable[None]]


def _authorized(token: str, authorization: str | None) -> bool:
    """Compare the bearer token in constant time, whatever its length."""
    expected = f"Bearer {token}".encode()
    provided = (authorization or "").encode()
    return hmac.compare_digest(provided, expected)


def _guard_fields(guard: Any) -> dict[str, str] | None:
    """Validate the guard object brain_guard injects. None means it is missing or malformed."""
    if not isinstance(guard, Mapping):
        return None
    requested_by = guard.get("requested_by")
    session = guard.get("session")
    source_integrity = guard.get("source_integrity")
    approval = guard.get("approval", "none")
    if not isinstance(requested_by, str) or not requested_by.strip():
        return None
    if not isinstance(session, str) or not session.strip():
        return None
    if not isinstance(source_integrity, str) or source_integrity not in INTEGRITY_VALUES:
        return None
    if not isinstance(approval, str) or not approval.strip():
        return None
    return {
        "requested_by": requested_by,
        "session": session,
        "source_integrity": source_integrity,
        "approval": approval,
    }


def dispatch(
    registry: Registry,
    token: str,
    authorization: str | None,
    name: str,
    arguments: Mapping[str, Any],
) -> dict[str, Any]:
    """Authorize, resolve, validate and run one tool call. Never raises: errors are results."""
    if not _authorized(token, authorization):
        return {"ok": False, "code": "unauthorized", "message": "missing or invalid bearer token"}
    try:
        tool = registry.get(name)
    except KeyError:
        return {"ok": False, "code": "unknown_tool", "message": f"no tool named {name}"}
    fields = _guard_fields(arguments.get("guard"))
    if fields is None:
        return {
            "ok": False,
            "code": "no_guard",
            "message": "the call did not come through the guard, refusing it",
        }
    payload = {key: value for key, value in arguments.items() if key != "guard"}
    try:
        params = tool.spec.params.model_validate(payload)
    except ValidationError as exc:
        return {"ok": False, "code": "bad_params", "message": exc.errors(include_url=False).__repr__()}
    provenance = Provenance(
        requested_by=fields["requested_by"],
        reason=str(getattr(params, "reason", "") or ""),
        source_integrity=cast(Literal["trusted", "untrusted"], fields["source_integrity"]),
        session=fields["session"],
        approval=fields["approval"],
    )
    try:
        return tool.handler(params, provenance)
    except VaultError as exc:
        return {"ok": False, "code": exc.code, "message": exc.message}


def _signature(params: type[BaseModel]) -> inspect.Signature:
    """The MCP input schema: the pydantic params, plus the guard object brain_guard injects."""
    fields = [
        inspect.Parameter(
            name,
            inspect.Parameter.KEYWORD_ONLY,
            annotation=Annotated[(field.annotation, *field.metadata, Field(description=field.description))],
            default=inspect.Parameter.empty if field.is_required() else field.default,
        )
        for name, field in params.model_fields.items()
    ]
    fields.append(
        inspect.Parameter(
            "guard",
            inspect.Parameter.KEYWORD_ONLY,
            annotation=Annotated[
                dict[str, Any] | None,
                WithJsonSchema({"type": "object", "additionalProperties": True}),
                Field(description="Injected by brain_guard. Reserved: a call without it is refused."),
            ],
            default=None,
        )
    )
    return inspect.Signature(fields)


def _authorization(ctx: Context[Any, Any, Any]) -> str | None:
    """The Authorization header of the request this tool call arrived on."""
    try:
        request = ctx.request_context.request
    except (AttributeError, LookupError, ValueError):
        return None
    headers = getattr(request, "headers", None)
    return headers.get("authorization") if headers is not None else None


def tool_callable(registry: Registry, token: str, name: str) -> Callable[..., dict[str, Any]]:
    """The MCP tool for one registered tool.

    Its signature is the input schema the model sees, and the bearer token comes from the
    `Authorization` header of the request the call arrived on.
    """
    tool = registry.get(name)

    def call(*, ctx: Context[Any, Any, Any], **arguments: Any) -> dict[str, Any]:
        return dispatch(registry, token, _authorization(ctx), tool.spec.name, arguments)

    # FastMCP builds the input schema from the signature, so it must be the spec's params.
    call.__name__ = tool.spec.name
    call.__qualname__ = tool.spec.name
    call.__doc__ = tool.spec.description
    call.__annotations__["ctx"] = Context
    call.__signature__ = _signature(tool.spec.params)  # type: ignore[attr-defined]
    return call


def build_server(registry: Registry, token: str) -> FastMCP:
    """A FastMCP server with every registered tool, served over streamable HTTP."""
    server = FastMCP(
        "brain_tools",
        stateless_http=True,
        json_response=True,
        # Auth is the bearer token below; the server only listens on the internal network.
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )
    for spec in registry.specs():
        server.add_tool(
            tool_callable(registry, token, spec.name),
            name=spec.name,
            description=spec.description,
            structured_output=False,
        )
    return server


class BearerAuth:
    """ASGI middleware: refuse any HTTP request whose bearer token does not match."""

    def __init__(self, app: ASGIApp, token: str) -> None:
        self._app = app
        self._token = token

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] == "http" and not _authorized(self._token, _header(scope, b"authorization")):
            body = json.dumps({"ok": False, "code": "unauthorized", "message": "unauthorized"}).encode()
            await send(
                {
                    "type": "http.response.start",
                    "status": 401,
                    "headers": [
                        (b"content-type", b"application/json"),
                        (b"content-length", str(len(body)).encode()),
                    ],
                }
            )
            await send({"type": "http.response.body", "body": body})
            return
        await self._app(scope, receive, send)


def _header(scope: dict[str, Any], name: bytes) -> str | None:
    for key, value in scope.get("headers", []):
        if key.lower() == name:
            return value.decode("latin-1")
    return None


def _scope_headers(scope: Mapping[str, Any]) -> dict[str, str]:
    """Every header of one request, so the outbox can look one up by name."""
    return {key.decode("latin-1"): value.decode("latin-1") for key, value in scope.get("headers", [])}


async def _read_body(receive: Any, limit: int) -> bytes:
    """Read the request body, stopping one byte past the limit so an oversized one is refused."""
    chunks: list[bytes] = []
    total = 0
    while True:
        message = await receive()
        if message.get("type") != "http.request":
            break
        chunk = message.get("body", b"")
        chunks.append(chunk)
        total += len(chunk)
        if total > limit or not message.get("more_body", False):
            break
    return b"".join(chunks)


async def _answer(send: Any, status: int, body: Mapping[str, Any]) -> None:
    payload = json.dumps(dict(body)).encode()
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(payload)).encode()),
            ],
        }
    )
    await send({"type": "http.response.body", "body": payload})


class OutboxEndpoint:
    """ASGI middleware: `POST /outbox` goes to the receiver, everything else to the app.

    The receiver answers with a `Receipt`, which becomes the status and a small JSON body. A request
    to another path, or with another method, is not touched here: the app behind decides.
    """

    def __init__(self, app: ASGIApp, receiver: OutboxReceiver, path: str = OUTBOX_PATH) -> None:
        self._app = app
        self._receiver = receiver
        self._path = path

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http" or scope.get("path") != self._path:
            await self._app(scope, receive, send)
            return
        if scope.get("method") != "POST":
            await _answer(send, 405, {"ok": False, "code": "method_not_allowed"})
            return
        body = await _read_body(receive, MAX_BODY_BYTES)
        receipt: Receipt = self._receiver.handle(_scope_headers(scope), body)
        await _answer(
            send,
            receipt.status,
            {"ok": 200 <= receipt.status < 300, "code": receipt.code, "message": receipt.message},
        )


def build_app(registry: Registry, token: str, outbox: OutboxReceiver | None = None) -> ASGIApp:
    """The ASGI app: the MCP server behind bearer authentication, plus the outbox when configured."""
    app: ASGIApp = BearerAuth(build_server(registry, token).streamable_http_app(), token)
    if outbox is None:
        return app
    return OutboxEndpoint(app, outbox)


def main() -> None:
    """Serve the tools over streamable HTTP, configured from `BRAIN_TOOLS_CONFIG`."""
    logging.basicConfig(level=logging.INFO)
    config = ToolsConfig.load()
    token = config.read_token()
    registry = build_registry(config)
    vault = Vault(config.vault)
    start_human_commit_timer(vault, config.human_commit_seconds)
    host, port = config.listen.host, config.listen.port
    outbox = None
    if config.outbox is not None and config.jobs is not None:
        store = config.jobs.store()
        outbox = config.outbox.receiver(store, config.jobs.kinds)
        importer = config.outbox.importer(vault, load_schema(vault.root), store, config.jobs.kinds)
        start_outbox_timer(importer, OUTBOX_IMPORT_SECONDS)
    elif config.outbox is not None:
        log.warning("outbox configured without a job queue: results cannot be checked, not serving it")
    if config.staging is not None:
        importer = config.staging.importer(vault, load_schema(vault.root))
        start_staging_timer(importer, config.staging.poll_seconds or STAGING_IMPORT_SECONDS)
    log.info("brain_tools serving %d tools on %s:%d", len(registry.specs()), host, port)
    uvicorn.run(build_app(registry, token, outbox), host=host, port=port, log_level="info")


def commit_human_changes_once(vault: Vault) -> str | None:
    """One tick of the timer. A failure is logged, never raised: the server keeps serving."""
    try:
        result = vault.commit_human_changes()
    except VaultError:
        log.exception("could not commit the changes made in the vault by hand")
        return None
    for entry in result.held_back:
        log.warning("not committing %s until the secret is removed", entry.path)
    return result.sha


def start_human_commit_timer(vault: Vault, seconds: float) -> threading.Thread:
    """Commit the user's edits in the background, so history shows them even with no brain write."""

    def loop() -> None:
        while True:
            time.sleep(seconds)
            commit_human_changes_once(vault)

    thread = threading.Thread(target=loop, name="brain_tools-human-commit", daemon=True)
    thread.start()
    return thread


def import_outbox_once(importer: OutboxImporter) -> int:
    """One tick of the importer. A failure is logged, never raised: the server keeps serving."""
    try:
        results = importer.sweep()
    except Exception:
        log.exception("could not sweep the outbox")
        return 0
    imported = 0
    for result in results:
        if result.ok:
            imported += 1
            log.info("imported %s as %s for job %s", result.source, result.note, result.job_id)
        else:
            log.warning("did not import %s for job %s: %s", result.source, result.job_id, result.error)
    return imported


def import_staging_once(importer: StagingImporter) -> int:
    """One tick of the staging importer. A failure is logged, never raised."""
    try:
        results = importer.sweep()
    except Exception:
        log.exception("could not sweep the staging folder")
        return 0
    imported = 0
    for result in results:
        if result.ok:
            imported += 1
            log.info("imported %s as %s", result.source, result.note)
        else:
            log.warning("did not import %s: %s", result.source, result.error)
    return imported


def start_staging_timer(importer: StagingImporter, seconds: float) -> threading.Thread:
    """Import the candidates in the background: brain_tools is the only writer of the vault."""

    def loop() -> None:
        while True:
            time.sleep(seconds)
            import_staging_once(importer)

    thread = threading.Thread(target=loop, name="brain_tools-staging-import", daemon=True)
    thread.start()
    return thread


def start_outbox_timer(importer: OutboxImporter, seconds: float) -> threading.Thread:
    """Import the research results in the background: this is the only writer of the vault."""

    def loop() -> None:
        while True:
            time.sleep(seconds)
            import_outbox_once(importer)

    thread = threading.Thread(target=loop, name="brain_tools-outbox-import", daemon=True)
    thread.start()
    return thread


if __name__ == "__main__":
    main()
