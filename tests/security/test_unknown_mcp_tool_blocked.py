"""An MCP tool brain_tools never declared is not a tool the guard lets through.

`mcp__brain__*` names are looked up in the instance's table. A name that is missing, or that
comes from another MCP server, is external and never allowed, and the stamp refuses it too.
"""

from __future__ import annotations

from second_brain.guard import plugin


def test_unknown_brain_tool_is_blocked(guard: plugin.Guard) -> None:
    decision = guard.pre_tool_call(
        tool_name="mcp__brain__shell_exec", args={"cmd": "id"}, session_id="s1", tool_call_id="c1"
    )
    assert decision == {"action": "block", "message": "tool is never allowed"}


def test_tool_from_another_mcp_server_is_blocked(guard: plugin.Guard) -> None:
    decision = guard.pre_tool_call(
        tool_name="mcp__other__vault_write",
        args={"path": "inbox/a.md"},
        session_id="s1",
        tool_call_id="c1",
    )
    assert decision == {"action": "block", "message": "tool is never allowed"}


def test_unknown_tool_never_gets_a_guard_object(guard: plugin.Guard) -> None:
    args = {"path": "inbox/a.md"}
    guard.pre_tool_call(tool_name="mcp__brain__shell_exec", args=args, session_id="s1", tool_call_id="c1")
    assert (
        guard.stamp_guard(tool_name="mcp__brain__shell_exec", args=args, session_id="s1", tool_call_id="c1")
        is None
    )


def test_a_tool_the_instance_removed_is_blocked(guard: plugin.Guard) -> None:
    assert (
        guard.pre_tool_call(
            tool_name="mcp__brain__vault_delete", args={"path": "inbox/a.md"}, session_id="s1"
        )
        is None
    )
    guard.tool_classes.pop("vault_delete")
    decision = guard.pre_tool_call(
        tool_name="mcp__brain__vault_delete", args={"path": "inbox/a.md"}, session_id="s1"
    )
    assert decision == {"action": "block", "message": "tool is never allowed"}
