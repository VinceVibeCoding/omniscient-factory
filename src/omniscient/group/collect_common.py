"""What the herdr and AgentRun collectors share: the store protocol and the poll loop."""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Sequence
from dataclasses import replace
from typing import Any, Protocol

from omniscient.group.models import Pending, Session, Step, Usage

log = logging.getLogger(__name__)


class CollectorStore(Protocol):
    """The part of omniscient.group.store.Store the collectors use."""

    def get_session(self, session_id: str) -> Session | None: ...
    def upsert_session(self, session: Session) -> Session: ...
    def add_step(self, step: Step) -> Any: ...
    def open_pending(self, pending: Pending) -> Pending: ...
    def pending_for(self, session_id: str) -> Sequence[Pending]: ...
    def update_usage(self, session_id: str, usage: Usage) -> Any: ...
    def resolve_pending(
        self, pending_id: str, answer: dict[str, Any] | None, status: str
    ) -> Any: ...


MAX_BACKOFF_S = 60.0


def run_loop(
    name: str, poll: Callable[[], None], stop: threading.Event, interval_s: float
) -> None:
    """Call `poll` every `interval_s` until `stop` is set; a failed poll is not fatal.

    A failure is logged once with its traceback; the same error repeating (herdr not
    running, say) stays quiet while the delay doubles up to MAX_BACKOFF_S. A different
    error is logged again, and the first good poll logs the recovery.
    """
    failing: str | None = None
    delay = interval_s
    while not stop.is_set():
        try:
            poll()
        except Exception as exc:
            signature = f"{type(exc).__name__}: {exc}"
            if signature != failing:
                log.exception("omnigroup collector %s: poll failed", name)
                failing = signature
            delay = min(max(delay * 2, interval_s), MAX_BACKOFF_S)
        else:
            if failing is not None:
                log.info("omnigroup collector %s: recovered", name)
                failing = None
            delay = interval_s
        stop.wait(delay)


def save_session(store: CollectorStore, fresh: Session) -> Session:
    """Upsert `fresh`; the store merges it with what it knows (start time, dismissal)."""
    return store.upsert_session(fresh)


def _same_usage(a: Usage | None, b: Usage) -> bool:
    return a is not None and replace(a, updated_at=b.updated_at) == b


def apply_usage(
    store: CollectorStore,
    sid: str,
    usage: Usage | None,
    *,
    ours: Usage | None = None,
    guard: bool = False,
) -> bool:
    """Store `usage` when it says something new; True when written.

    With `guard`, usage someone else wrote (the Claude hooks: richer) is kept: only an empty
    slot or `ours` (what this collector wrote last) is replaced.
    """
    known = store.get_session(sid)
    if usage is None or known is None or _same_usage(known.usage, usage):
        return False
    if guard and known.usage is not None and known.usage != ours:
        return False
    store.update_usage(sid, usage)
    return True


def open_pendings(store: CollectorStore, sid: str) -> list[Pending]:
    return [p for p in store.pending_for(sid) if p.status == "open"]


def cancel_open(store: CollectorStore, sid: str) -> None:
    for pending in open_pendings(store, sid):
        store.resolve_pending(pending.id, None, "cancelled")
