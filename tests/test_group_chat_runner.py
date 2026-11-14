"""chat_runner: stream-json parsing, argv building and one turn against a fake `claude`."""

from __future__ import annotations

import json
import sys
import threading
from pathlib import Path

import pytest

from omniscient.group import chat_runner as cr

FAKE_CLAUDE = r"""
import json, sys, time
args = sys.argv[1:]
prompt = sys.stdin.read()
resume = args[args.index("--resume") + 1] if "--resume" in args else None
sid = resume or "sess-new"
def out(obj):
    print(json.dumps(obj), flush=True)
out({"type": "system", "subtype": "init", "session_id": sid})
if prompt.startswith("sleep"):
    time.sleep(30)
if prompt.startswith("crash"):
    sys.stderr.write("boom token=sk-ant-abcdefghijklmnopqrstuvwxyz0123\n")
    sys.exit(3)
out({"type": "assistant", "parent_tool_use_id": None, "session_id": sid,
     "message": {"content": [{"type": "text", "text": "echo: " + prompt},
                             {"type": "tool_use", "name": "Bash", "input": {"command": "ls"}}]}})
out({"type": "assistant", "parent_tool_use_id": "toolu_1", "session_id": sid,
     "message": {"content": [{"type": "text", "text": "from a subagent"}]}})
print("not json", flush=True)
out({"type": "result", "subtype": "success", "is_error": False, "result": "done",
     "total_cost_usd": 0.0123, "session_id": sid})
"""


@pytest.fixture
def fake_claude(tmp_path: Path) -> list[str]:
    script = tmp_path / "fake_claude.py"
    script.write_text(FAKE_CLAUDE, encoding="utf-8")
    return [sys.executable, str(script)]


def test_parse_init_assistant_and_result() -> None:
    init = cr.parse_line(
        json.dumps({"type": "system", "subtype": "init", "session_id": "s1"})
    )
    assert [(e.kind, e.session_id) for e in init] == [("init", "s1")]
    line = json.dumps(
        {
            "type": "assistant",
            "parent_tool_use_id": None,
            "message": {
                "content": [
                    {"type": "text", "text": "Bonjour"},
                    {"type": "thinking", "thinking": "hidden"},
                    {
                        "type": "tool_use",
                        "name": "Read",
                        "input": {"file_path": "a.py"},
                    },
                ]
            },
        }
    )
    events = cr.parse_line(line)
    assert [(e.kind, e.text, e.tool) for e in events] == [
        ("text", "Bonjour", None),
        ("tool", "a.py", "Read"),
    ]
    result = cr.parse_line(
        json.dumps(
            {
                "type": "result",
                "is_error": True,
                "result": "raté",
                "total_cost_usd": 0.5,
            }
        )
    )
    assert (
        result[0].kind == "result" and result[0].is_error and result[0].cost_usd == 0.5
    )


def test_parse_ignores_noise_and_subagents() -> None:
    assert cr.parse_line("") == ()
    assert cr.parse_line("{not json") == ()
    assert cr.parse_line("[1, 2]") == ()
    assert cr.parse_line(json.dumps({"type": "user", "message": {}})) == ()
    sub = {"type": "assistant", "parent_tool_use_id": "toolu_x",
           "message": {"content": [{"type": "text", "text": "x"}]}}  # fmt: skip
    assert cr.parse_line(json.dumps(sub)) == ()


def test_tool_summary_is_short_and_redacted() -> None:
    secret = "sk-ant-abcdefghijklmnopqrstuvwxyz0123456789"
    events = cr.parse_line(
        json.dumps(
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "name": "Bash",
                            "input": {"command": f"echo {secret} " + "x" * 500},
                        }
                    ]
                },
            }
        )
    )
    assert secret not in (events[0].text or "")
    assert len(events[0].text or "") <= cr.TOOL_SUMMARY_MAX + 1


def test_build_argv_is_a_list_without_shell() -> None:
    assert cr.build_argv(["claude"]) == [
        "claude",
        "-p",
        "--output-format",
        "stream-json",
        "--verbose",
    ]
    argv = cr.build_argv(["claude"], resume="abc-123", max_budget_usd=2.5)
    assert argv[-4:] == ["--resume", "abc-123", "--max-budget-usd", "2.5"]
    with pytest.raises(ValueError):
        cr.build_argv(["claude"], resume="bad id; rm -rf /")


def test_run_turn_streams_events_and_outcome(
    fake_claude: list[str], tmp_path: Path
) -> None:
    seen: list[cr.RunnerEvent] = []
    spec = cr.TurnSpec(argv=cr.build_argv(fake_claude), cwd=tmp_path, text="salut")
    outcome = cr.run_turn(spec, seen.append, cr.TurnHandle())
    assert outcome.ok and outcome.session_id == "sess-new"
    assert outcome.cost_usd == pytest.approx(0.0123)
    kinds = [e.kind for e in seen]
    assert kinds == ["init", "text", "tool", "result"]
    assert seen[1].text == "echo: salut"


def test_run_turn_resume_and_failure(fake_claude: list[str], tmp_path: Path) -> None:
    spec = cr.TurnSpec(
        argv=cr.build_argv(fake_claude, resume="sess-old"), cwd=tmp_path, text="re"
    )
    assert cr.run_turn(spec, lambda e: None, cr.TurnHandle()).session_id == "sess-old"
    bad = cr.TurnSpec(argv=cr.build_argv(fake_claude), cwd=tmp_path, text="crash")
    outcome = cr.run_turn(bad, lambda e: None, cr.TurnHandle())
    assert not outcome.ok
    assert "sk-ant-abcdefghijklmnopqrstuvwxyz0123" not in outcome.error
    assert "3" in outcome.error


def test_run_turn_missing_binary(tmp_path: Path) -> None:
    spec = cr.TurnSpec(argv=["/nonexistent/claude-bin", "-p"], cwd=tmp_path, text="x")
    outcome = cr.run_turn(spec, lambda e: None, cr.TurnHandle())
    assert not outcome.ok and "introuvable" in outcome.error


def test_run_turn_timeout_and_stop(fake_claude: list[str], tmp_path: Path) -> None:
    spec = cr.TurnSpec(
        argv=cr.build_argv(fake_claude), cwd=tmp_path, text="sleep", timeout_s=0.5
    )
    outcome = cr.run_turn(spec, lambda e: None, cr.TurnHandle(), grace_s=0.2)
    assert not outcome.ok and "délai" in outcome.error
    handle = cr.TurnHandle()
    spec2 = cr.TurnSpec(argv=cr.build_argv(fake_claude), cwd=tmp_path, text="sleep")
    timer = threading.Timer(0.5, handle.stop)
    timer.start()
    outcome2 = cr.run_turn(spec2, lambda e: None, handle, grace_s=0.2)
    timer.join()
    assert not outcome2.ok and outcome2.stopped
