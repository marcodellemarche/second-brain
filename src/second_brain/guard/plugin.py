"""brain_guard: the second brain policy, enforced in Hermes hooks.

The plugin keeps one in-memory state per session and turns what a session reads into flags the
policy checks later. Children point at their parent, and every lookup walks the chain, so a flag
set on a parent after a child started still applies.

Anything that means "cannot decide" is raised, never swallowed: Hermes blocks the tool on a hook
exception, and a config the plugin cannot read stops the plugin, which the watchdog notices.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from second_brain.core.types import Autonomy, stricter
from second_brain.guard.audit import AuditLog
from second_brain.guard.budget import BudgetVerdict, check_budget, note_search_result
from second_brain.guard.classify import (
    MCP_PREFIX,
    TERMINAL_DENIED_MESSAGE,
    ToolClass,
    classify,
    path_taint,
    terminal_denied,
)
from second_brain.guard.config import GuardConfig
from second_brain.guard.heartbeat import write_heartbeat
from second_brain.guard.rules import Decision, decide
from second_brain.guard.sanitize import sanitize_reply
from second_brain.guard.session import SessionStore

log = logging.getLogger("brain_guard")

HEARTBEAT_INTERVAL = 10.0
HEARTBEAT_FILE = "heartbeat.json"
AUDIT_FILE = "audit.jsonl"

# Tools whose decisive argument is a URL, and is therefore literal only if the user typed it.
URL_TOOLS = frozenset({"web_extract", "browser_navigate"})
ARGS_PREVIEW_CHARS = 200
MAX_PENDING_VERDICTS = 256
NO_VERDICT_MESSAGE = "brain_guard made no decision for this call, so it was not run. Try the call again."

URL_RE = re.compile(r"https?://[^\s\"'<>)\]}\\]+")
VAULT_PATH_RE = re.compile(r"[^\s\"'<>:|,]+\.(?:md|txt|ya?ml|json|csv)\b")
# Tools whose `path` argument names a vault path: what they return is classified by where it lives.
PATH_TOOLS = frozenset({"read_file", "search_files", "vault_read"})
# A bound on the strings walked inside one JSON result, so a huge result cannot stall the hook.
MAX_NOTE_TEXTS = 200
FRONTMATTER_RE = re.compile(r"\A---[ \t]*\n(?P<body>.*?)\n---[ \t]*(?:\n|\Z)", re.DOTALL)
TRUST_RE = re.compile(r"^trust:[ \t]*(?P<value>[^\s#]+)", re.MULTILINE)
UNTRUSTED_OPEN = '<untrusted-content source="{}">'
UNTRUSTED_CLOSE = "</untrusted-content>"


@dataclass(frozen=True)
class Verdict:
    """What the guard decided about one call, and the facts it decided on."""

    tool_class: ToolClass
    autonomy: Autonomy
    decision: Decision
    untrusted: bool
    private: bool
    message: str = ""


class Guard:
    """The hook callbacks and the state they share."""

    def __init__(self, config: GuardConfig, store: SessionStore | None = None) -> None:
        self.config = config
        self.store = store if store is not None else SessionStore()
        self.guard_dir = Path(config.guard_dir)
        self.guard_dir.mkdir(parents=True, exist_ok=True)
        self.audit = AuditLog(self.guard_dir / AUDIT_FILE)
        self.tool_classes = config.tool_classes()
        self._heartbeat_stop = threading.Event()
        self._heartbeat_thread: threading.Thread | None = None
        self._approvals: dict[tuple[str, str], Verdict] = {}
        self._approvals_lock = threading.Lock()

    # --- hooks that observe ------------------------------------------------------------------

    def pre_llm_call(
        self,
        session_id: str = "",
        sender_id: str = "",
        platform: str = "",
        user_message: Any = "",
        parent_session_id: str = "",
        **_kwargs: Any,
    ) -> None:
        """Remember who is talking, which session is the parent, and the URLs the user gave."""
        self.store.set_identity(session_id, sender_id, platform)
        # The budget bounds one task, not the whole chat session: a new turn starts it over.
        self.store.start_turn(session_id)
        if parent_session_id:
            self.store.link_child(session_id, parent_session_id)
        if isinstance(user_message, str):
            urls = _urls(user_message)
            if urls:
                self.store.add_known_urls(session_id, urls)
        state = self.store.get(session_id)
        self.audit.append(
            {
                "event": "pre_llm_call",
                "session": session_id,
                "sender": state.sender,
                "platform": state.platform,
                "parent": state.parent or "",
            }
        )

    def subagent_start(
        self,
        parent_session_id: str = "",
        child_session_id: str = "",
        child_goal: str = "",
        **_kwargs: Any,
    ) -> None:
        """Link the child to its parent: the chain is what makes flags and sender inherited."""
        if parent_session_id and child_session_id:
            self.store.link_child(child_session_id, parent_session_id)
        self.audit.append(
            {
                "event": "subagent_start",
                "parent": parent_session_id,
                "child": child_session_id,
                "goal": child_goal or "",
            }
        )

    def transform_tool_result(
        self,
        tool_name: str = "",
        args: Any = None,
        result: Any = None,
        session_id: str = "",
        **_kwargs: Any,
    ) -> str | None:
        """Turn what the tool returned into session flags, and fence off untrusted text."""
        text = result if isinstance(result, str) else _dumps(result)
        call_args = args if isinstance(args, dict) else {}
        tool_class = classify(tool_name, self.tool_classes)

        untrusted = tool_class.taints_untrusted
        private = tool_class.taints_private
        if tool_name in PATH_TOOLS or _policy_name(tool_name) in PATH_TOOLS:
            path_untrusted, path_private = path_taint(
                str(call_args.get("path", "")),
                self.config.vault_root,
                self.config.private_prefixes,
                self.config.untrusted_prefixes,
            )
            untrusted = untrusted or path_untrusted
            private = private or path_private
        if any(_is_untrusted_note(candidate) for candidate in _note_texts(text)):
            untrusted = True

        if untrusted:
            self.store.mark_untrusted(session_id)
        if private:
            self.store.mark_private(session_id)

        if tool_name in self.config.search_tools:
            note_search_result(self.store, session_id, len(_search_items(text)))

        if untrusted or private:
            self.audit.append(
                {
                    "event": "taint",
                    "session": session_id,
                    "tool": tool_name,
                    "untrusted": untrusted,
                    "private": private,
                }
            )

        if untrusted and not text.lstrip().startswith("<untrusted-content"):
            return f"{UNTRUSTED_OPEN.format(tool_name)}{text}{UNTRUSTED_CLOSE}"
        return None

    def transform_llm_output(
        self,
        text: str | None = None,
        session_id: str = "",
        response_text: str | None = None,
        **_kwargs: Any,
    ) -> str | None:
        """Strip attachment tags and filesystem paths, so nothing leaves the chat by accident."""
        source = response_text if isinstance(response_text, str) else text
        if not isinstance(source, str) or not source:
            return None
        cleaned, removed = sanitize_reply(source, self.config.media_dir)
        if removed:
            self.audit.append({"event": "sanitize", "session": session_id, "removed": removed})
        return cleaned

    # --- the decision ------------------------------------------------------------------------

    def pre_tool_call(
        self,
        tool_name: str = "",
        args: Any = None,
        session_id: str = "",
        tool_call_id: str = "",
        **_kwargs: Any,
    ) -> dict[str, str] | None:
        """Budget, then the policy. `None` lets the call run; the guard object comes from stamp_guard."""
        call_args = args if isinstance(args, dict) else {}
        budget = check_budget(
            self.store,
            session_id,
            tool_name,
            call_args,
            self.config.policy,
            time.monotonic(),
            self.config.search_tools,
        )
        verdict = self.evaluate(tool_name, call_args, session_id, budget=budget)
        self.audit_decision(tool_name, call_args, session_id, tool_call_id, verdict)
        if tool_name in self.config.search_tools and verdict.decision.action != "block":
            self.store.start_search(session_id)
        if _is_brain_tool(tool_name):
            # Handed to stamp_guard: the approval it writes must be the one this verdict earned,
            # not one recomputed after a concurrent tool result changed the flags.
            self._remember(session_id, tool_call_id, verdict)

        if verdict.decision.action == "block":
            return {"action": "block", "message": verdict.message or verdict.decision.reason}
        if verdict.decision.action == "ask":
            message = _approval_text(tool_name, call_args, verdict.decision)
            directive = {"action": "approve", "message": message}
            if tool_call_id:
                # One approval covers this call only, never the same tool for the rest of the session.
                directive["rule_key"] = f"brain_guard:{tool_call_id}"
            return directive
        return None

    def stamp_guard(
        self,
        tool_name: str = "",
        args: Any = None,
        session_id: str = "",
        tool_call_id: str = "",
        task_id: str = "",
        **_kwargs: Any,
    ) -> dict[str, Any] | None:
        """Replace the `guard` argument of a brain_tools call with one built from the store.

        The model sees `guard` in the tool schema and could fill it with anything, so nothing it
        sent survives. Hermes runs the approval gate before it applies modified args, so a call
        that needed approval carries `human:` here only if a human actually said yes.
        """
        if not _is_brain_tool(tool_name):
            return None
        verdict = self._recall(session_id, tool_call_id)
        if verdict is None:
            # Every brain_tools call is decided in the same hook pass, so a missing verdict means
            # the decision itself failed. brain_tools must not run without one.
            self.audit.append(
                {
                    "event": "stamp_without_verdict",
                    "tool": tool_name,
                    "session": session_id,
                    "call": tool_call_id,
                }
            )
            return {"action": "block", "message": NO_VERDICT_MESSAGE}
        if verdict.decision.action == "block":
            return None
        return {
            "action": "modify",
            "args": {"guard": self._guard_object(session_id, task_id, verdict)},
        }

    def evaluate(
        self,
        tool_name: str,
        args: dict[str, Any],
        session_id: str,
        budget: BudgetVerdict | None = None,
    ) -> Verdict:
        """Budget first when one is given, then the terminal rules, then the policy. No state changes."""
        tool_class = classify(tool_name, self.tool_classes)
        autonomy = self._autonomy(tool_name, tool_class)
        untrusted, private = self.store.flags(session_id)
        denied = _terminal_reason(tool_name, args)
        if budget is not None and not budget.ok:
            decision, message = Decision("block", budget.reason), budget.message_to_model
        elif denied is not None:
            decision, message = Decision("block", denied), TERMINAL_DENIED_MESSAGE.format(reason=denied)
        else:
            decision = decide(
                tool_class.effect,
                autonomy,
                untrusted=untrusted,
                private=private,
                user_literal=self._user_literal(tool_name, args, session_id),
            )
            message = ""
        return Verdict(tool_class, autonomy, decision, untrusted, private, message)

    def audit_decision(
        self,
        tool_name: str,
        args: dict[str, Any],
        session_id: str,
        tool_call_id: str,
        verdict: Verdict,
    ) -> None:
        # The exact text the human is shown, so the audit says what was asked, not only that.
        prompt = _approval_text(tool_name, args, verdict.decision) if verdict.decision.action == "ask" else ""
        self.audit.append(
            {
                "event": "decision",
                "tool": tool_name,
                "action": verdict.decision.action,
                "reason": verdict.decision.reason,
                "prompt": prompt,
                "session": session_id,
                "call": tool_call_id,
                "sender": self.store.sender(session_id),
                "autonomy": str(verdict.autonomy),
                "effect": str(verdict.tool_class.effect),
                "untrusted": verdict.untrusted,
                "private": verdict.private,
                "args": _preview(args, ARGS_PREVIEW_CHARS),
            }
        )

    def _remember(self, session_id: str, tool_call_id: str, verdict: Verdict) -> None:
        if not tool_call_id:
            return
        with self._approvals_lock:
            while len(self._approvals) >= MAX_PENDING_VERDICTS:
                self._approvals.pop(next(iter(self._approvals)))
            self._approvals[(session_id, tool_call_id)] = verdict

    def _recall(self, session_id: str, tool_call_id: str) -> Verdict | None:
        with self._approvals_lock:
            return self._approvals.pop((session_id, tool_call_id), None) if tool_call_id else None

    def _autonomy(self, tool_name: str, tool_class: ToolClass) -> Autonomy:
        """The class default, tightened by the instance policy. Policy can never loosen `never`."""
        if tool_class.autonomy is Autonomy.NEVER:
            return Autonomy.NEVER
        tool_policy = self.config.policy.tools.get(_policy_name(tool_name))
        if tool_policy is None or tool_policy.autonomy is None:
            return tool_class.autonomy
        return stricter(tool_class.autonomy, tool_policy.autonomy)

    def _user_literal(self, tool_name: str, args: dict[str, Any], session_id: str) -> bool:
        """True only for a URL the user typed in this chain: nothing else can be literal."""
        if tool_name not in URL_TOOLS:
            return False
        urls = _arg_urls(args)
        return bool(urls) and all(self.store.knows_url(session_id, url) for url in urls)

    def _guard_object(self, session_id: str, task_id: str, verdict: Verdict) -> dict[str, str]:
        """Built from the store only: the model can see `guard` in the schema and fake it."""
        sender = self.store.sender(session_id)
        requested_by = sender or _scheduled(task_id, session_id)
        return {
            "requested_by": requested_by,
            "session": session_id,
            "source_integrity": "untrusted" if verdict.untrusted else "trusted",
            "approval": _approval(verdict, sender or requested_by),
        }

    # --- heartbeat ---------------------------------------------------------------------------

    def beat(self) -> None:
        write_heartbeat(
            self.guard_dir / HEARTBEAT_FILE,
            {"pid": os.getpid(), "plugin": "brain_guard"},
        )

    def start_heartbeat(self) -> None:
        self.beat()
        thread = threading.Thread(target=self._heartbeat_loop, name="brain_guard-heartbeat", daemon=True)
        self._heartbeat_thread = thread
        thread.start()

    def stop_heartbeat(self) -> None:
        self._heartbeat_stop.set()

    def _heartbeat_loop(self) -> None:
        while not self._heartbeat_stop.wait(HEARTBEAT_INTERVAL):
            try:
                self.beat()
            except OSError:
                log.exception("brain_guard heartbeat failed")


def register(ctx: Any) -> None:
    """Entry point Hermes calls once, when it loads the plugin."""
    config = GuardConfig.load()
    # Before anything else: an approval the guard stamps is only worth the human behind it.
    config.check_approvals()
    guard = Guard(config)
    guard.start_heartbeat()
    guard.audit.append({"event": "loaded", "pid": os.getpid()})
    ctx.register_hook("pre_llm_call", guard.pre_llm_call)
    ctx.register_hook("subagent_start", guard.subagent_start)
    ctx.register_hook("transform_tool_result", guard.transform_tool_result)
    ctx.register_hook("pre_tool_call", guard.pre_tool_call)
    # A second callback: Hermes keeps `modify` directives apart from block/approve ones, so the
    # guard object is applied to the call that runs, and only after a human approved it.
    ctx.register_hook("pre_tool_call", guard.stamp_guard)
    ctx.register_hook("transform_llm_output", guard.transform_llm_output)
    log.info("brain_guard loaded")


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def _preview(args: dict[str, Any], limit: int) -> str:
    """The arguments as one short line, so the audit and the approval text stay readable."""
    text = " ".join(_dumps(args).split())
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _is_brain_tool(tool_name: str) -> bool:
    return tool_name.startswith(MCP_PREFIX)


def _terminal_reason(tool_name: str, args: dict[str, Any]) -> str | None:
    """Why a terminal call is refused outright, or None. Not a policy question: never allowed."""
    if tool_name != "terminal":
        return None
    command = args.get("command", "")
    return terminal_denied(command) if isinstance(command, str) else None


def _policy_name(tool_name: str) -> str:
    """Policy and config key tools without the MCP prefix."""
    return tool_name.removeprefix(MCP_PREFIX)


def _arg_urls(args: dict[str, Any]) -> list[str]:
    """The URLs a tool call is about, whether they arrive as one string or as a list."""
    found: list[str] = []
    for value in args.values():
        if isinstance(value, str):
            found.extend(_urls(value))
        elif isinstance(value, list):
            found.extend(url for item in value if isinstance(item, str) for url in _urls(item))
    return found


def _approval(verdict: Verdict, approved_by: str) -> str:
    """brain_tools refuses an approval-only zone when this is "none".

    An `ask` decision reaches the tool only after the human gate allowed it, so that is the one
    case where the call carries an approval. Everything else never had a human in the loop.
    """
    if verdict.decision.action != "ask":
        return "none"
    return f"human:{approved_by or 'unknown'}"


def _scheduled(task_id: str, session_id: str) -> str:
    """Who asked, for a run with no human behind it: a cron job id, else the session itself."""
    if task_id:
        return f"schedule:{task_id}"
    return f"session:{session_id}" if session_id else ""


def _approval_text(tool_name: str, args: dict[str, Any], decision: Decision) -> str:
    """What the human sees when asked: the tool, the arguments, and why the guard is asking."""
    text = f"{tool_name} ({_preview(args, ARGS_PREVIEW_CHARS)}): {decision.reason}"
    calendar = _target_calendar(tool_name, args)
    return f"Calendar: {calendar.upper()}\n{text}" if calendar else text


def _target_calendar(tool_name: str, args: dict[str, Any]) -> str | None:
    """The calendar a calendar write lands on, shown first: the arguments preview is easy to skim."""
    name = _policy_name(tool_name)
    if not name.startswith("calendar_"):
        return None
    if name == "calendar_main_write":
        return "main"
    calendar = args.get("calendar")
    return calendar if isinstance(calendar, str) and calendar else None


def _note_texts(text: str) -> list[str]:
    """The text, plus every string inside it when it is JSON: an MCP result wraps the note."""
    found = [text]
    pending: list[Any] = []
    try:
        pending.append(json.loads(text))
    except ValueError:
        return found
    while pending and len(found) < MAX_NOTE_TEXTS:
        item = pending.pop()
        if isinstance(item, str):
            found.append(item.lstrip())
            if item.lstrip().startswith(("{", "[")):
                try:
                    pending.append(json.loads(item))
                except ValueError:
                    pass
        elif isinstance(item, dict):
            pending.extend(item.values())
        elif isinstance(item, list):
            pending.extend(item)
    return found


def _is_untrusted_note(text: str) -> bool:
    """True when the text opens with frontmatter that says `trust: untrusted`."""
    match = FRONTMATTER_RE.match(text)
    if match is None:
        return False
    trust = TRUST_RE.search(match.group("body"))
    if trust is None:
        return False
    return trust.group("value").strip("'\"").lower() == "untrusted"


def _urls(text: str) -> set[str]:
    """URLs in a text, without the sentence punctuation that follows them."""
    return {url.rstrip(".,;:!?'\"") for url in URL_RE.findall(text)}


def _search_items(text: str) -> set[str]:
    """What a search returned, so a result with nothing in it can be told apart from one with hits."""
    return _urls(text) | set(VAULT_PATH_RE.findall(text))


__all__ = ["Guard", "register"]
