"""The core vault tools: read, write, move, delete and undo.

Each handler turns a `VaultError` into a result the model can read, so a refused write reaches
the conversation as an error code instead of a transport failure.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from pydantic import BaseModel, Field

from second_brain.core.types import Autonomy, Effect, ToolSpec
from second_brain.tools.registry import Tool
from second_brain.tools.vault import Provenance, Vault, VaultError


class ReadParams(BaseModel):
    path: str = Field(min_length=1, description="Vault-relative path of the note.")


class WriteParams(BaseModel):
    path: str = Field(min_length=1, description="Vault-relative path of the note.")
    content: str = Field(description="Full new content of the note.")
    reason: str = Field(min_length=1, description="Why the note changes, in one line.")
    expected_sha256: str | None = Field(
        default=None,
        description="sha256 returned by vault_read. Required when the note already exists.",
    )


class MoveParams(BaseModel):
    src: str = Field(min_length=1, description="Vault-relative path to move.")
    dst: str = Field(min_length=1, description="Vault-relative destination path.")
    reason: str = Field(min_length=1, description="Why the note moves, in one line.")


class DeleteParams(BaseModel):
    path: str = Field(min_length=1, description="Vault-relative path of the note.")
    reason: str = Field(min_length=1, description="Why the note goes away, in one line.")


class UndoParams(BaseModel):
    commit: str = Field(min_length=1, description="Commit to revert, as returned by a write.")
    reason: str = Field(min_length=1, description="Why the commit is undone, in one line.")


def _refused(exc: VaultError) -> dict[str, Any]:
    return {"ok": False, "code": exc.code, "message": exc.message}


def _guarded(run: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    """Run a vault operation, turning a refusal into a result instead of an exception."""
    try:
        return {"ok": True, **run()}
    except VaultError as exc:
        return _refused(exc)


def vault_tools(vault: Vault) -> list[Tool]:
    def read(params: BaseModel, provenance: Provenance) -> dict[str, Any]:
        assert isinstance(params, ReadParams)

        def run() -> dict[str, Any]:
            content, sha256 = vault.read(params.path)
            return {"content": content, "sha256": sha256}

        return _guarded(run)

    def write(params: BaseModel, provenance: Provenance) -> dict[str, Any]:
        assert isinstance(params, WriteParams)
        return _guarded(
            lambda: {"commit": vault.write(params.path, params.content, provenance, params.expected_sha256)}
        )

    def move(params: BaseModel, provenance: Provenance) -> dict[str, Any]:
        assert isinstance(params, MoveParams)
        return _guarded(lambda: {"commit": vault.move(params.src, params.dst, provenance)})

    def delete(params: BaseModel, provenance: Provenance) -> dict[str, Any]:
        assert isinstance(params, DeleteParams)
        return _guarded(lambda: {"commit": vault.delete(params.path, provenance)})

    def undo(params: BaseModel, provenance: Provenance) -> dict[str, Any]:
        assert isinstance(params, UndoParams)
        return _guarded(lambda: {"commit": vault.undo(params.commit, provenance)})

    return [
        Tool(
            spec=ToolSpec(
                name="vault_read",
                description="Read a note of the vault, with the sha256 needed to rewrite it.",
                params=ReadParams,
                effect=Effect.READ,
                autonomy=Autonomy.AUTO,
            ),
            handler=read,
        ),
        Tool(
            spec=ToolSpec(
                name="vault_write",
                description="Create or replace a note of the vault, as one undoable commit.",
                params=WriteParams,
                effect=Effect.INTERNAL,
                autonomy=Autonomy.NOTIFY,
            ),
            handler=write,
        ),
        Tool(
            spec=ToolSpec(
                name="vault_move",
                description="Move or rename a note of the vault, as one undoable commit.",
                params=MoveParams,
                effect=Effect.INTERNAL,
                autonomy=Autonomy.NOTIFY,
            ),
            handler=move,
        ),
        Tool(
            spec=ToolSpec(
                name="vault_delete",
                description="Move a note of the vault to the trash, as one undoable commit.",
                params=DeleteParams,
                effect=Effect.INTERNAL,
                autonomy=Autonomy.NOTIFY,
            ),
            handler=delete,
        ),
        Tool(
            spec=ToolSpec(
                name="vault_undo",
                description="Revert a commit of the vault, as one undoable commit.",
                params=UndoParams,
                effect=Effect.INTERNAL,
                autonomy=Autonomy.NOTIFY,
            ),
            handler=undo,
        ),
    ]
