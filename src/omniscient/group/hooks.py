"""Translate Claude Code and GitHub Copilot hook events for omnigroup (pure functions).

`translate(source, event)` turns one hook event into the session update, step and
pending decision the server stores; `decision_output(source, event, answer)` renders the
owner's answer in the JSON the agent expects on stdout. The hook command itself lives in
`hook_client.py`; the settings installer in `hook_install.py`.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from omniscient.group.models import JumpTarget, Pending, Session, Step, StepKind, utcnow
from omniscient.group.permission_rules import always_permissions, rule_labels
from omniscient.group.sanitize import PAYLOAD_TEXT_MAX

MARK = "omniscient group hook"  # how installed hook commands are recognised
HOOK_SCRIPT = "omniscient-hook"  # the lightweight console script the installer uses
MARKS = (MARK, "omniscient.group.hook_client", HOOK_SCRIPT)
WAIT_S = {"claude": 100, "copilot": 25}  # within hook timeouts of 110 s and 30 s
ASK_TOOL = "AskUserQuestion"
DENY_MESSAGE = "Refusé depuis omnigroup"
PREVIEW_MAX = PAYLOAD_TEXT_MAX  # beyond: clipped, "truncated", only "deny" (M1)
CONTEXT_KEY = "omnigroup"  # extra info the hook adds to the event it posts
COPILOT_EVENTS = {
    "sessionStart": "SessionStart",
    "sessionEnd": "SessionEnd",
    "userPromptSubmitted": "UserPromptSubmit",
    "preToolUse": "PreToolUse",
    "postToolUse": "PostToolUse",
    "permissionRequest": "PermissionRequest",
    "agentStop": "Stop",
    "subagentStop": "SubagentStop",
    "errorOccurred": "PostToolUseFailure",
}
# canonical event -> (session state, step kind); None = no step
_TRACK: dict[str, tuple[str, StepKind | None]] = {
    "SessionStart": ("idle", "notice"),
    "SessionEnd": ("done", "stop"),
    "UserPromptSubmit": ("working", "prompt"),
    "PreToolUse": ("working", "tool"),
    "PostToolUse": ("working", "tool_result"),
    "PostToolUseFailure": ("working", "error"),
    "Stop": ("idle", "stop"),
    "SubagentStop": ("working", "notice"),
    "PermissionRequest": ("waiting", "permission"),
}
_NOTIFY_STATE = {
    "permission_prompt": "waiting",
    "agent_needs_input": "waiting",
    "idle_prompt": "idle",
    "agent_completed": "idle",
}
_BRIEF_KEYS = ("command", "file_path", "path", "pattern", "url", "query", "description")


class HookError(ValueError):
    """The hook event is not something omnigroup understands."""


@dataclass(frozen=True)
class Translation:
    session: Session
    step: Step | None
    pending: Pending | None
    wait_s: int | None


# ---------------------------------------------------------------- translation (pure)


def event_name(event: Mapping[str, Any]) -> str:
    """The canonical (Claude-style) event name of a Claude or Copilot hook event."""
    raw = event.get("hook_event_name") or event.get("hookEventName")
    if not isinstance(raw, str) or not raw:
        raise HookError("hook event without hook_event_name")
    return COPILOT_EVENTS.get(raw, raw)


def _native_id(event: Mapping[str, Any]) -> str:
    sid = event.get("session_id") or event.get("sessionId")
    if isinstance(sid, str) and sid.strip():
        return sid.strip()[:200]
    cwd = str(event.get("cwd") or "")  # Copilot CLI may send no session id
    return "cwd-" + hashlib.sha256(cwd.encode()).hexdigest()[:16]


def session_key(source: str, event: Mapping[str, Any]) -> str:
    """The omnigroup session id ("<source>:<native id>") of a hook event."""
    return f"{source}:{_native_id(event)}"


def tool_input(event: Mapping[str, Any]) -> Any:
    value = event.get("tool_input", event.get("toolArgs"))
    if isinstance(value, str):  # Copilot sends toolArgs as a JSON string
        try:
            return json.loads(value)
        except ValueError:
            return value
    return value


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, default=str)


def _tool_summary(tool: str, args: Any) -> str:
    if isinstance(args, dict):
        for key in _BRIEF_KEYS:
            if isinstance(args.get(key), str) and args[key]:
                return f"{tool} : {args[key]}"
    brief = _text(args)
    return f"{tool} : {brief}" if brief else tool


def _questions(args: Any) -> list[dict[str, Any]]:
    qs = args.get("questions") if isinstance(args, dict) else None
    return [q for q in qs if isinstance(q, dict)] if isinstance(qs, list) else []


def _labels(questions: list[dict[str, Any]]) -> tuple[str, ...]:
    labels: list[str] = []
    for q in questions:
        for opt in q.get("options") or []:
            label = opt.get("label") if isinstance(opt, dict) else opt
            if isinstance(label, str) and label not in labels:
                labels.append(label)
    return tuple(labels)


def _jump(source: str, native: str, event: Mapping[str, Any]) -> JumpTarget | None:
    ctx = event.get(CONTEXT_KEY)
    entry = ctx.get("entrypoint") if isinstance(ctx, dict) else None
    if source != "claude" or native.startswith("cwd-"):
        return None
    if entry == "claude-vscode":
        return JumpTarget("vscode_session", native, "Ouvrir dans VS Code")
    return JumpTarget("terminal", native, "Reprendre dans un terminal")


def _step_text(
    name: str, event: Mapping[str, Any], tool: str
) -> tuple[str, str | None]:
    if name == "UserPromptSubmit":
        prompt = _text(event.get("prompt"))
        return prompt or "Nouveau message", prompt or None
    if name in ("PreToolUse", "PermissionRequest"):
        args = tool_input(event)
        prefix = "Autorisation : " if name == "PermissionRequest" else ""
        return prefix + _tool_summary(tool, args), _text(args) or None
    if name == "PostToolUse":
        out = _text(event.get("tool_response", event.get("toolResult")))
        return f"{tool} terminé", out or None
    if name == "PostToolUseFailure":
        err = _text(event.get("error") or event.get("tool_response"))
        return f"{tool or 'Agent'} : échec", err or None
    if name == "Stop":
        msg = _text(event.get("last_assistant_message") or event.get("message"))
        return msg or "Tour terminé", msg or None
    if name == "Notification":
        return _text(event.get("message")) or "Notification", None
    labels = {"SessionStart": "Session ouverte", "SessionEnd": "Session fermée"}
    return labels.get(name, "Sous-agent terminé"), None


def _pending(
    name: str,
    source: str,
    sid: str,
    event: Mapping[str, Any],
    now: datetime,
    new_id: str,
) -> tuple[Pending | None, int | None, str | None]:
    """(pending, wait_s, overriding step kind) for decision events."""
    tool = _text(event.get("tool_name") or event.get("toolName")) or None
    wait = WAIT_S.get(source, WAIT_S["copilot"])
    expires = now + timedelta(seconds=wait)
    if name == "PermissionRequest":
        payload, options = _permission_payload(source, tool or "", event)
        p = Pending(new_id, sid, "permission", now, expires, tool, payload, options)
        return p, wait, None
    if name == "PreToolUse" and tool == ASK_TOOL and source == "claude":
        qs = _questions(tool_input(event))
        p = Pending(
            new_id, sid, "question", now, expires, tool, {"questions": qs}, _labels(qs)
        )
        return p, wait, "question"
    return None, None, None


def preview_text(args: Any) -> str:
    """The full tool input as the owner reads it (indented JSON, or the raw string).
    Secrets are masked later by the Pending itself; paths are kept (M1)."""
    if isinstance(args, str):
        return args
    return json.dumps(args, ensure_ascii=False, indent=2, default=str)


def _permission_payload(
    source: str, tool: str, event: Mapping[str, Any]
) -> tuple[dict[str, Any], tuple[str, ...]]:
    args = tool_input(event)
    full = preview_text(args)
    payload: dict[str, Any] = {"tool_input": full[:PREVIEW_MAX]}
    if len(full) > PREVIEW_MAX:
        payload["truncated"] = len(full)  # the widget refuses "allow" (too long)
    always = always_permissions(tool, args, event) if source == "claude" else None
    if always:
        payload["always_rules"] = rule_labels(always)
    options = ("allow", "deny", "always") if always else ("allow", "deny")
    return payload, options


def translate(
    source: str,
    event: Mapping[str, Any],
    now: datetime | None = None,
    new_id: Callable[[], str] = lambda: str(uuid.uuid4()),
) -> Translation:
    """The session update, step and pending decision for one hook event."""
    if source not in WAIT_S:
        raise HookError(f"unknown hook source {source!r}")
    if not isinstance(event, Mapping):
        raise HookError("hook event must be a JSON object")
    at = now or utcnow()
    name = event_name(event)
    if name in ("StatusLine", "Status"):
        raise HookError("statusLine events carry usage only: use translate_usage")
    native = _native_id(event)
    sid = session_key(source, event)
    tool = _text(event.get("tool_name") or event.get("toolName"))
    state, kind = _TRACK.get(name, ("working", "notice"))
    if name == "Notification":
        state = _NOTIFY_STATE.get(_text(event.get("notification_type")), "working")
    ctx = event.get(CONTEXT_KEY)
    tracking_only = isinstance(ctx, dict) and bool(ctx.get("fast"))
    pending, wait, override = (
        (None, None, None)
        if tracking_only
        else _pending(name, source, sid, event, at, new_id())
    )
    if override is not None:
        state, kind = "waiting", override  # type: ignore[assignment]
    summary, detail = _step_text(name, event, tool)
    if kind == "question":
        qs = _questions(tool_input(event))
        summary = _text(qs[0].get("question")) if qs else "Question"
    cwd = event.get("cwd") if isinstance(event.get("cwd"), str) else None
    title = (
        Path(cwd).name if cwd else ("Claude Code" if source == "claude" else "Copilot")
    )
    mode = event.get("permission_mode")
    session = Session(
        id=sid,
        source=source,
        native_id=native,
        title=title,
        cwd=cwd,  # type: ignore[arg-type]
        state=state,
        started_at=at,
        updated_at=at,
        last_step=summary,  # type: ignore[arg-type]
        jump=_jump(source, native, event),
        meta={
            "event": name,
            **({"permission_mode": mode} if isinstance(mode, str) else {}),
        },
    )
    step = Step(sid, at, kind, summary, detail) if kind is not None else None
    return Translation(session, step, pending, wait)


# ---------------------------------------------------------------- decisions (pure)


def _claude_permission(
    answer: Mapping[str, Any], tool: str, event: Mapping[str, Any]
) -> dict[str, Any] | None:
    choice = answer.get("decision")
    if choice in ("allow", "always"):
        decision: dict[str, Any] = {"behavior": "allow"}
        rules = always_permissions(tool, tool_input(event), event)
        if choice == "always" and rules:  # recomputed here, never taken from the daemon
            decision["updatedPermissions"] = rules
    elif choice == "deny":
        message = _text(answer.get("message")) or DENY_MESSAGE
        decision = {"behavior": "deny", "message": message, "interrupt": False}
    else:
        return None
    return {
        "hookSpecificOutput": {
            "hookEventName": "PermissionRequest",
            "decision": decision,
        }
    }


def _claude_answers(answer: Mapping[str, Any], args: Any) -> dict[str, Any] | None:
    raw = answer.get("answers")
    if not isinstance(raw, dict) or not raw:
        return None
    answers = {
        str(q): ", ".join(map(str, a)) if isinstance(a, list) else str(a)
        for q, a in raw.items()
    }
    updated = {"questions": _questions(args), "answers": answers}
    out = {
        "hookEventName": "PreToolUse",
        "permissionDecision": "allow",
        "updatedInput": updated,
    }
    return {"hookSpecificOutput": out}


def decision_output(
    source: str, event: Mapping[str, Any], answer: Mapping[str, Any]
) -> dict[str, Any] | None:
    """The JSON the agent expects on stdout for the owner's answer (None = no decision)."""
    name = event_name(event)
    tool = _text(event.get("tool_name") or event.get("toolName"))
    if source == "claude" and name == "PermissionRequest":
        return _claude_permission(answer, tool, event)
    if source == "claude" and name == "PreToolUse" and tool == ASK_TOOL:
        return _claude_answers(answer, tool_input(event))
    if source == "copilot" and name == "PermissionRequest":
        choice = answer.get("decision")
        if choice in ("allow", "always"):
            return {"behavior": "allow"}
        if choice == "deny":
            return {
                "behavior": "deny",
                "message": _text(answer.get("message")) or DENY_MESSAGE,
            }
    return None
