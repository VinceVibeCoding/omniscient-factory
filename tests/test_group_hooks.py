"""omnigroup hooks: event translation, decision rendering and the fail-open hook command."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import pytest

from omniscient.group.hooks import (
    HookError,
    decision_output,
    event_name,
    tool_input,
    translate,
)

NOW = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
PID = "6f1c1d2e-0000-4000-8000-000000000001"


def claude(name: str, **fields: Any) -> dict[str, Any]:
    return {"session_id": "s-1", "cwd": "/work/demo", "hook_event_name": name, **fields}


ASK_INPUT = {
    "questions": [
        {
            "question": "Quelle base ?",
            "header": "Base",
            "multiSelect": False,
            "options": [{"label": "SQLite"}, {"label": "Postgres"}],
        }
    ]
}


# ---------------------------------------------------------------- translate


def test_permission_request_opens_a_permission_pending() -> None:
    event = claude("PermissionRequest", tool_name="Bash", tool_input={"command": "ls"})
    t = translate("claude", event, NOW, new_id=lambda: PID)
    assert t.session.id == "claude:s-1"
    assert t.session.state == "waiting"
    assert t.session.title == "demo"
    assert t.step is not None and t.step.kind == "permission"
    assert t.step.summary == "Autorisation : Bash : ls"
    assert t.pending is not None
    assert (t.pending.id, t.pending.kind, t.pending.tool) == (PID, "permission", "Bash")
    assert t.pending.options == ("allow", "deny", "always")
    assert t.pending.payload == {
        "tool_input": '{\n  "command": "ls"\n}',
        "always_rules": ["Bash(ls)"],
    }
    assert t.wait_s == 100
    assert t.pending.expires_at is not None
    assert (t.pending.expires_at - NOW).total_seconds() == 100


def test_ask_user_question_opens_a_question_pending() -> None:
    event = claude("PreToolUse", tool_name="AskUserQuestion", tool_input=ASK_INPUT)
    t = translate("claude", event, NOW)
    assert t.session.state == "waiting"
    assert t.step is not None and t.step.kind == "question"
    assert t.step.summary == "Quelle base ?"
    assert t.pending is not None and t.pending.kind == "question"
    assert t.pending.options == ("SQLite", "Postgres")
    assert t.pending.payload["questions"] == ASK_INPUT["questions"]


@pytest.mark.parametrize(
    ("event", "state", "kind"),
    [
        (claude("SessionStart"), "idle", "notice"),
        (claude("UserPromptSubmit", prompt="Fais X"), "working", "prompt"),
        (claude("PreToolUse", tool_name="Read", tool_input={"file_path": "a.py"}), "working", "tool"),
        (claude("PostToolUse", tool_name="Read", tool_response="ok"), "working", "tool_result"),
        (claude("PostToolUseFailure", tool_name="Bash", error="boom"), "working", "error"),
        (claude("Stop", last_assistant_message="Fini"), "idle", "stop"),
        (claude("SubagentStop"), "working", "notice"),
        (claude("SessionEnd"), "done", "stop"),
        (claude("Notification", notification_type="idle_prompt", message="?"), "idle", "notice"),
        (claude("Notification", notification_type="permission_prompt"), "waiting", "notice"),
    ],
)  # fmt: skip
def test_tracked_events_map_to_state_and_step(
    event: dict[str, Any], state: str, kind: str
) -> None:
    t = translate("claude", event, NOW)
    assert t.session.state == state
    assert t.step is not None and t.step.kind == kind
    assert t.pending is None and t.wait_s is None


def test_step_texts_are_redacted_and_clipped() -> None:
    secret = "sk-ant-" + "a" * 30
    event = claude("UserPromptSubmit", prompt=f"use {secret} " + "x" * 5000)
    t = translate("claude", event, NOW)
    assert t.step is not None and secret not in t.step.summary
    assert len(t.step.summary) <= 300
    assert t.step.detail is not None and len(t.step.detail) <= 4000


def test_permission_preview_is_redacted() -> None:
    args = {"command": "curl -H 'x: ghp_" + "b" * 36 + "'"}
    t = translate(
        "claude", claude("PermissionRequest", tool_name="Bash", tool_input=args)
    )
    assert t.pending is not None and "ghp_" not in t.pending.payload["tool_input"]


def test_fast_invocation_never_opens_a_pending() -> None:
    event = claude("PermissionRequest", tool_name="Bash", omnigroup={"fast": True})
    t = translate("claude", event, NOW)
    assert t.pending is None and t.step is not None


def test_jump_target_depends_on_the_entrypoint() -> None:
    vscode = claude("Stop", omnigroup={"entrypoint": "claude-vscode"})
    cli = claude("Stop", omnigroup={"entrypoint": "cli"})
    assert translate("claude", vscode).session.jump.kind == "vscode_session"  # type: ignore[union-attr]
    assert translate("claude", cli).session.jump.kind == "terminal"  # type: ignore[union-attr]
    assert translate("claude", cli).session.meta == {"event": "Stop"}


def test_copilot_events_use_camel_case_fields() -> None:
    event = {
        "hookEventName": "permissionRequest",
        "sessionId": "cp-9",
        "cwd": "/w/app",
        "toolName": "shell",
        "toolArgs": json.dumps({"command": "rm x"}),
    }
    t = translate("copilot", event, NOW)
    assert t.session.id == "copilot:cp-9" and t.session.jump is None
    assert t.pending is not None and t.pending.options == ("allow", "deny")
    assert t.wait_s == 25
    assert t.step is not None and t.step.summary == "Autorisation : shell : rm x"


def test_copilot_without_session_id_falls_back_to_cwd() -> None:
    a = translate("copilot", {"hookEventName": "agentStop", "cwd": "/w/a"})
    b = translate("copilot", {"hookEventName": "agentStop", "cwd": "/w/a"})
    assert a.session.id == b.session.id and a.session.native_id.startswith("cwd-")
    assert a.session.state == "idle"


def test_translate_rejects_bad_input() -> None:
    with pytest.raises(HookError):
        translate("herdr", claude("Stop"))
    with pytest.raises(HookError):
        translate("claude", {"session_id": "x"})
    with pytest.raises(HookError):
        translate("claude", [])  # type: ignore[arg-type]


def test_helpers() -> None:
    assert event_name({"hookEventName": "userPromptSubmitted"}) == "UserPromptSubmit"
    assert tool_input({"toolArgs": "not json"}) == "not json"
    assert tool_input({"tool_input": {"a": 1}}) == {"a": 1}


# ---------------------------------------------------------------- decisions


def test_claude_permission_decisions() -> None:
    event = claude(
        "PermissionRequest", tool_name="Bash", tool_input={"command": "npm test"}
    )
    allow = decision_output("claude", event, {"decision": "allow"})
    assert allow == {
        "hookSpecificOutput": {
            "hookEventName": "PermissionRequest",
            "decision": {"behavior": "allow"},
        }
    }
    always = decision_output("claude", event, {"decision": "always"})
    assert always is not None
    assert always["hookSpecificOutput"]["decision"]["updatedPermissions"] == [
        {
            "type": "addRules",
            "rules": [{"toolName": "Bash", "ruleContent": "npm test"}],
            "behavior": "allow",
            "destination": "session",
        }
    ]
    deny = decision_output("claude", event, {"decision": "deny"})
    assert deny is not None
    assert deny["hookSpecificOutput"]["decision"] == {
        "behavior": "deny",
        "message": "Refusé depuis omnigroup",
        "interrupt": False,
    }
    assert decision_output("claude", event, {"decision": "maybe"}) is None


def test_claude_question_answers() -> None:
    event = claude("PreToolUse", tool_name="AskUserQuestion", tool_input=ASK_INPUT)
    out = decision_output("claude", event, {"answers": {"Quelle base ?": ["A", "B"]}})
    assert out == {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "allow",
            "updatedInput": {
                "questions": ASK_INPUT["questions"],
                "answers": {"Quelle base ?": "A, B"},
            },
        }
    }
    assert decision_output("claude", event, {"answers": {}}) is None


def test_copilot_permission_decisions() -> None:
    event = {"hookEventName": "permissionRequest", "toolName": "shell"}
    assert decision_output("copilot", event, {"decision": "always"}) == {
        "behavior": "allow"
    }
    assert decision_output(
        "copilot", event, {"decision": "deny", "message": "non"}
    ) == {
        "behavior": "deny",
        "message": "non",
    }
    assert decision_output("copilot", {"hookEventName": "agentStop"}, {}) is None
