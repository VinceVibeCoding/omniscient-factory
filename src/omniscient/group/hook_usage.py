"""Token usage and context for Claude Code sessions (read by the daemon, never the hook).

Hooks carry no usage, only `transcript_path`. The daemon calls
`translate_usage(source, event, cursor)` on Stop / PostToolUse / SubagentStop /
Notification / StatusLine events: it reads the transcript JSONL from the session's
cursor (byte offset, so each byte is read once), sums the main conversation's usage and
keeps the last call's context. The format is internal to Claude Code: unknown lines are
skipped and any error leaves the previous figures in place.

The optional `omniscient group statusline` forwards Claude Code's documented statusLine
JSON (`context_window`, `cost.total_cost_usd`, `rate_limits.five_hour|seven_day`), which
fills the context window size, the cost and the plan usage exactly.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from omniscient.group.hooks import event_name
from omniscient.group.models import Usage, utcnow

USAGE_EVENTS = frozenset(
    {"Stop", "PostToolUse", "SubagentStop", "Notification", "StatusLine", "Status"}
)
STATUS_EVENTS = frozenset({"StatusLine", "Status"})  # usage only: no session state
TAIL_BYTES = 256 * 1024  # first read of a transcript the daemon has not seen yet
MAX_READ_BYTES = 8 * 1024 * 1024  # per call, so a huge backlog never stalls the daemon
DEFAULT_CONTEXT = 200_000
LARGE_CONTEXT = 1_000_000  # "[1m]" model ids, or a context already above 200k
# à vérifier: the context window per model; the statusLine value always wins.
CONTEXT_BY_MODEL: dict[str, int] = {}


@dataclass(frozen=True)
class UsageCursor:
    """What the daemon keeps per session between two reads of its transcript."""

    path: str = ""
    offset: int = 0
    model: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    context_tokens: int = 0
    last_message_id: str | None = None
    context_limit: int | None = None  # from the statusLine, when known
    cost_usd: float | None = None
    plan_used_pct: float | None = None
    plan_resets_at: datetime | None = None


def is_status_event(event: Mapping[str, Any]) -> bool:
    """A statusLine refresh: update usage only, never the session state or steps."""
    try:
        return event_name(event) in STATUS_EVENTS
    except ValueError:
        return False


def context_limit(model: str | None, context_tokens: int) -> int:
    name = (model or "").lower()
    if "[1m]" in name or context_tokens > DEFAULT_CONTEXT:
        return LARGE_CONTEXT
    return CONTEXT_BY_MODEL.get(name, DEFAULT_CONTEXT)


def _int(value: Any) -> int:
    return value if isinstance(value, int) and value >= 0 else 0


def _fold(cursor: UsageCursor, entry: Any) -> UsageCursor:
    """The cursor after one transcript line (unchanged if it is not a main-loop reply)."""
    if not isinstance(entry, dict) or entry.get("type") != "assistant":
        return cursor
    msg = entry.get("message")
    if entry.get("isSidechain") or not isinstance(msg, dict):
        return cursor
    usage = msg.get("usage")
    if not isinstance(usage, dict):
        return cursor
    mid = msg.get("id") if isinstance(msg.get("id"), str) else None
    if mid is not None and mid == cursor.last_message_id:
        return cursor  # one API reply is written once per content block
    inp, out = _int(usage.get("input_tokens")), _int(usage.get("output_tokens"))
    read = _int(usage.get("cache_read_input_tokens"))
    write = _int(usage.get("cache_creation_input_tokens"))
    model = msg.get("model") if isinstance(msg.get("model"), str) else cursor.model
    return replace(
        cursor,
        model=model,
        input_tokens=cursor.input_tokens + inp,
        output_tokens=cursor.output_tokens + out,
        cache_read_tokens=cursor.cache_read_tokens + read,
        cache_write_tokens=cursor.cache_write_tokens + write,
        context_tokens=inp + read + write,
        last_message_id=mid,
    )


def read_transcript(path: Path, cursor: UsageCursor) -> UsageCursor:
    """Fold the lines appended since the cursor; OSError propagates to the caller."""
    if cursor.path != str(path):  # another transcript: fresh counts, same plan figures
        cursor = UsageCursor(
            path=str(path),
            context_limit=cursor.context_limit,
            cost_usd=cursor.cost_usd,
            plan_used_pct=cursor.plan_used_pct,
            plan_resets_at=cursor.plan_resets_at,
        )
    size = path.stat().st_size
    fresh = cursor.offset == 0 or cursor.offset > size  # new, or rewritten
    start = max(0, size - TAIL_BYTES) if fresh else cursor.offset
    with path.open("rb") as fh:
        fh.seek(start)
        chunk = fh.read(MAX_READ_BYTES)
    if fresh and start > 0:  # we jumped into the middle of a line: drop it
        cut = chunk.find(b"\n") + 1
        chunk, start = chunk[cut:], start + cut
    end = chunk.rfind(b"\n") + 1  # a half-written last line waits for next time
    for raw in chunk[:end].splitlines():
        if b'"usage"' not in raw:
            continue
        try:
            cursor = _fold(cursor, json.loads(raw))
        except ValueError:
            continue  # a line in a format we do not know: skip it
    return replace(cursor, offset=start + end)


def _epoch(value: Any) -> datetime | None:
    if isinstance(value, int | float) and value > 0:
        return datetime.fromtimestamp(value, UTC)
    return None


def _number(value: Any, default: float | None) -> float | None:
    ok = isinstance(value, int | float) and not isinstance(value, bool) and value >= 0
    return float(value) if ok else default


def fold_status(cursor: UsageCursor, event: Mapping[str, Any]) -> UsageCursor:
    """Fold a statusLine JSON (documented fields only; missing ones keep their value)."""
    ctx = (
        event.get("context_window")
        if isinstance(event.get("context_window"), dict)
        else {}
    )
    cost = event.get("cost") if isinstance(event.get("cost"), dict) else {}
    limits = (
        event.get("rate_limits") if isinstance(event.get("rate_limits"), dict) else {}
    )
    window = limits.get("five_hour") or limits.get("seven_day")
    window = window if isinstance(window, dict) else {}
    model = event.get("model") if isinstance(event.get("model"), dict) else {}
    size = ctx.get("context_window_size")
    used = ctx.get("total_input_tokens")
    return replace(
        cursor,
        model=model.get("id") if isinstance(model.get("id"), str) else cursor.model,
        context_limit=size
        if isinstance(size, int) and size > 0
        else cursor.context_limit,
        context_tokens=used
        if isinstance(used, int) and used >= 0
        else cursor.context_tokens,
        cost_usd=_number(cost.get("total_cost_usd"), cursor.cost_usd),
        plan_used_pct=_number(window.get("used_percentage"), cursor.plan_used_pct),
        plan_resets_at=_epoch(window.get("resets_at")) or cursor.plan_resets_at,
    )


def to_usage(cursor: UsageCursor, now: datetime | None = None) -> Usage:
    limit = cursor.context_limit or context_limit(cursor.model, cursor.context_tokens)
    return Usage(
        model=cursor.model,
        input_tokens=cursor.input_tokens,
        output_tokens=cursor.output_tokens,
        cache_read_tokens=cursor.cache_read_tokens,
        cache_write_tokens=cursor.cache_write_tokens,
        context_tokens=cursor.context_tokens,
        context_limit=limit,
        cost_usd=cursor.cost_usd,
        plan_used_pct=cursor.plan_used_pct,
        plan_resets_at=cursor.plan_resets_at,
        updated_at=now or utcnow(),
    )


def translate_usage(
    source: str, event: Mapping[str, Any], cursor: UsageCursor | None = None
) -> tuple[Usage | None, UsageCursor]:
    """(usage to store or None, cursor to keep for the session). Never raises.

    Copilot hook payloads carry no usage: always (None, cursor).
    """
    cursor = cursor or UsageCursor()
    if source != "claude":
        return None, cursor
    try:
        name = event_name(event)
        if name not in USAGE_EVENTS:
            return None, cursor
        if name in STATUS_EVENTS:
            cursor = fold_status(cursor, event)
        path = event.get("transcript_path")
        if isinstance(path, str) and path.endswith(".jsonl") and Path(path).is_file():
            cursor = read_transcript(Path(path), cursor)
    except (OSError, ValueError):
        return None, cursor  # usage is best effort: the previous figures stay
    if cursor.last_message_id is None and cursor.context_limit is None:
        return None, cursor  # nothing known yet
    return to_usage(cursor), cursor
