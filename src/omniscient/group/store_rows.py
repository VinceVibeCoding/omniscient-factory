"""Row <-> model conversion for the omnigroup SQLite store."""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from omniscient.group.models import (
    Pending,
    Session,
    Step,
    pending_from_json,
    session_from_json,
    step_from_json,
)

_JSON_COLUMNS = frozenset({"jump", "meta", "usage", "payload", "options", "answer"})


def _row(r: sqlite3.Row) -> dict[str, Any]:
    return {
        k: json.loads(r[k]) if k in _JSON_COLUMNS and r[k] is not None else r[k]
        for k in r.keys()  # noqa: SIM118 - iterating a Row yields values
    }


def session_from_row(r: sqlite3.Row) -> Session:
    return session_from_json(_row(r))


def step_from_row(r: sqlite3.Row) -> Step:
    return step_from_json(_row(r))


def pending_from_row(r: sqlite3.Row) -> Pending:
    return pending_from_json(_row(r))
