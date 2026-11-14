"""Widget chat (mode B): the conversation of a session already running in an IDE or a
terminal, read from its Claude Code transcript (`<claude dir>/projects/*/<id>.jsonl`).

Only the main conversation's user and assistant text is shown: no thinking, tool calls,
tool results, meta lines or subagent (sidechain) lines. Text is passed through
`redact_secrets`. The transcript format is internal to Claude Code: unknown or broken
lines are skipped. Sending a message reuses POST /sessions/{id}/prompt; `can_prompt`
says whether that can reach the agent (herdr pane or AgentRun run) rather than only
explain how to do it by hand (a VS Code session cannot be typed into from outside).
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

from omniscient.group.models import Session, iso
from omniscient.redact import redact_secrets

TAIL_BYTES = 1024 * 1024
DEFAULT_LIMIT = 50
MESSAGE_MAX = 8000
SESSION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")


def claude_dir(environ: Mapping[str, str] | None = None) -> Path:
    env = os.environ if environ is None else environ
    custom = env.get("CLAUDE_CONFIG_DIR")
    return Path(custom).expanduser() if custom else Path.home() / ".claude"


_default_claude_dir = claude_dir  # `session_transcript` takes a parameter of that name


def can_prompt(session: Session) -> bool:
    if session.source in ("herdr", "agentrun"):
        return True
    return session.jump is not None and session.jump.kind == "herdr"


def _inside(path: Path, folder: Path) -> bool:
    try:
        path.resolve().relative_to(folder.resolve())
    except (OSError, ValueError):
        return False
    return True


def find_transcript(
    session: Session, base: Path, known: str | None = None
) -> Path | None:
    """The session's transcript: the hook-reported path when it lies under
    `<base>/projects`, else a lookup by Claude session id."""
    if session.source != "claude" or not SESSION_ID.match(session.native_id):
        return None
    projects = base / "projects"
    if known:
        candidate = Path(known)
        if (
            candidate.suffix == ".jsonl"
            and candidate.is_file()
            and _inside(candidate, projects)
        ):
            return candidate
    if not projects.is_dir():
        return None
    return next(
        (p for p in projects.glob(f"*/{session.native_id}.jsonl") if p.is_file()), None
    )


def _tail_lines(path: Path) -> list[str]:
    with path.open("rb") as fh:
        size = fh.seek(0, os.SEEK_END)
        start = max(0, size - TAIL_BYTES)
        fh.seek(start)
        data = fh.read()
    lines = data.decode("utf-8", errors="replace").splitlines()
    return lines[1:] if start > 0 else lines  # the first line may be cut


def _text_of(content: Any) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts = [b["text"] for b in content if isinstance(b, dict) and b.get("type") == "text"
             and isinstance(b.get("text"), str)]  # fmt: skip
    return "\n".join(parts)


def _message(data: Any) -> dict[str, Any] | None:
    if not isinstance(data, dict) or data.get("type") not in ("user", "assistant"):
        return None
    if data.get("isMeta") or data.get("isSidechain"):
        return None
    message = data.get("message")
    text = _text_of(
        message.get("content") if isinstance(message, dict) else None
    ).strip()
    if not text or text.startswith("<"):
        return None  # tool results, slash-command echoes, system reminders
    at = data.get("timestamp")
    try:
        when = iso(datetime.fromisoformat(at)) if isinstance(at, str) else None
    except ValueError:
        when = None
    clean = redact_secrets(text, keep_paths=True)
    clean = clean if len(clean) <= MESSAGE_MAX else clean[:MESSAGE_MAX] + "…"
    return {"role": data["type"], "text": clean, "at": when}


def session_transcript(
    session: Session,
    claude_dir: Path | None = None,
    limit: int = DEFAULT_LIMIT,
    known_path: str | None = None,
) -> dict[str, Any]:
    """`{"messages": [...last `limit`...], "can_prompt": bool}` for the widget."""
    base = claude_dir if claude_dir is not None else _default_claude_dir()
    path = find_transcript(session, base, known_path)
    messages: list[dict[str, Any]] = []
    if path is not None:
        try:
            lines = _tail_lines(path)
        except OSError:
            lines = []
        for line in lines:
            try:
                found = _message(json.loads(line))
            except ValueError:
                continue
            if found is not None:
                messages.append(found)
    return {"messages": messages[-limit:], "can_prompt": can_prompt(session)}
