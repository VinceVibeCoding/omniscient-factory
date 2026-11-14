"""The OSD feed: plan limits, live tokens/min, subagents and the per-turn cost ticker.

Plan limits come only from Claude Code's documented statusLine JSON
(`rate_limits.five_hour|seven_day`: `used_percentage`, `resets_at` in epoch seconds),
forwarded by `omniscient group statusline`. They are kept in memory and in
`<home>/.omniscient/osd_limits.json` (0600) so a daemon restart keeps them. No OAuth
token is ever read. Live figures come from the transcripts (transcripts_live).

`OsdWorker.run(app)` publishes an SSE `osd` event every TICK_S when the snapshot changed
(ignoring `updated_at`), and a `config` event when config.json or settings.json changed.
Inspired by bozdemir/claude-usage-widget (MIT).
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from omniscient.group import config_store
from omniscient.group.config_store import ConfigPaths
from omniscient.group.credentials import secure_dir, write_atomic
from omniscient.group.transcripts_live import (
    TranscriptTail,
    claude_dir,
    count_active_subagents,
    live_rate,
)

log = logging.getLogger(__name__)

TICK_S = 2.0
TICKER_WINDOW_S = 15 * 60
TICKER_MAX = 40
LIMITS_FILE = "osd_limits.json"
WINDOWS = (("five_hour", "Session"), ("seven_day", "Weekly"))


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).isoformat().replace("+00:00", "Z")


def parse_limits(event: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """{window_id: {"used_pct", "resets_at"}} from a statusLine JSON (documented fields)."""
    raw = event.get("rate_limits")
    if not isinstance(raw, dict):
        return {}
    found: dict[str, dict[str, Any]] = {}
    for window_id, _label in WINDOWS:
        window = raw.get(window_id)
        if not isinstance(window, dict):
            continue
        pct = window.get("used_percentage")
        if (
            isinstance(pct, bool)
            or not isinstance(pct, int | float)
            or not 0 <= pct <= 1000
        ):
            continue
        resets = window.get("resets_at")
        ok = (
            isinstance(resets, int | float)
            and not isinstance(resets, bool)
            and resets > 0
        )
        found[window_id] = {
            "used_pct": round(float(pct), 1),
            "resets_at": iso(resets) if ok else None,
        }
    return found


@dataclass
class OsdState:
    """Mutable daemon state behind the OSD (one per daemon; methods are thread-safe)."""

    state_dir: Path
    claude_root: Path = field(default_factory=claude_dir)
    paths: ConfigPaths = field(default_factory=ConfigPaths.default)
    lock: threading.Lock = field(default_factory=threading.Lock)
    limits: dict[str, dict[str, Any]] = field(default_factory=dict)
    tail: TranscriptTail | None = None

    def __post_init__(self) -> None:
        self.tail = TranscriptTail(self.claude_root)
        self.limits = self._load_limits()

    # ---- limits -----------------------------------------------------------------------

    def record_limits(self, event: Mapping[str, Any]) -> bool:
        """Fold a statusLine JSON; True when the limits changed (then persisted)."""
        found = parse_limits(event)
        if not found:
            return False
        with self.lock:
            merged = {**self.limits, **found}
            if merged == self.limits:
                return False
            self.limits = merged
            self._save_limits(merged)
        return True

    def _load_limits(self) -> dict[str, dict[str, Any]]:
        path = self.state_dir / LIMITS_FILE
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as exc:
            log.warning("omnigroup: %s ignored: %s", LIMITS_FILE, exc)
            return {}
        if not isinstance(data, dict):
            return {}
        return parse_limits(
            {"rate_limits": {k: _as_status(v) for k, v in data.items()}}
        )

    def _save_limits(self, limits: dict[str, dict[str, Any]]) -> None:
        try:
            secure_dir(self.state_dir)
            write_atomic(self.state_dir / LIMITS_FILE, json.dumps(limits) + "\n")
        except OSError as exc:
            log.warning("omnigroup: limits not saved: %s", exc)

    # ---- snapshot ---------------------------------------------------------------------

    def snapshot(self, now: float | None = None) -> dict[str, Any]:
        """The OSD payload (contract §2). Scans the transcripts incrementally."""
        now = time.time() if now is None else now
        midnight = _local_midnight(now)
        with self.lock:
            assert self.tail is not None
            self.tail.scan(now, since=min(midnight, now - TICKER_WINDOW_S))
            today = self.tail.turns(since=midnight)
            recent = self.tail.turns(since=now - TICKER_WINDOW_S)
            limits = dict(self.limits)
        live = live_rate(recent, now)
        costs = [t.cost_usd for t in today if t.cost_usd is not None]
        return {
            "limits": [
                {"id": wid, "label": label, **limits[wid]}
                for wid, label in WINDOWS
                if wid in limits
            ],
            "live": {
                "is_live": live.is_live,
                "tokens_per_minute": live.tokens_per_minute,
            },
            "subagents": count_active_subagents(self.tail.projects, now),
            "ticker": [_ticker_item(t) for t in recent[-TICKER_MAX:]],
            "today_cost_usd": round(sum(costs), 6) if costs else 0.0,
            "updated_at": iso(now),
        }

    def config_view(self) -> dict[str, Any]:
        return config_store.effective(self.paths)


def _as_status(saved: Any) -> Any:
    """Saved {"used_pct", "resets_at" ISO} back to the statusLine shape parse_limits reads."""
    if not isinstance(saved, dict):
        return None
    resets = saved.get("resets_at")
    epoch = None
    if isinstance(resets, str):
        try:
            epoch = datetime.fromisoformat(resets).timestamp()
        except ValueError:
            epoch = None
    return {"used_percentage": saved.get("used_pct"), "resets_at": epoch}


def _local_midnight(now: float) -> float:
    local = datetime.fromtimestamp(now).astimezone()
    return local.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


def _ticker_item(turn: Any) -> dict[str, Any]:
    return {
        "at": iso(turn.at),
        "msg_id": turn.msg_id,
        "cost_usd": turn.cost_usd,
        "tool": turn.tool,
        "output_tokens": turn.output_tokens,
        "model": turn.model,
    }


def _comparable(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in snapshot.items() if k != "updated_at"}


@dataclass
class OsdWorker:
    """Publishes `osd` (on change) and `config` (when its files change) every TICK_S."""

    state: OsdState
    publish: Callable[[str, dict[str, Any]], None]
    tick_s: float = TICK_S
    _last_osd: dict[str, Any] | None = None
    _last_files: tuple[float, float] | None = None

    def tick(self, now: float | None = None) -> None:
        snapshot = self.state.snapshot(now)
        if _comparable(snapshot) != self._last_osd:
            self._last_osd = _comparable(snapshot)
            self.publish("osd", snapshot)
        files = config_store.fingerprint(self.state.paths)
        if self._last_files is not None and files != self._last_files:
            self.publish("config", self.state.config_view())
        self._last_files = files

    def run(self, stopping: threading.Event) -> None:
        while not stopping.is_set():
            try:
                self.tick()
            except Exception:  # a bad transcript must never kill the feed
                log.exception("omnigroup: osd tick failed")
            stopping.wait(self.tick_s)
