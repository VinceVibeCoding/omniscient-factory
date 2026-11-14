"""transcript_view: a running session's conversation, read from its Claude transcript."""

from __future__ import annotations

import json
from pathlib import Path

from omniscient.group import transcript_view as tv
from omniscient.group.models import JumpTarget, Session, utcnow

SID = "0b6c7c1e-1111-4222-8333-444455556666"


def session(
    source: str = "claude", native: str = SID, jump: JumpTarget | None = None
) -> Session:
    now = utcnow()
    return Session(
        id=f"{source}:{native}", source=source, native_id=native, title="demo",  # type: ignore[arg-type]
        cwd=None, state="idle", started_at=now, updated_at=now, jump=jump,
    )  # fmt: skip


def write_transcript(claude_dir: Path, lines: list[dict]) -> Path:
    folder = claude_dir / "projects" / "-tmp-demo"
    folder.mkdir(parents=True)
    path = folder / f"{SID}.jsonl"
    path.write_text("\n".join(json.dumps(x) for x in lines) + "\n", encoding="utf-8")
    return path


LINES = [
    {"type": "user", "timestamp": "2026-11-14T10:00:00Z",
     "message": {"role": "user", "content": "Bonjour, lis config password=hunter2hunter2"}},
    {"type": "user", "isMeta": True, "timestamp": "2026-11-14T10:00:01Z",
     "message": {"role": "user", "content": "<command-name>/clear</command-name>"}},
    {"type": "assistant", "timestamp": "2026-11-14T10:00:02Z",
     "message": {"content": [{"type": "thinking", "thinking": "secret thoughts"},
                             {"type": "text", "text": "Voici la réponse."},
                             {"type": "tool_use", "name": "Bash", "input": {"command": "ls"}}]}},
    {"type": "user", "timestamp": "2026-11-14T10:00:03Z",
     "message": {"role": "user", "content": [{"type": "tool_result", "content": "files"}]}},
    {"type": "assistant", "isSidechain": True, "timestamp": "2026-11-14T10:00:04Z",
     "message": {"content": [{"type": "text", "text": "subagent chatter"}]}},
    "garbage line",
]  # fmt: skip


def test_reads_user_and_assistant_text_only(tmp_path: Path) -> None:
    write_transcript(tmp_path, [x for x in LINES if isinstance(x, dict)])
    view = tv.session_transcript(session(), claude_dir=tmp_path, limit=50)
    assert [(m["role"], m["text"]) for m in view["messages"]][1] == (
        "assistant",
        "Voici la réponse.",
    )
    assert len(view["messages"]) == 2
    assert "hunter2hunter2" not in view["messages"][0]["text"]
    assert view["messages"][0]["at"].startswith("2026-11-14T10:00:00")
    assert view["can_prompt"] is False


def test_limit_and_garbage(tmp_path: Path) -> None:
    path = write_transcript(tmp_path, [LINES[0]] * 10)
    with path.open("a", encoding="utf-8") as fh:
        fh.write("not json\n[1]\n")
    view = tv.session_transcript(session(), claude_dir=tmp_path, limit=3)
    assert len(view["messages"]) == 3


def test_missing_transcript_and_bad_ids(tmp_path: Path) -> None:
    assert tv.session_transcript(session(), claude_dir=tmp_path)["messages"] == []
    assert (
        tv.session_transcript(session(native="../../etc"), claude_dir=tmp_path)[
            "messages"
        ]
        == []
    )
    herdr = session("herdr", native="w1:p2")
    view = tv.session_transcript(herdr, claude_dir=tmp_path)
    assert view == {"messages": [], "can_prompt": True}


def test_can_prompt_for_herdr_jump(tmp_path: Path) -> None:
    s = session(jump=JumpTarget("herdr", "w1:p2", "herdr"))
    assert tv.session_transcript(s, claude_dir=tmp_path)["can_prompt"] is True


def test_known_path_must_stay_under_projects(tmp_path: Path) -> None:
    path = write_transcript(tmp_path, [LINES[0]])
    assert tv.find_transcript(session(), tmp_path, known=str(path)) == path
    outside = tmp_path / "evil.jsonl"
    outside.write_text(json.dumps(LINES[0]) + "\n", encoding="utf-8")
    assert tv.find_transcript(session(), tmp_path, known=str(outside)) == path


def test_claude_dir_from_env(tmp_path: Path) -> None:
    assert tv.claude_dir({"CLAUDE_CONFIG_DIR": str(tmp_path)}) == tmp_path
    assert tv.claude_dir({}) == Path.home() / ".claude"
