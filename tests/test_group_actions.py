"""Jump and prompt actions, with fake runners: nothing is launched for real."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from omniscient.group.actions import (
    Env,
    find_code_cmd,
    is_wsl,
    jump,
    prompt,
    safe_url,
)
from omniscient.group.collect_agentrun import AgentRunHttpError
from omniscient.group.models import JumpTarget, Session
from omniscient.studio.herdr import HerdrError

NOW = datetime(2026, 1, 2, 12, 0, tzinfo=UTC)
UUID = "0b1c2d3e-aaaa-bbbb-cccc-000000000001"
CODE_CMD = r"C:\Program Files\Microsoft VS Code\bin\code.cmd"


class Recorder:
    def __init__(self, code: int = 0, out: str = "") -> None:
        self.code, self.out = code, out
        self.calls: list[list[str]] = []

    def __call__(self, argv: Sequence[str]) -> tuple[int, str]:
        self.calls.append(list(argv))
        return self.code, self.out


class HerdrRecorder:
    def __init__(self, error: str | None = None) -> None:
        self.error = error
        self.calls: list[list[str]] = []

    def __call__(self, args: Sequence[str]) -> str:
        self.calls.append(list(args))
        if self.error:
            raise HerdrError(self.error)
        return "{}"


def session(
    source: str = "claude",
    native: str = UUID,
    state: str = "idle",
    jump_to: JumpTarget | None = None,
    meta: dict[str, str] | None = None,
) -> Session:
    return Session(
        id=f"{source}:{native}",
        source=source,  # type: ignore[arg-type]
        native_id=native,
        title="t",
        cwd="/work/demo",
        state=state,  # type: ignore[arg-type]
        started_at=NOW,
        updated_at=NOW,
        jump=jump_to,
        meta=meta or {},
    )


def target(kind: str, value: str) -> JumpTarget:
    return JumpTarget(kind=kind, value=value, label="x")  # type: ignore[arg-type]


def wsl_env(runner: Recorder, herdr: HerdrRecorder | None = None) -> Env:
    return Env(
        runner=runner,
        herdr=herdr or HerdrRecorder(),
        wsl=True,
        environ={"WSL_DISTRO_NAME": "Ubuntu"},
    )


# ---- jump --------------------------------------------------------------------------


def test_jump_without_target_says_so() -> None:
    assert jump(session(), wsl_env(Recorder())).ok is False


def test_jump_vscode_session_opens_the_uri_through_cmd_start() -> None:
    runner = Recorder()
    result = jump(session(jump_to=target("vscode_session", UUID)), wsl_env(runner))
    uri = f"vscode://anthropic.claude-code/open?session={UUID}"
    assert result.ok
    assert runner.calls == [["cmd.exe", "/c", "start", "", uri]]


@pytest.mark.parametrize("bad", ["abc&calc", "a b", "x;rm", "%PATH%"])
def test_jump_vscode_session_refuses_odd_ids(bad: str) -> None:
    runner = Recorder()
    assert not jump(session(jump_to=target("vscode_session", bad)), wsl_env(runner)).ok
    assert runner.calls == []


def test_jump_outside_wsl_returns_the_uri_as_text() -> None:
    runner = Recorder()
    env = Env(runner=runner, wsl=False, environ={})
    result = jump(session(jump_to=target("vscode_session", UUID)), env)
    assert result.ok and result.command is not None and UUID in result.command
    assert runner.calls == []


def test_jump_reports_a_windows_failure() -> None:
    result = jump(
        session(jump_to=target("vscode_session", UUID)), wsl_env(Recorder(code=1))
    )
    assert not result.ok


def test_find_code_cmd_picks_the_cmd_and_refuses_metacharacters() -> None:
    out = "C:\\x\\bin\\code\r\n" + CODE_CMD + "\r\n"
    assert find_code_cmd(wsl_env(Recorder(out=out))) == CODE_CMD
    assert find_code_cmd(wsl_env(Recorder(out="C:\\a&b\\code.cmd\n"))) is None
    assert find_code_cmd(wsl_env(Recorder(code=1))) is None


def test_jump_vscode_folder_runs_code_cmd_with_the_wsl_folder_uri() -> None:
    runner = Recorder(out=CODE_CMD + "\n")
    result = jump(
        session(jump_to=target("vscode_folder", "/work/demo")), wsl_env(runner)
    )
    assert result.ok
    assert runner.calls == [
        ["cmd.exe", "/c", "where", "code"],
        [
            "cmd.exe",
            "/c",
            CODE_CMD,
            "--folder-uri",
            "vscode-remote://wsl+Ubuntu/work/demo",
        ],
    ]


@pytest.mark.parametrize("bad", ["/a b", "/a&b", "relative", "/x/../etc"])
def test_jump_vscode_folder_refuses_unsafe_paths(bad: str) -> None:
    runner = Recorder(out=CODE_CMD)
    assert not jump(session(jump_to=target("vscode_folder", bad)), wsl_env(runner)).ok
    assert runner.calls == []


def test_jump_vscode_folder_without_code_cmd_fails_clearly() -> None:
    result = jump(
        session(jump_to=target("vscode_folder", "/work")), wsl_env(Recorder(out=""))
    )
    assert not result.ok and "code.cmd" in result.message


def test_jump_vscode_folder_outside_wsl_gives_the_code_command() -> None:
    env = Env(runner=Recorder(), wsl=False, environ={})
    result = jump(session(jump_to=target("vscode_folder", "/work/demo")), env)
    assert result.ok and result.command == "code /work/demo"


def test_jump_herdr_focuses_the_pane() -> None:
    herdr = HerdrRecorder()
    assert jump(
        session(jump_to=target("herdr", "w1:p1")), wsl_env(Recorder(), herdr)
    ).ok
    assert herdr.calls == [["agent", "focus", "w1:p1"]]


def test_jump_herdr_reports_herdr_errors() -> None:
    herdr = HerdrRecorder(error="server_not_running")
    result = jump(session(jump_to=target("herdr", "w1:p1")), wsl_env(Recorder(), herdr))
    assert not result.ok and "server_not_running" in result.message


@pytest.mark.parametrize(
    ("url", "ok"),
    [
        ("https://console.example.com/runs/r1", True),
        ("http://127.0.0.1:7340/runs/r1", True),
        ("http://example.com/x", False),
        ("file:///etc/passwd", False),
        ("https://x.com/a b", False),
        ('https://x.com/"&calc', False),
    ],
)
def test_safe_url(url: str, ok: bool) -> None:
    assert safe_url(url) is ok


def test_jump_url_opens_with_explorer() -> None:
    runner = Recorder()
    url = "https://console.example.com/runs/r1"
    assert jump(session(jump_to=target("url", url)), wsl_env(runner)).ok
    assert runner.calls == [["explorer.exe", url]]


def test_jump_url_refuses_other_schemes() -> None:
    runner = Recorder()
    assert not jump(
        session(jump_to=target("url", "javascript:alert(1)")), wsl_env(runner)
    ).ok
    assert runner.calls == []


def test_jump_terminal_returns_the_resume_command() -> None:
    result = jump(session(jump_to=target("terminal", UUID)), wsl_env(Recorder()))
    assert result.ok and result.command == f"claude --resume {UUID}"


def test_is_wsl(tmp_path: Path) -> None:
    version = tmp_path / "version"
    version.write_text("Linux 6.0-microsoft-standard-WSL2", encoding="utf-8")
    assert is_wsl({"WSL_DISTRO_NAME": "Ubuntu"})
    assert is_wsl({}, version)
    assert not is_wsl({}, tmp_path / "missing")


# ---- prompt ------------------------------------------------------------------------


def test_prompt_refuses_empty_or_huge_text() -> None:
    assert not prompt(session(), "   ").ok
    assert not prompt(session(), "x" * 9000).ok


def test_prompt_herdr_idle_uses_agent_prompt_with_argv() -> None:
    herdr = HerdrRecorder()
    text = 'fix "it" && rm -rf /'
    result = prompt(session("herdr", "w1:p1", "idle"), text, wsl_env(Recorder(), herdr))
    assert result.ok
    assert herdr.calls == [["agent", "prompt", "w1:p1", text]]


def test_prompt_herdr_busy_is_refused() -> None:
    herdr = HerdrRecorder()
    assert not prompt(
        session("herdr", "w1:p1", "working"), "go", wsl_env(Recorder(), herdr)
    ).ok
    assert herdr.calls == []


def test_prompt_claude_in_herdr_goes_through_the_pane() -> None:
    herdr = HerdrRecorder()
    s = session(jump_to=target("herdr", "w1:p2"))
    assert prompt(s, "next", wsl_env(Recorder(), herdr)).ok
    assert herdr.calls == [["agent", "prompt", "w1:p2", "next"]]


class FakeApi:
    def __init__(self, error: bool = False) -> None:
        self.error = error
        self.calls: list[tuple[str, str, Mapping[str, Any] | None]] = []

    def __call__(
        self, method: str, path: str, body: Mapping[str, Any] | None = None
    ) -> Any:
        self.calls.append((method, path, body))
        if self.error:
            raise AgentRunHttpError("AgentRun a répondu 409")
        return {}


def test_prompt_agentrun_posts_a_message() -> None:
    api = FakeApi()
    assert prompt(session("agentrun", "r1", "idle"), "continue", agentrun=api).ok
    assert api.calls == [("POST", "/runs/r1/messages", {"content": "continue"})]


def test_prompt_agentrun_working_is_refused_and_errors_are_reported() -> None:
    api = FakeApi()
    assert not prompt(session("agentrun", "r1", "working"), "x", agentrun=api).ok
    assert api.calls == []
    result = prompt(
        session("agentrun", "r1", "done"), "x", agentrun=FakeApi(error=True)
    )
    assert not result.ok and "409" in result.message


def test_prompt_claude_idle_returns_the_command_and_runs_nothing() -> None:
    runner = Recorder()
    result = prompt(session(), "say 'hi'", wsl_env(runner))
    assert result.ok and result.command is not None
    assert result.command.startswith(f"claude -p --resume {UUID} ")
    assert runner.calls == []


def test_prompt_claude_refused_when_busy_or_in_vscode() -> None:
    assert not prompt(session(state="working"), "x").ok
    in_vscode = session(jump_to=target("vscode_session", UUID))
    assert not prompt(in_vscode, "x").ok
    assert not prompt(session(meta={"ide": "vscode"}), "x").ok


def test_prompt_copilot_is_refused() -> None:
    assert not prompt(session("copilot", "abc"), "x").ok


def test_action_result_json() -> None:
    result = jump(session(jump_to=target("terminal", UUID)), wsl_env(Recorder()))
    assert set(result.to_json()) == {"ok", "message", "command"}
