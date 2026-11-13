"""omnigroup end to end, offline: real daemon (ephemeral port, tmp home), real hook
client fed recorded Claude Code payloads on a fake stdin, answers posted over HTTP."""

from __future__ import annotations

import http.client
import io
import json
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from omniscient.group import server as srv
from omniscient.group.hook_client import HookContext, run_hook, run_statusline
from omniscient.home import Home

SID = "5b0c7a52-1f3e-4c1a-9a57-0d7f3c2e9b11"
PERMISSION_REQUEST = {
    "session_id": SID,
    "transcript_path": "/tmp/none.jsonl",
    "cwd": "/work/demo",
    "permission_mode": "default",
    "hook_event_name": "PermissionRequest",
    "tool_name": "Bash",
    "tool_input": {"command": "ls -la", "description": "Lister les fichiers"},
    "permission_suggestions": [
        {
            "type": "addRules",
            "rules": [{"toolName": "Bash"}],
            "behavior": "allow",
            "destination": "localSettings",
        }
    ],
}
QUESTIONS = [
    {
        "question": "Quelle base ?",
        "header": "Base",
        "multiSelect": False,
        "options": [
            {"label": "SQLite", "description": "fichier"},
            {"label": "Postgres", "description": "serveur"},
        ],
    }
]
ASK_USER_QUESTION = {
    "session_id": SID,
    "cwd": "/work/demo",
    "hook_event_name": "PreToolUse",
    "tool_name": "AskUserQuestion",
    "tool_input": {"questions": QUESTIONS},
    "tool_use_id": "toolu_01",
}
STATUS_LINE = {
    "session_id": SID,
    "model": {"id": "claude-test", "display_name": "Test"},
    "context_window": {
        "context_window_size": 200000,
        "total_input_tokens": 50000,
        "used_percentage": 25,
    },
    "cost": {"total_cost_usd": 0.42},
    "rate_limits": {"five_hour": {"used_percentage": 12, "resets_at": 1900000000}},
}


WIDGET: dict[int, str] = {}  # server port -> the widget token paired in the fixture


@pytest.fixture
def daemon(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[srv.GroupServer]:
    # A proxy that does not exist: the hook client must never go through it (M7).
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:9")
    monkeypatch.setenv("no_proxy", "")
    server = srv.create_server(
        Home(tmp_path / "home"), port=0, show_code=lambda code: True
    )
    server.start_background()
    server.app.presence.touch()  # a widget is connected
    WIDGET[server.port] = server.app.widgets.mint("e2e")
    yield server
    server.close_all()


def context(server: srv.GroupServer, tmp_path: Path) -> HookContext:
    return HookContext(
        state_dir=server.app.home.state,
        environ={"OMNIGROUP_URL": f"http://127.0.0.1:{server.port}"},
        copilot_hooks=tmp_path / "no-copilot.json",
    )


def call(
    server: srv.GroupServer,
    method: str,
    path: str,
    body: Any = None,
    token: str | None = None,
    status: int = 200,
) -> Any:
    conn = http.client.HTTPConnection("127.0.0.1", server.port, timeout=5)
    headers = {"Authorization": f"Bearer {token or WIDGET[server.port]}"}
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    conn.request(method, path, body=data, headers=headers)
    resp = conn.getresponse()
    payload = json.loads(resp.read())
    conn.close()
    assert resp.status == status, payload
    return payload


def run_in_thread(
    event: dict[str, Any], ctx: HookContext
) -> tuple[threading.Thread, io.StringIO]:
    out = io.StringIO()
    thread = threading.Thread(
        target=run_hook,
        args=("claude", io.StringIO(json.dumps(event)), out, ctx),
        daemon=True,
    )
    thread.start()
    return thread, out


def open_pending(server: srv.GroupServer) -> dict[str, Any]:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        sessions = call(server, "GET", "/sessions")["sessions"]
        pending = [p for s in sessions for p in s["pending"]]
        if pending:
            return pending[0]
        time.sleep(0.05)
    raise AssertionError("the hook never opened a pending")


def test_permission_request_allowed_from_the_widget(
    daemon: srv.GroupServer, tmp_path: Path
) -> None:
    thread, out = run_in_thread(PERMISSION_REQUEST, context(daemon, tmp_path))
    pending = open_pending(daemon)
    assert pending["kind"] == "permission" and pending["tool"] == "Bash"
    assert pending["options"] == ["allow", "deny", "always"]
    call(daemon, "POST", f"/pending/{pending['id']}/answer", {"decision": "allow"})
    thread.join(10)
    assert not thread.is_alive()
    assert json.loads(out.getvalue()) == {
        "hookSpecificOutput": {
            "hookEventName": "PermissionRequest",
            "decision": {"behavior": "allow"},
        }
    }
    session = call(daemon, "GET", f"/sessions/claude:{SID}")
    assert session["state"] == "working" and session["pending"] == []


def test_ask_user_question_answered_from_the_widget(
    daemon: srv.GroupServer, tmp_path: Path
) -> None:
    thread, out = run_in_thread(ASK_USER_QUESTION, context(daemon, tmp_path))
    pending = open_pending(daemon)
    assert pending["kind"] == "question"
    assert pending["options"] == ["SQLite", "Postgres"]
    answer = {"answers": {"Quelle base ?": "Postgres"}}
    call(daemon, "POST", f"/pending/{pending['id']}/answer", answer)
    thread.join(10)
    assert not thread.is_alive()
    assert json.loads(out.getvalue()) == {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "allow",
            "updatedInput": {
                "questions": QUESTIONS,
                "answers": {"Quelle base ?": "Postgres"},
            },
        }
    }


def test_status_line_usage_shows_in_sessions(
    daemon: srv.GroupServer, tmp_path: Path
) -> None:
    ctx = context(daemon, tmp_path)
    start = {"session_id": SID, "cwd": "/work/demo", "hook_event_name": "SessionStart"}
    assert run_hook("claude", io.StringIO(json.dumps(start)), io.StringIO(), ctx) == 0
    line = io.StringIO()
    assert run_statusline(io.StringIO(json.dumps(STATUS_LINE)), line, ctx) == 0
    assert "contexte 25 %" in line.getvalue()
    sessions = call(daemon, "GET", "/sessions")["sessions"]
    assert len(sessions) == 1
    usage = sessions[0]["usage"]
    assert usage["cost_usd"] == 0.42 and usage["context_tokens"] == 50000
    assert usage["context_limit"] == 200000 and usage["plan_used_pct"] == 12.0
    assert sessions[0]["state"] == "idle"  # as SessionStart left it: no state change


def test_always_adds_the_exact_rule_and_the_hook_credential_cannot_answer(
    daemon: srv.GroupServer, tmp_path: Path
) -> None:
    thread, out = run_in_thread(PERMISSION_REQUEST, context(daemon, tmp_path))
    pending = open_pending(daemon)
    assert pending["payload"]["always_rules"] == ["Bash(ls -la)"]
    hook = daemon.app.hook_token  # what any agent can read on disk
    path = f"/pending/{pending['id']}/answer"
    call(daemon, "POST", path, {"decision": "allow"}, token=hook, status=403)
    call(daemon, "GET", "/sessions", token=hook, status=403)
    call(daemon, "POST", path, {"decision": "always"})
    thread.join(10)
    decision = json.loads(out.getvalue())["hookSpecificOutput"]["decision"]
    assert decision["updatedPermissions"] == [
        {
            "type": "addRules",
            "rules": [{"toolName": "Bash", "ruleContent": "ls -la"}],
            "behavior": "allow",
            "destination": "session",
        }
    ]


def test_without_a_widget_the_hook_never_waits(
    daemon: srv.GroupServer, tmp_path: Path
) -> None:
    from omniscient.group.gates import Presence

    object.__setattr__(daemon.app, "presence", Presence())  # never connected
    started = time.monotonic()
    out = io.StringIO()
    ctx = context(daemon, tmp_path)
    run_hook("claude", io.StringIO(json.dumps(PERMISSION_REQUEST)), out, ctx)
    assert out.getvalue() == "" and time.monotonic() - started < 3
    session = call(daemon, "GET", f"/sessions/claude:{SID}")
    assert session["pending"] == []
