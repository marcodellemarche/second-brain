"""The MCP server (brain_tools) over streamable HTTP.

Every registered tool becomes an MCP tool whose input schema is the tool's pydantic params plus
one extra optional `guard` object. brain_guard fills `guard` before the call leaves Hermes; a call
that arrives without it is refused, so a caller that skipped the guard cannot write. The HTTP layer
refuses a request without the bearer token before it reaches the MCP handler.
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

from second_brain.tools.config import ToolsConfig, build_registry
from second_brain.tools.registry import Registry
from second_brain.tools.vault import Provenance, Vault, VaultError

log = logging.getLogger(__name__)

INTEGRITY_VALUES: tuple[str, ...] = ("trusted", "untrusted")

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


def build_app(registry: Registry, token: str) -> ASGIApp:
    """The ASGI app: the MCP server behind bearer authentication."""
    return BearerAuth(build_server(registry, token).streamable_http_app(), token)


def main() -> None:
    """Serve the tools over streamable HTTP, configured from `BRAIN_TOOLS_CONFIG`."""
    logging.basicConfig(level=logging.INFO)
    config = ToolsConfig.load()
    token = config.read_token()
    registry = build_registry(config)
    start_human_commit_timer(Vault(config.vault), config.human_commit_seconds)
    host, port = config.listen.host, config.listen.port
    log.info("brain_tools serving %d tools on %s:%d", len(registry.specs()), host, port)
    uvicorn.run(build_app(registry, token), host=host, port=port, log_level="info")


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


if __name__ == "__main__":
    main()
