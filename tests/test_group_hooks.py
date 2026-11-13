"""omnigroup hooks: event translation, decision rendering and the fail-open hook command."""

from __future__ import annotations

import io
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from omniscient.group.hook_client import HookContext, default_context, main, run_hook
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
    assert t.pending.payload == {"tool_input": '{"command": "ls"}'}
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
    event = claude("PermissionRequest", tool_name="Bash")
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
            "rules": [{"toolName": "Bash"}],
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


# ---------------------------------------------------------------- hook command


class FakeDaemon:
    """Records calls; answers /hook and /pending/<id>/wait from scripted replies."""

    def __init__(self, hook: Any, waits: list[Any] | None = None, fail: bool = False):
        self.hook, self.waits, self.fail = hook, list(waits or []), fail
        self.calls: list[tuple[str, str, Any, float]] = []
        self.now = 0.0  # fake clock: an "open" reply takes its timeout (or open_takes)
        self.open_takes = 0.0

    def clock(self) -> float:
        return self.now

    def __call__(
        self,
        method: str,
        url: str,
        headers: dict[str, str],
        body: bytes | None,
        t: float,
    ) -> Any:
        assert headers["Authorization"] == "Bearer tok"
        self.calls.append((method, url, json.loads(body) if body else None, t))
        if self.fail:
            raise ConnectionRefusedError("down")
        if method == "POST":
            return self.hook
        reply = self.waits.pop(0)
        if reply.get("status") == "open":
            self.now += self.open_takes or float(url.rsplit("=", 1)[1])
        return reply


def make_ctx(
    tmp_path: Path, daemon: FakeDaemon, token: str | None = "tok"
) -> HookContext:
    state = tmp_path / "state"
    state.mkdir(exist_ok=True)
    if token is not None:
        (state / "group.token").write_text(token + "\n", encoding="utf-8")
    return HookContext(
        state_dir=state,
        environ={"CLAUDE_CODE_ENTRYPOINT": "cli"},
        transport=daemon,
        copilot_hooks=tmp_path / "copilot.json",
        clock=daemon.clock,
    )


def run(
    ctx: HookContext, event: dict[str, Any], source: str = "claude", **kw: Any
) -> str:
    out = io.StringIO()
    assert run_hook(source, io.StringIO(json.dumps(event)), out, ctx, **kw) == 0
    return out.getvalue()


PERMISSION = claude("PermissionRequest", tool_name="Bash", tool_input={"command": "ls"})


def test_permission_waits_and_prints_the_decision(tmp_path: Path) -> None:
    daemon = FakeDaemon(
        {"pending_id": PID, "wait_s": 100},
        [{"status": "open"}, {"status": "answered", "answer": {"decision": "allow"}}],
    )
    daemon.open_takes = 40.0  # e.g. the daemon restarted mid-wait
    out = run(make_ctx(tmp_path, daemon), PERMISSION)
    assert json.loads(out)["hookSpecificOutput"]["decision"] == {"behavior": "allow"}
    method, url, body, timeout = daemon.calls[0]
    assert (method, url, timeout) == ("POST", "http://127.0.0.1:7341/hook", 2.0)
    assert body["source"] == "claude"
    assert body["event"]["omnigroup"] == {"entrypoint": "cli", "fast": False}
    assert daemon.calls[1][1].endswith(f"/pending/{PID}/wait?timeout=100")
    assert daemon.calls[2][1].endswith(f"/pending/{PID}/wait?timeout=60")


def test_expired_pending_prints_nothing(tmp_path: Path) -> None:
    daemon = FakeDaemon({"pending_id": PID, "wait_s": 100}, [{"status": "expired"}])
    assert run(make_ctx(tmp_path, daemon), PERMISSION) == ""


def test_wait_stops_at_the_deadline(tmp_path: Path) -> None:
    daemon = FakeDaemon({"pending_id": PID, "wait_s": 30}, [{"status": "open"}] * 10)
    assert run(make_ctx(tmp_path, daemon), PERMISSION) == ""
    assert len(daemon.calls) == 2  # POST + one 30 s poll, then the deadline


@pytest.mark.parametrize("token", [None, ""])
def test_no_token_means_silence_and_no_call(tmp_path: Path, token: str | None) -> None:
    daemon = FakeDaemon({})
    assert run(make_ctx(tmp_path, daemon, token=token), PERMISSION) == ""
    assert daemon.calls == []


def test_daemon_down_fails_open(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    daemon = FakeDaemon({}, fail=True)
    assert run(make_ctx(tmp_path, daemon), PERMISSION) == ""
    assert "omnigroup hook ignoré" in capsys.readouterr().err


def test_bad_stdin_and_bad_pending_id_fail_open(tmp_path: Path) -> None:
    ctx = make_ctx(tmp_path, FakeDaemon({"pending_id": "../../x"}))
    out = io.StringIO()
    assert run_hook("claude", io.StringIO("not json"), out, ctx) == 0
    assert run(ctx, PERMISSION) == ""


def test_non_local_daemon_url_is_refused(tmp_path: Path) -> None:
    daemon = FakeDaemon({})
    ctx = make_ctx(tmp_path, daemon)
    ctx = HookContext(ctx.state_dir, {"OMNIGROUP_URL": "http://evil:1"}, daemon)
    assert run(ctx, PERMISSION) == "" and daemon.calls == []


def test_fast_entry_posts_with_short_timeout_and_never_waits(tmp_path: Path) -> None:
    daemon = FakeDaemon({"pending_id": PID, "wait_s": 100})
    run(make_ctx(tmp_path, daemon), claude("Stop"), fast=True)
    assert [(c[0], c[3]) for c in daemon.calls] == [("POST", 1.0)]


def test_pre_tool_use_is_handled_by_exactly_one_entry(tmp_path: Path) -> None:
    ask = claude("PreToolUse", tool_name="AskUserQuestion", tool_input=ASK_INPUT)
    bash = claude("PreToolUse", tool_name="Bash")
    daemon = FakeDaemon({"pending_id": None})
    ctx = make_ctx(tmp_path, daemon)
    run(ctx, ask, fast=True)  # the "*" entry skips AskUserQuestion
    run(ctx, bash, fast=False)  # the AskUserQuestion entry skips other tools
    assert daemon.calls == []
    run(ctx, ask, fast=False)
    run(ctx, bash, fast=True)
    assert len(daemon.calls) == 2


def test_question_answer_is_printed(tmp_path: Path) -> None:
    daemon = FakeDaemon(
        {"pending_id": PID, "wait_s": 100},
        [{"status": "answered", "answer": {"answers": {"Quelle base ?": "SQLite"}}}],
    )
    ask = claude("PreToolUse", tool_name="AskUserQuestion", tool_input=ASK_INPUT)
    out = json.loads(run(make_ctx(tmp_path, daemon), ask))
    assert out["hookSpecificOutput"]["updatedInput"]["answers"] == {
        "Quelle base ?": "SQLite"
    }


def test_copilot_event_name_comes_from_the_flag(tmp_path: Path) -> None:
    daemon = FakeDaemon(
        {"pending_id": PID, "wait_s": 999},
        [{"status": "answered", "answer": {"decision": "deny"}}],
    )
    event = {"cwd": "/w", "toolName": "shell", "toolArgs": "{}"}
    out = run(
        make_ctx(tmp_path, daemon), event, "copilot", event_arg="permissionRequest"
    )
    assert json.loads(out) == {"behavior": "deny", "message": "Refusé depuis omnigroup"}
    assert daemon.calls[0][2]["event"]["hookEventName"] == "permissionRequest"
    assert daemon.calls[1][1].endswith("timeout=25")  # capped to the Copilot wait


def test_copilot_payload_in_claude_settings(tmp_path: Path) -> None:
    event = {"hookEventName": "PreToolUse", "sessionId": "c1", "toolName": "x"}
    daemon = FakeDaemon({"pending_id": PID, "wait_s": 100})
    ctx = make_ctx(tmp_path, daemon)
    run(ctx, event, fast=True)
    assert daemon.calls[0][2]["source"] == "copilot"
    assert daemon.calls[0][2]["event"]["omnigroup"]["fast"] is True
    ctx.copilot_hooks.write_text('{"x": "omniscient group hook"}', encoding="utf-8")
    run(ctx, event, fast=True)
    assert len(daemon.calls) == 1  # the Copilot hook reports it instead


def test_default_context_and_main(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert default_context(tmp_path).state_dir == tmp_path.resolve() / ".omniscient"
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(PERMISSION)))
    assert main(["--source", "claude", "--home", str(tmp_path)]) == 0
