"""Server-sent events for the widget: store events as SSE, with a heartbeat."""

from __future__ import annotations

import json
import queue
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from omniscient.group.server import App

STREAM_TICK_S = 0.5


def sse_event(event: dict[str, Any]) -> str:
    data = json.dumps(event["data"], ensure_ascii=False)  # one line: newlines escaped
    return f"event: {event['type']}\ndata: {data}\n\n"


def pump_events(
    app: App, q: queue.Queue[dict[str, Any]], write: Callable[[str], None]
) -> None:
    """Write store events as SSE, with a heartbeat comment, until stop or drop."""
    last_beat = time.monotonic()
    while not app.stopping.is_set():
        try:
            write(sse_event(q.get(timeout=STREAM_TICK_S)))
            continue
        except queue.Empty:
            pass
        if not app.store.is_subscribed(q):
            return  # dropped as a stalled reader
        if time.monotonic() - last_beat >= app.heartbeat_s:
            write(": heartbeat\n\n")
            app.presence.touch()
            last_beat = time.monotonic()
