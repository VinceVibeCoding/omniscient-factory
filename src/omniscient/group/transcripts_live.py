"""Live activity read from Claude Code transcripts: turns, tokens per minute, subagents.

Claude Code appends every assistant turn (with its `message.usage`) to
`<claude_dir>/projects/<project>/<session>.jsonl`; Task subagents write their own
`<project>/<session>/subagents/**/agent-*.jsonl`. `TranscriptTail.scan` reads only the
bytes appended since the previous scan (byte offsets per file, complete lines only), so
polling every few seconds stays cheap. One API message can span several JSONL lines
(one per content block) sharing `message.id`: turns are merged by that id. The format
is internal to Claude Code: malformed lines are skipped. Inspired by
bozdemir/claude-usage-widget (MIT): live_stream.py, subagents.py, ticker.py.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Any

from omniscient.group.pricing import turn_cost

log = logging.getLogger(__name__)

FIRST_READ_BYTES = 8 * 1024 * 1024  # first visit of a file: at most its last 8 MB
MAX_READ_BYTES = 8 * 1024 * 1024  # per file and scan
LIVE_WINDOW_S = 5 * 60
ACTIVE_CUTOFF_S = 90
SUBAGENT_ACTIVE_S = 60
TURNS_KEPT_S = 26 * 3600  # enough for "today" in any time zone


@dataclass(frozen=True)
class Turn:
    """One assistant API message."""

    at: float  # unix seconds
    msg_id: str
    model: str
    output_tokens: int
    cost_usd: float | None
    tool: str  # first tool_use name, "" for a text-only turn


def claude_dir(environ: Mapping[str, str] | None = None) -> Path:
    env = os.environ if environ is None else environ
    custom = env.get("CLAUDE_CONFIG_DIR", "").strip()
    return Path(custom).expanduser() if custom else Path.home() / ".claude"


def _timestamp(raw: Any) -> float | None:
    if not isinstance(raw, str) or not raw:
        return None
    try:
        return datetime.fromisoformat(raw).timestamp()
    except ValueError:
        return None


def _first_tool(content: Any) -> str:
    if not isinstance(content, list):
        return ""
    for block in content:
        if isinstance(block, dict) and block.get("type") == "tool_use":
            name = block.get("name")
            return name[:60] if isinstance(name, str) else ""
    return ""


def parse_turn(entry: Any) -> Turn | None:
    """A Turn from one JSONL entry, or None when it is not an assistant turn with usage."""
    if not isinstance(entry, dict):
        return None
    msg = entry.get("message")
    if not isinstance(msg, dict) or msg.get("role") != "assistant":
        return None
    usage, msg_id = msg.get("usage"), msg.get("id")
    at = _timestamp(entry.get("timestamp"))
    if (
        not isinstance(usage, dict)
        or not isinstance(msg_id, str)
        or not msg_id
        or at is None
    ):
        return None
    out = usage.get("output_tokens")
    out = (
        int(out)
        if isinstance(out, int) and not isinstance(out, bool) and out > 0
        else 0
    )
    model = msg.get("model") if isinstance(msg.get("model"), str) else ""
    return Turn(
        at,
        msg_id[:120],
        model,
        out,
        turn_cost(model, usage),
        _first_tool(msg.get("content")),
    )


def merge(old: Turn, new: Turn) -> Turn:
    """Two lines of the same message: keep the largest usage and the first tool seen."""
    best = new if new.output_tokens >= old.output_tokens else old
    return replace(best, at=min(old.at, new.at), tool=old.tool or new.tool)


def transcript_files(projects: Path) -> Iterator[Path]:
    yield from projects.glob("*/*.jsonl")
    yield from projects.glob("*/*/subagents/**/agent-*.jsonl")


def count_active_subagents(projects: Path, now: float) -> int:
    count = 0
    for path in projects.glob("*/*/subagents/**/agent-*.jsonl"):
        try:
            if path.stat().st_mtime >= now - SUBAGENT_ACTIVE_S:
                count += 1
        except OSError:
            continue
    return count


class TranscriptTail:
    """Turns of the recent transcripts, merged by message id (not thread-safe: one owner)."""

    def __init__(self, root: Path) -> None:
        self.projects = root / "projects"
        self._offsets: dict[Path, int] = {}
        self._turns: dict[str, Turn] = {}

    def scan(self, now: float, since: float) -> None:
        """Read what was appended to the transcripts modified after `since`."""
        if not self.projects.is_dir():
            return
        for path in transcript_files(self.projects):
            try:
                stat = path.stat()
            except OSError:
                continue
            if stat.st_mtime < since:
                continue
            self._read(path, stat.st_size)
        self._prune(now - TURNS_KEPT_S)

    def turns(self, since: float = 0.0) -> list[Turn]:
        """Known turns at or after `since`, oldest first."""
        return sorted(
            (t for t in self._turns.values() if t.at >= since), key=lambda t: t.at
        )

    def _read(self, path: Path, size: int) -> None:
        start = self._offsets.get(path)
        if start is None or start > size:  # new file, or truncated/replaced
            start = max(0, size - FIRST_READ_BYTES)
            skip_partial = start > 0
        else:
            skip_partial = False
        if size <= start:
            return
        try:
            with path.open("rb") as fh:
                fh.seek(start)
                data = fh.read(min(size - start, MAX_READ_BYTES))
        except OSError as exc:
            log.warning("omnigroup: transcript unreadable (%s): %s", path.name, exc)
            return
        end = data.rfind(b"\n")
        if end < 0:
            return  # no complete line yet
        lines = data[: end + 1].split(b"\n")
        if skip_partial:
            lines = lines[1:]
        self._offsets[path] = start + end + 1
        for line in lines:
            self._ingest(line)

    def _ingest(self, line: bytes) -> None:
        if not line.strip():
            return
        try:
            turn = parse_turn(json.loads(line))
        except (ValueError, UnicodeDecodeError):
            return
        if turn is None:
            return
        old = self._turns.get(turn.msg_id)
        self._turns[turn.msg_id] = turn if old is None else merge(old, turn)

    def _prune(self, before: float) -> None:
        for key in [k for k, t in self._turns.items() if t.at < before]:
            del self._turns[key]


@dataclass(frozen=True)
class Live:
    is_live: bool
    tokens_per_minute: float


def live_rate(turns: list[Turn], now: float) -> Live:
    """Output tokens per minute over the last LIVE_WINDOW_S; live if a turn just landed."""
    recent = [t for t in turns if now - LIVE_WINDOW_S <= t.at <= now + 60]
    if not recent:
        return Live(False, 0.0)
    latest = max(t.at for t in recent)
    if now - latest > ACTIVE_CUTOFF_S:
        return Live(False, 0.0)
    span_min = max(1.0, (now - min(t.at for t in recent)) / 60)
    tokens = sum(t.output_tokens for t in recent)
    return Live(True, round(tokens / span_min, 1))
