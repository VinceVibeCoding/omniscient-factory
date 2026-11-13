"""omnigroup state: a thread-safe SQLite store with change notifications.

One connection (check_same_thread=False) guarded by a lock; a Condition on the same lock
wakes threads waiting for a pending decision. Every change is published to subscribers as
{"type": "session" | "step" | "pending" | "resolved", "data": {...}}.

Rules kept here so every producer behaves the same:
- upserting a session keeps its earliest start, and the previous title, cwd, jump and
  last step when the new values are empty; meta is merged.
- a dismissed session stays dismissed until its state changes (a pollster re-sending the
  same state does not bring it back);
- opening a pending puts its session in "waiting"; resolving the last open one puts it
  back in "working"; dismissing a session cancels its open pendings.
"""

from __future__ import annotations

import json
import logging
import queue
import sqlite3
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from omniscient.group.credentials import secure_db_files
from omniscient.group.models import (
    Pending,
    PendingStatus,
    Session,
    Step,
    Usage,
    iso,
    merge_sessions,
    to_json,
    utcnow,
)
from omniscient.group.store_rows import (
    pending_from_row,
    session_from_row,
    step_from_row,
)

log = logging.getLogger(__name__)

SUBSCRIBER_QUEUE_MAX = 1000
DEFAULT_STEP_LIMIT = 200
MAX_STEP_LIMIT = 1000
_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY, source TEXT NOT NULL, native_id TEXT NOT NULL,
    title TEXT NOT NULL, cwd TEXT, state TEXT NOT NULL, started_at TEXT NOT NULL,
    updated_at TEXT NOT NULL, last_step TEXT NOT NULL, dismissed INTEGER NOT NULL,
    jump TEXT, meta TEXT NOT NULL, usage TEXT);
CREATE TABLE IF NOT EXISTS steps (
    seq INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL, at TEXT NOT NULL,
    kind TEXT NOT NULL, summary TEXT NOT NULL, detail TEXT);
CREATE INDEX IF NOT EXISTS steps_by_session ON steps(session_id, seq);
CREATE TABLE IF NOT EXISTS pending (
    id TEXT PRIMARY KEY, session_id TEXT NOT NULL, kind TEXT NOT NULL,
    created_at TEXT NOT NULL, expires_at TEXT, tool TEXT, payload TEXT NOT NULL,
    options TEXT NOT NULL, status TEXT NOT NULL, answer TEXT);
CREATE INDEX IF NOT EXISTS pending_by_session ON pending(session_id, status);
"""
_FINAL = ("answered", "expired", "cancelled")
_LIVE = ("open", "answering")  # a hook still waits on these


class StoreError(Exception):
    """A store operation refers to something missing or in the wrong state."""


class Store:
    def __init__(self, path: Path | str) -> None:
        if str(path) != ":memory:":
            secure_db_files(Path(path))  # folder 0700, database and journals 0600
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self._changed = threading.Condition(self._lock)
        self._subscribers: list[queue.Queue[dict[str, Any]]] = []
        with self._lock:
            self._db.executescript(_SCHEMA)
            self._db.commit()

    def close(self) -> None:
        with self._lock:
            self._db.close()

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            try:
                yield self._db
                self._db.commit()
            except BaseException:
                self._db.rollback()
                raise

    # ---- notifications ---------------------------------------------------------

    def subscribe(self) -> queue.Queue[dict[str, Any]]:
        q: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=SUBSCRIBER_QUEUE_MAX)
        with self._lock:
            self._subscribers.append(q)
        return q

    def unsubscribe(self, q: queue.Queue[dict[str, Any]]) -> None:
        with self._lock:
            if q in self._subscribers:
                self._subscribers.remove(q)

    def is_subscribed(self, q: queue.Queue[dict[str, Any]]) -> bool:
        with self._lock:
            return q in self._subscribers

    def _publish(self, kind: str, data: dict[str, Any]) -> None:
        event = {"type": kind, "data": data}
        with self._lock:
            for q in list(self._subscribers):
                try:
                    q.put_nowait(event)
                except queue.Full:
                    # A stalled reader: drop it; its stream notices and closes.
                    log.warning("omnigroup: dropping a stalled subscriber")
                    self._subscribers.remove(q)
            self._changed.notify_all()

    # ---- sessions ----------------------------------------------------------------

    def upsert_session(self, session: Session) -> Session:
        """Insert or merge `session` (see the module rules); returns the stored one."""
        with self._tx():
            old = self._get_session(session.id)
            merged = session if old is None else merge_sessions(old, session)
            if merged == old:
                return merged
            self._write_session(merged)
        # A pollster bumping only updated_at is not news for the widget.
        if old is None or replace(old, updated_at=merged.updated_at) != merged:
            self._publish("session", to_json(merged))
        return merged

    def _put(self, table: str, data: dict[str, Any]) -> None:
        """INSERT OR REPLACE a model's JSON form (dicts and lists stored as JSON)."""
        cols = ", ".join(data)
        marks = ", ".join("?" * len(data))
        values = [
            json.dumps(v) if isinstance(v, dict | list) else v for v in data.values()
        ]
        self._db.execute(
            f"INSERT OR REPLACE INTO {table} ({cols}) VALUES ({marks})", values
        )

    def _write_session(self, s: Session) -> None:
        self._put("sessions", to_json(s))

    def get_session(self, session_id: str) -> Session | None:
        with self._lock:
            return self._get_session(session_id)

    def _get_session(self, session_id: str) -> Session | None:
        row = self._db.execute(
            "SELECT * FROM sessions WHERE id = ?", (session_id,)
        ).fetchone()
        return session_from_row(row) if row else None

    def list_sessions(self, include_dismissed: bool = False) -> list[Session]:
        """Sessions, most recently updated first."""
        where = "" if include_dismissed else "WHERE dismissed = 0"
        with self._lock:
            rows = self._db.execute(
                f"SELECT * FROM sessions {where} ORDER BY updated_at DESC"
            ).fetchall()
        return [session_from_row(r) for r in rows]

    def dismiss(self, session_id: str) -> Session:
        """Cross a session off (and cancel its open pendings)."""
        with self._tx():
            old = self._require_session(session_id)
            cancelled = self._open_pendings(session_id)
            for p in cancelled:
                self._set_status(p.id, "cancelled", None)
            updated = replace(old, dismissed=True)
            self._write_session(updated)
        for p in cancelled:
            self._publish("resolved", to_json(self._reload(p.id)))
        self._publish("session", to_json(updated))
        return updated

    def update_usage(self, session_id: str, usage: Usage) -> Session:
        """Store a session's latest usage; publishes {"session_id", "usage"}."""
        with self._tx():
            updated = replace(self._require_session(session_id), usage=usage)
            self._write_session(updated)
        self._publish("usage", {"session_id": session_id, "usage": to_json(usage)})
        return updated

    def _require_session(self, session_id: str) -> Session:
        found = self._get_session(session_id)
        if found is None:
            raise StoreError(f"unknown session {session_id!r}")
        return found

    # ---- steps ---------------------------------------------------------------------

    def add_step(self, step: Step) -> Step:
        """Append a step; the session's last_step and updated_at follow it."""
        with self._tx():
            old = self._require_session(step.session_id)
            self._put("steps", to_json(step))
            updated = replace(
                old,
                last_step=step.summary or old.last_step,
                updated_at=max(old.updated_at, step.at),
            )
            changed = updated != old
            if changed:
                self._write_session(updated)
        self._publish("step", to_json(step))
        if changed:
            self._publish("session", to_json(updated))
        return step

    def steps(self, session_id: str, limit: int = DEFAULT_STEP_LIMIT) -> list[Step]:
        """The latest `limit` steps of a session, oldest first."""
        limit = max(1, min(int(limit), MAX_STEP_LIMIT))
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM steps WHERE session_id = ? ORDER BY seq DESC LIMIT ?",
                (session_id, limit),
            ).fetchall()
        return [step_from_row(r) for r in reversed(rows)]

    # ---- pendings ------------------------------------------------------------------

    def open_pending(self, pending: Pending) -> Pending:
        """Store an open pending; its session goes to "waiting"."""
        if not pending.is_open:
            raise StoreError("only an open pending can be opened")
        with self._tx():
            session = self._require_session(pending.session_id)
            if self._get_pending(pending.id) is not None:
                raise StoreError(f"pending {pending.id} already exists")
            self._put("pending", to_json(pending))
            waiting = replace(session, state="waiting", updated_at=utcnow())
            self._write_session(waiting)
        self._publish("pending", to_json(pending))
        self._publish("session", to_json(waiting))
        return pending

    def get_pending(self, pending_id: str) -> Pending | None:
        with self._lock:
            return self._get_pending(pending_id)

    def _get_pending(self, pending_id: str) -> Pending | None:
        row = self._db.execute(
            "SELECT * FROM pending WHERE id = ?", (pending_id,)
        ).fetchone()
        return pending_from_row(row) if row else None

    def _reload(self, pending_id: str) -> Pending:
        found = self.get_pending(pending_id)
        if found is None:
            raise StoreError(f"unknown pending {pending_id!r}")
        return found

    def pending_for(self, session_id: str) -> list[Pending]:
        """The session's open pendings, oldest first."""
        with self._lock:
            return self._open_pendings(session_id)

    def _open_pendings(self, session_id: str) -> list[Pending]:
        rows = self._db.execute(
            "SELECT * FROM pending WHERE session_id = ? AND status = 'open' "
            "ORDER BY created_at",
            (session_id,),
        ).fetchall()
        return [pending_from_row(r) for r in rows]

    def resolve_pending(
        self,
        pending_id: str,
        answer: dict[str, Any] | None,
        status: PendingStatus = "answered",
    ) -> Pending:
        """Close an open pending (answered / expired / cancelled) and wake waiters."""
        if status not in _FINAL:
            raise StoreError(f"cannot resolve to status {status!r}")
        with self._tx():
            current = self._get_pending(pending_id)
            if current is None:
                raise StoreError(f"unknown pending {pending_id!r}")
            if current.status not in (_LIVE if status == "answered" else ("open",)):
                raise StoreError(f"pending {pending_id} is already {current.status}")
            self._set_status(pending_id, status, answer)
            session = self._after_resolve(current.session_id)
        resolved = self._reload(pending_id)
        self._publish("resolved", to_json(resolved))
        if session is not None:
            self._publish("session", to_json(session))
        return resolved

    def claim_pending(self, pending_id: str) -> Pending:
        """open -> answering, atomically: exactly one answer may proceed (H2)."""
        with self._tx():
            current = self._get_pending(pending_id)
            if current is None:
                raise StoreError(f"unknown pending {pending_id!r}")
            if not current.is_open:
                raise StoreError(f"pending {pending_id} is already {current.status}")
            self._db.execute(
                "UPDATE pending SET status = 'answering' WHERE id = ?", (pending_id,)
            )
        return self._reload(pending_id)

    def release_claim(self, pending_id: str) -> Pending:
        """answering -> open again (the collector could not deliver the answer)."""
        with self._tx():
            self._db.execute(
                "UPDATE pending SET status = 'open' WHERE id = ? AND status = "
                "'answering'",
                (pending_id,),
            )
        return self._reload(pending_id)

    def _set_status(
        self, pending_id: str, status: str, answer: dict[str, Any] | None
    ) -> None:
        self._db.execute(
            "UPDATE pending SET status = ?, answer = ? WHERE id = ?",
            (status, json.dumps(answer) if answer is not None else None, pending_id),
        )

    def _after_resolve(self, session_id: str) -> Session | None:
        """Back to "working" once nothing waits on the owner; None when unchanged."""
        session = self._get_session(session_id)
        if session is None or session.state != "waiting":
            return None
        if self._open_pendings(session_id):
            return None
        working = replace(session, state="working", updated_at=utcnow())
        self._write_session(working)
        return working

    def wait_for_answer(self, pending_id: str, timeout_s: float) -> Pending:
        """Block until the pending is closed or `timeout_s` elapses (then: expired)."""
        deadline = time.monotonic() + max(0.0, timeout_s)
        with self._changed:
            while True:
                current = self._reload(pending_id)
                if current.status not in _LIVE:
                    return current
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._changed.wait(remaining)
        try:
            return self.resolve_pending(pending_id, None, "expired")
        except StoreError:
            return self._reload(pending_id)  # answered in the meantime

    def expire_overdue(self, now: datetime | None = None) -> list[Pending]:
        """Expire open pendings whose expires_at has passed (their hook gave up)."""
        stamp = iso(now or utcnow())
        sql = "SELECT id FROM pending WHERE status = 'open' AND expires_at <= ?"
        with self._lock:
            ids = [r["id"] for r in self._db.execute(sql, (stamp,)).fetchall()]
        expired = []
        for pid in ids:
            try:
                expired.append(self.resolve_pending(pid, None, "expired"))
            except StoreError:
                continue  # resolved concurrently: nothing left to expire
        return expired

    # ---- housekeeping --------------------------------------------------------------

    def prune(self, older_than_days: int = 7) -> int:
        """Delete sessions (with steps and pendings) idle for more than N days."""
        cutoff = iso(utcnow() - timedelta(days=older_than_days))
        stale = "SELECT id FROM sessions WHERE updated_at < ?"
        with self._tx() as db:
            db.execute(f"DELETE FROM steps WHERE session_id IN ({stale})", (cutoff,))
            db.execute(f"DELETE FROM pending WHERE session_id IN ({stale})", (cutoff,))
            sql = "DELETE FROM sessions WHERE updated_at < ?"
            return db.execute(sql, (cutoff,)).rowcount
