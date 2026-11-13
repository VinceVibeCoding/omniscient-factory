"""omnigroup usage: Claude transcript tail, statusLine JSON, and the statusline command."""

from __future__ import annotations

import io
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from omniscient.group.hook_client import HookContext, run_statusline, status_text
from omniscient.group.hook_usage import (
    LARGE_CONTEXT,
    TAIL_BYTES,
    UsageCursor,
    context_limit,
    is_status_event,
    read_transcript,
    translate_usage,
)
from omniscient.group.hooks import HookError, translate


def reply(mid: str, inp: int, out: int, read: int, write: int, **kw: Any) -> str:
    usage = {
        "input_tokens": inp,
        "output_tokens": out,
        "cache_read_input_tokens": read,
        "cache_creation_input_tokens": write,
    }
    message = {"id": mid, "model": "claude-test-1", "usage": usage}
    return json.dumps({"type": "assistant", "message": message, **kw}) + "\n"


def transcript(tmp_path: Path, *lines: str) -> Path:
    path = tmp_path / "session.jsonl"
    path.write_text("".join(lines), encoding="utf-8")
    return path


def stop(path: Path, name: str = "Stop") -> dict[str, Any]:
    return {"session_id": "s", "hook_event_name": name, "transcript_path": str(path)}


STATUS = {
    "hook_event_name": "StatusLine",
    "session_id": "s",
    "model": {"id": "claude-test-1", "display_name": "Test"},
    "cost": {"total_cost_usd": 1.25},
    "context_window": {
        "total_input_tokens": 50_000,
        "context_window_size": 1_000_000,
        "used_percentage": 5,
    },
    "rate_limits": {
        "five_hour": {"used_percentage": 42.5, "resets_at": 1_800_000_000},
        "seven_day": {"used_percentage": 10, "resets_at": 1_800_500_000},
    },
}


def test_transcript_totals_and_last_context(tmp_path: Path) -> None:
    path = transcript(
        tmp_path,
        json.dumps({"type": "user", "message": {"content": "hi"}}) + "\n",
        reply("m1", 2, 10, 100, 1000),
        reply("m1", 2, 10, 100, 1000),  # same reply, second content block
        reply("side", 9, 9, 9, 9, isSidechain=True),
        '{not json with "usage"\n',
        reply("m2", 3, 20, 1100, 50),
    )
    usage, cursor = translate_usage("claude", stop(path))
    assert usage is not None
    assert (usage.input_tokens, usage.output_tokens) == (5, 30)
    assert (usage.cache_read_tokens, usage.cache_write_tokens) == (1200, 1050)
    assert usage.context_tokens == 3 + 1100 + 50
    assert usage.context_limit == 200_000 and usage.model == "claude-test-1"
    assert usage.cost_usd is None and usage.plan_used_pct is None
    assert cursor.offset == path.stat().st_size


def test_incremental_reads_and_half_written_lines(tmp_path: Path) -> None:
    path = transcript(tmp_path, reply("m1", 1, 1, 0, 0))
    _, cursor = translate_usage("claude", stop(path))
    partial = reply("m2", 1, 5, 0, 0)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(partial[:20])
    usage, cursor = translate_usage("claude", stop(path, "PostToolUse"), cursor)
    assert usage is not None and usage.output_tokens == 1
    with path.open("a", encoding="utf-8") as fh:
        fh.write(partial[20:])
    usage, _ = translate_usage("claude", stop(path, "SubagentStop"), cursor)
    assert usage is not None and usage.output_tokens == 6


def test_big_unseen_transcript_reads_only_its_tail(tmp_path: Path) -> None:
    filler = json.dumps({"type": "user", "pad": "x" * 1000}) + "\n"
    path = transcript(
        tmp_path, reply("old", 1, 1000, 0, 0), filler * 400, reply("new", 1, 7, 0, 0)
    )
    assert path.stat().st_size > TAIL_BYTES
    cursor = read_transcript(path, UsageCursor())
    assert cursor.output_tokens == 7


def test_rewritten_transcript_restarts(tmp_path: Path) -> None:
    path = transcript(tmp_path, reply("a", 1, 1, 0, 0), reply("b", 1, 1, 0, 0))
    cursor = read_transcript(path, UsageCursor())
    path.write_text(reply("c", 1, 4, 0, 0), encoding="utf-8")
    assert read_transcript(path, cursor).output_tokens == 4 + 2
    other = tmp_path / "other.jsonl"
    other.write_text("", encoding="utf-8")
    assert read_transcript(other, cursor).output_tokens == 0  # new file: fresh totals


def test_statusline_fills_cost_plan_and_window(tmp_path: Path) -> None:
    usage, cursor = translate_usage("claude", STATUS)
    assert usage is not None
    assert usage.context_limit == 1_000_000 and usage.context_tokens == 50_000
    assert usage.cost_usd == 1.25 and usage.plan_used_pct == 42.5
    assert usage.plan_resets_at == datetime.fromtimestamp(1_800_000_000, UTC)
    path = transcript(tmp_path, reply("m", 1, 2, 3, 4))
    usage, _ = translate_usage("claude", stop(path), cursor)
    assert usage is not None and usage.cost_usd == 1.25
    assert usage.context_tokens == 8 and usage.context_limit == 1_000_000


def test_usage_is_best_effort() -> None:
    assert translate_usage("copilot", {"hookEventName": "agentStop"})[0] is None
    assert translate_usage("claude", {"hook_event_name": "UserPromptSubmit"})[0] is None
    assert translate_usage("claude", {"no": "name"})[0] is None
    missing = stop(Path("/nonexistent/x.jsonl"))
    assert translate_usage("claude", missing)[0] is None
    assert translate_usage("claude", {**missing, "transcript_path": 3})[0] is None


def test_context_limit_table() -> None:
    assert context_limit(None, 10) == 200_000
    assert context_limit("claude-x[1m]", 10) == LARGE_CONTEXT
    assert context_limit("claude-x", 250_000) == LARGE_CONTEXT


def test_status_events_are_usage_only() -> None:
    assert is_status_event(STATUS) and not is_status_event({"x": 1})
    with pytest.raises(HookError):
        translate("claude", STATUS)


def test_status_text() -> None:
    assert (
        status_text(STATUS) == "omnigroup · Test · contexte 5 % · 5 h 42 % · 7 j 10 %"
    )
    assert status_text({}) == "omnigroup"


def _ctx(tmp_path: Path, calls: list[Any], token: bool = True) -> HookContext:
    (tmp_path / "state").mkdir(exist_ok=True)
    if token:
        (tmp_path / "state" / "group.token").write_text("tok", encoding="utf-8")

    def transport(*args: Any) -> Any:
        calls.append(args)
        raise ConnectionRefusedError("down")

    return HookContext(tmp_path / "state", {}, transport)


def test_statusline_command_prints_then_forwards(tmp_path: Path) -> None:
    calls: list[Any] = []
    out = io.StringIO()
    raw = {k: v for k, v in STATUS.items() if k != "hook_event_name"}
    run_statusline(io.StringIO(json.dumps(raw)), out, _ctx(tmp_path, calls))
    assert out.getvalue().startswith("omnigroup · Test")
    method, url, _headers, body, timeout = calls[0]
    assert (method, url, timeout) == ("POST", "http://127.0.0.1:7341/hook", 0.5)
    assert json.loads(body)["event"]["hook_event_name"] == "StatusLine"


def test_statusline_command_without_daemon_or_input(tmp_path: Path) -> None:
    calls: list[Any] = []
    out = io.StringIO()
    run_statusline(io.StringIO(json.dumps(STATUS)), out, _ctx(tmp_path, calls, False))
    run_statusline(io.StringIO("nope"), out, _ctx(tmp_path, calls, False))
    assert calls == [] and out.getvalue().splitlines()[-1] == "omnigroup"
