"""The builder's side of the UI pane: read an AgentRun run, launch and tail a build.

Reads use GET /runs/{id} and GET /runs/{id}/timeline only (never start or change a run).
`launch_build` is the one thing here that spends credit; the pane calls it only after the
owner confirms.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

FINISHED = frozenset({"completed", "failed", "cancelled"})
SKIPPED_KINDS = frozenset({"usage", "user_message"})
KEEP_EVENTS = 8
EVENT_WIDTH = 110
TIMELINE_PAGE = 200
MAX_PAGES = 20  # per poll: a long build catches up over a few polls
BASE_URL = os.environ.get("AGENTRUN_BASE_URL", "http://127.0.0.1:7340/v2")
TOKEN_FILE = Path.home() / ".agentrun" / ".token"
BUILD_LOGS = Path(".omniscient") / "builds"


class RunSource(Protocol):
    """Read-only view of AgentRun runs (GET /runs/{id}, GET /runs/{id}/timeline)."""

    def get(self, run_id: str) -> Mapping[str, Any]: ...
    def timeline(self, run_id: str, after: int | None) -> Mapping[str, Any]: ...


class AgentRunSource:
    """RunSource over the parcha-agentrun SDK (GETs only: never starts or changes runs)."""

    def __init__(self, client: Any) -> None:
        self._runs = client.runs

    def get(self, run_id: str) -> Mapping[str, Any]:
        return self._runs.get(run_id)

    def timeline(self, run_id: str, after: int | None) -> Mapping[str, Any]:
        return self._runs.timeline(run_id, after=after, limit=TIMELINE_PAGE)


def default_source() -> RunSource | None:
    """The local AgentRun server, or None without a token / SDK (status-only mode)."""
    key = os.environ.get("AGENTRUN_API_KEY")
    if not key and TOKEN_FILE.is_file():
        key = TOKEN_FILE.read_text(encoding="utf-8").strip()
    if not key:
        return None
    try:
        from agentrun import AgentRun
    except ImportError:
        return None
    return AgentRunSource(AgentRun(base_url=BASE_URL, api_key=key, max_retries=0))


@dataclass(frozen=True)
class RunStatus:
    status: str
    created_at: datetime | None
    ended_at: datetime | None
    cost_usd: float
    cap_usd: float | None

    @property
    def finished(self) -> bool:
        return self.status in FINISHED


def _when(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _usd(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def parse_run(run: Mapping[str, Any]) -> RunStatus:
    cost = run.get("cost") or {}
    return RunStatus(
        status=str(run.get("status") or "unknown"),
        created_at=_when(run.get("created_at")),
        ended_at=_when(run.get("ended_at")),
        cost_usd=_usd(cost.get("usd")) or 0.0,
        cap_usd=_usd(cost.get("cap_usd")),
    )


def format_event(row: Mapping[str, Any]) -> str | None:
    """One timeline row as « HH:MM:SS  kind  summary », or None for noise rows."""
    kind = str(row.get("kind") or "")
    if kind in SKIPPED_KINDS:
        return None
    when = _when(row.get("timestamp"))
    stamp = when.astimezone().strftime("%H:%M:%S") if when else "--:--:--"
    summary = " ".join(str(row.get("summary") or "").split())
    return f"{stamp}  {kind:<11} {summary}"[:EVENT_WIDTH]


def read_timeline(
    source: RunSource, run_id: str, cursor: int | None
) -> tuple[tuple[str, ...], bool, int | None]:
    """(new event lines, reset?, next cursor), paging forward from `cursor`."""
    lines: list[str] = []
    reset = False
    for _ in range(MAX_PAGES):
        page = source.timeline(run_id, cursor)
        if page.get("reset"):
            reset, lines = True, []
        for row in page.get("rows") or []:
            text = format_event(row)
            if text is not None:
                lines.append(text)
        nxt = page.get("next_after")
        cursor = int(nxt) if isinstance(nxt, int) else cursor
        if not page.get("has_more"):
            break
    return tuple(lines[-KEEP_EVENTS * 4 :]), reset, cursor


def source_errors() -> tuple[type[Exception], ...]:
    base: tuple[type[Exception], ...] = (OSError, ValueError, KeyError, TypeError)
    try:
        from agentrun import AgentRunError
    except ImportError:
        return base
    return (*base, AgentRunError)


def build_log_path(repo: Path, slug: str) -> Path:
    return repo / BUILD_LOGS / f"{slug}.log"


def launch_build(repo: Path, stem: str, log_path: Path) -> subprocess.Popen[bytes]:
    """Start `uv run omniscient build STEM` detached, its output going to log_path."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("wb") as out:
        return subprocess.Popen(
            ["uv", "run", "omniscient", "build", stem],  # argv: no shell, no quoting
            cwd=repo,
            stdin=subprocess.DEVNULL,
            stdout=out,
            stderr=subprocess.STDOUT,
            start_new_session=True,  # survives the pane quitting
        )


def tail_lines(path: Path, offset: int) -> tuple[tuple[str, ...], int]:
    """Complete lines appended to path since offset, and the new offset."""
    if not path.is_file():
        return (), offset
    with path.open("rb") as handle:
        handle.seek(offset)
        data = handle.read()
    end = data.rfind(b"\n") + 1
    text = data[:end].decode("utf-8", errors="replace")
    return tuple(ln for ln in text.splitlines() if ln.strip()), offset + end
