"""omnigroup hook command: posting, waiting with a ticket, failing open."""

from __future__ import annotations

import http.client
import io
import json
from pathlib import Path
from typing import Any

import pytest

from omniscient.group.hook_client import (
    HookContext,
    _base_url,
    default_context,
    main,
    run_hook,
)
from omniscient.group.hooks import HookError
from tests.test_group_hooks import ASK_INPUT, PID, claude

TICKET = "ticket-0123456789abcdef"


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
        if method == "GET":  # every wait carries the ticket /hook returned
            assert headers["X-Omnigroup-Ticket"] == TICKET
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
        {"pending_id": PID, "wait_s": 100, "wait_ticket": TICKET},
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
    daemon = FakeDaemon(
        {"pending_id": PID, "wait_s": 100, "wait_ticket": TICKET},
        [{"status": "expired"}],
    )
    assert run(make_ctx(tmp_path, daemon), PERMISSION) == ""


def test_wait_stops_at_the_deadline(tmp_path: Path) -> None:
    daemon = FakeDaemon(
        {"pending_id": PID, "wait_s": 30, "wait_ticket": TICKET},
        [{"status": "open"}] * 10,
    )
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
    daemon = FakeDaemon({"pending_id": PID, "wait_s": 100, "wait_ticket": TICKET})
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
        {"pending_id": PID, "wait_s": 100, "wait_ticket": TICKET},
        [{"status": "answered", "answer": {"answers": {"Quelle base ?": "SQLite"}}}],
    )
    ask = claude("PreToolUse", tool_name="AskUserQuestion", tool_input=ASK_INPUT)
    out = json.loads(run(make_ctx(tmp_path, daemon), ask))
    assert out["hookSpecificOutput"]["updatedInput"]["answers"] == {
        "Quelle base ?": "SQLite"
    }


def test_copilot_event_name_comes_from_the_flag(tmp_path: Path) -> None:
    daemon = FakeDaemon(
        {"pending_id": PID, "wait_s": 999, "wait_ticket": TICKET},
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
    daemon = FakeDaemon({"pending_id": PID, "wait_s": 100, "wait_ticket": TICKET})
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


def test_no_widget_means_no_wait(tmp_path: Path) -> None:
    daemon = FakeDaemon({"pending_id": None, "wait_s": 0})
    assert run(make_ctx(tmp_path, daemon), PERMISSION) == ""
    assert [c[0] for c in daemon.calls] == ["POST"]


def test_missing_ticket_means_no_wait(tmp_path: Path) -> None:
    daemon = FakeDaemon({"pending_id": PID, "wait_s": 100})
    assert run(make_ctx(tmp_path, daemon), PERMISSION) == ""
    assert [c[0] for c in daemon.calls] == ["POST"]


def test_http_protocol_errors_fail_open(tmp_path: Path) -> None:
    def broken(*args: Any) -> Any:
        raise http.client.RemoteDisconnected("closed")

    ctx = make_ctx(tmp_path, FakeDaemon({}))
    ctx = HookContext(ctx.state_dir, {}, broken)
    assert run(ctx, PERMISSION) == ""


@pytest.mark.parametrize(
    "url",
    [
        "http://evil:1",
        "https://127.0.0.1:7341",
        "http://user:pw@127.0.0.1:7341",
        "http://127.0.0.1.evil.example:7341",
        "http://localhost:7341/path",
        "http://127.0.0.1",
        "http://127.0.0.1:99999",
        "http://[::1]:7341",
    ],
)
def test_daemon_url_must_be_plain_local_http(url: str) -> None:
    with pytest.raises(HookError):
        _base_url({"OMNIGROUP_URL": url})


def test_daemon_url_accepts_local_forms() -> None:
    assert _base_url({}) == "http://127.0.0.1:7341"
    assert _base_url({"OMNIGROUP_URL": "http://localhost:8000/"}) == (
        "http://localhost:8000"
    )
