"""In-memory gates of the omnigroup daemon: wait tickets and widget presence.

- `TicketBook`: POST /hook returns a random ticket bound to the pending it opened; only
  that ticket (once) lets the hook credential wait on that pending. Only its hash is
  kept, tickets are forgotten after `TICKET_TTL_S`.
- `Presence`: counts the open /stream connections (at most `MAX_STREAMS`) and
  remembers when a widget was last connected. With no widget for `PRESENCE_S`, /hook
  opens no pending, so an agent never waits for an absent widget.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field

TICKET_TTL_S = 300.0
MAX_TICKETS = 4096
MAX_STREAMS = 8
PRESENCE_S = 30.0

Clock = Callable[[], float]


def _digest(ticket: str) -> bytes:
    return hashlib.sha256(ticket.encode("utf-8")).digest()


@dataclass
class TicketBook:
    clock: Clock = time.monotonic
    _tickets: dict[str, tuple[bytes, float]] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def issue(self, pending_id: str) -> str:
        """A fresh single-use ticket for `pending_id`."""
        ticket = secrets.token_urlsafe(24)
        with self._lock:
            self._prune()
            self._tickets[pending_id] = (_digest(ticket), self.clock())
        return ticket

    def redeem(self, pending_id: str, ticket: str) -> bool:
        """True (once) when `ticket` is the one issued for `pending_id`."""
        if not ticket:
            return False
        with self._lock:
            self._prune()
            found = self._tickets.get(pending_id)
            if found is None or not hmac.compare_digest(found[0], _digest(ticket)):
                return False
            del self._tickets[pending_id]
        return True

    def _prune(self) -> None:
        now = self.clock()
        stale = [k for k, (_, t) in self._tickets.items() if now - t > TICKET_TTL_S]
        for key in stale:
            del self._tickets[key]
        while len(self._tickets) >= MAX_TICKETS:  # oldest first (dicts keep order)
            del self._tickets[next(iter(self._tickets))]


@dataclass
class Presence:
    clock: Clock = time.monotonic
    max_streams: int = MAX_STREAMS
    window_s: float = PRESENCE_S
    _open: int = 0
    _last_seen: float | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def enter(self) -> bool:
        """Count one more stream; False (not counted) when the cap is reached."""
        with self._lock:
            if self._open >= self.max_streams:
                return False
            self._open += 1
            self._last_seen = self.clock()
            return True

    def leave(self) -> None:
        with self._lock:
            self._open = max(0, self._open - 1)
            self._last_seen = self.clock()

    def touch(self) -> None:
        """A heartbeat reached a widget: it is still there."""
        with self._lock:
            self._last_seen = self.clock()

    def widget_present(self) -> bool:
        with self._lock:
            if self._open > 0:
                return True
            last = self._last_seen
            return last is not None and self.clock() - last <= self.window_s
