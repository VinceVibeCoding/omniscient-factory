"""omnigroup models and SQLite store (offline, tmp database)."""

from __future__ import annotations

import queue
import threading
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from omniscient.group import store as store_mod
from omniscient.group.models import (
    DETAIL_MAX,
    SUMMARY_MAX,
    JumpTarget,
    ModelError,
    Pending,
    Session,
    Step,
    Usage,
    context_pct,
    pending_from_json,
    session_from_json,
    step_from_json,
    to_json,
    usage_from_json,
    utcnow,
)
from omniscient.group.store import Store, StoreError

T0 = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)


def make_session(native: str = "abc", **over: object) -> Session:
    fields: dict[str, object] = {
        "id": f"claude:{native}",
        "source": "claude",
        "native_id": native,
        "title": "Projet démo",
        "cwd": "/tmp/demo",
        "state": "working",
        "started_at": T0,
        "updated_at": T0,
    }
    fields.update(over)
    return Session(**fields)  # type: ignore[arg-type]


@pytest.fixture
def store(tmp_path: Path) -> Store:
    db = Store(tmp_path / "group.db")
    yield db
    db.close()


# ---- models -----------------------------------------------------------------------


def test_session_id_must_match_source_and_native_id() -> None:
    with pytest.raises(ModelError):
        make_session(id="claude:other")


def test_naive_datetimes_and_unknown_states_are_refused() -> None:
    with pytest.raises(ModelError):
        make_session(started_at=datetime(2026, 1, 1))
    with pytest.raises(ModelError):
        make_session(state="sleeping")
    with pytest.raises(ModelError):
        Step("claude:abc", T0, "dance", "x")


def test_datetimes_are_normalised_to_utc() -> None:
    east = timezone(timedelta(hours=5))
    session = make_session(updated_at=datetime(2026, 1, 2, 8, 4, 5, tzinfo=east))
    assert session.updated_at == T0
    assert session.updated_at.tzinfo == UTC


def test_step_texts_are_redacted_and_clipped() -> None:
    secret = "sk-ant-" + "a" * 30
    step = Step("claude:abc", T0, "tool", f"Bash {secret} " + "x" * 400, "y" * 5000)
    assert secret not in step.summary
    assert "[masqué]" in step.summary
    assert len(step.summary) == SUMMARY_MAX
    assert step.detail is not None and len(step.detail) == DETAIL_MAX


def test_json_round_trips() -> None:
    session = make_session(
        jump=JumpTarget("vscode_session", "abc", "VS Code"), meta={"k": "v"}
    )
    step = Step("claude:abc", T0, "message", "bonjour", None)
    pending = Pending.new(
        "claude:abc", "permission", tool="Bash", options=("allow", "deny")
    )
    assert session_from_json(to_json(session)) == session
    assert step_from_json(to_json(step)) == step
    assert pending_from_json(to_json(pending)) == pending
    assert to_json(session)["started_at"] == "2026-01-02T03:04:05.000000+00:00"


def test_from_json_reports_missing_fields() -> None:
    with pytest.raises(ModelError, match="missing"):
        session_from_json({"id": "claude:x"})
    with pytest.raises(ModelError):
        pending_from_json([])


def test_to_json_refuses_other_objects() -> None:
    with pytest.raises(TypeError):
        to_json("nope")  # type: ignore[arg-type]


# ---- sessions -------------------------------------------------------------------------


def test_upsert_merges_and_keeps_known_values(store: Store) -> None:
    store.upsert_session(make_session(meta={"a": "1"}, last_step="début"))
    later = T0 + timedelta(minutes=5)
    merged = store.upsert_session(
        make_session(
            title="",
            cwd=None,
            started_at=later,
            updated_at=later,
            meta={"b": "2"},
            state="idle",
        )
    )
    assert merged.title == "Projet démo"
    assert merged.cwd == "/tmp/demo"
    assert merged.started_at == T0
    assert merged.updated_at == later
    assert merged.meta == {"a": "1", "b": "2"}
    assert merged.last_step == "début"
    assert store.get_session("claude:abc") == merged


def test_list_is_newest_first_and_hides_dismissed(store: Store) -> None:
    store.upsert_session(make_session("old"))
    store.upsert_session(make_session("new", updated_at=T0 + timedelta(hours=1)))
    assert [s.native_id for s in store.list_sessions()] == ["new", "old"]
    store.dismiss("claude:old")
    assert [s.native_id for s in store.list_sessions()] == ["new"]
    assert len(store.list_sessions(include_dismissed=True)) == 2


def test_dismissed_session_stays_dismissed_until_state_changes(store: Store) -> None:
    store.upsert_session(make_session())
    store.dismiss("claude:abc")
    assert store.upsert_session(make_session()).dismissed
    assert not store.upsert_session(make_session(state="waiting")).dismissed


def test_dismiss_unknown_session_raises(store: Store) -> None:
    with pytest.raises(StoreError):
        store.dismiss("claude:missing")


def test_data_survives_reopening(tmp_path: Path) -> None:
    first = Store(tmp_path / "g.db")
    first.upsert_session(make_session())
    first.close()
    again = Store(tmp_path / "g.db")
    assert again.get_session("claude:abc") is not None
    again.close()


# ---- steps --------------------------------------------------------------------------


def test_steps_are_oldest_first_and_update_the_session(store: Store) -> None:
    store.upsert_session(make_session())
    for i in range(5):
        store.add_step(
            Step("claude:abc", T0 + timedelta(seconds=i), "tool", f"étape {i}")
        )
    assert [s.summary for s in store.steps("claude:abc", limit=3)] == [
        "étape 2",
        "étape 3",
        "étape 4",
    ]
    session = store.get_session("claude:abc")
    assert session is not None
    assert session.last_step == "étape 4"
    assert session.updated_at == T0 + timedelta(seconds=4)


def test_step_for_unknown_session_is_refused(store: Store) -> None:
    with pytest.raises(StoreError):
        store.add_step(Step("claude:ghost", T0, "tool", "x"))


# ---- pendings -----------------------------------------------------------------------


def open_one(store: Store, **fields: object) -> Pending:
    store.upsert_session(make_session())
    return store.open_pending(
        Pending.new("claude:abc", "permission", options=("allow", "deny"), **fields)
    )


def test_open_pending_puts_the_session_waiting(store: Store) -> None:
    pending = open_one(store)
    assert store.get_session("claude:abc").state == "waiting"  # type: ignore[union-attr]
    assert store.pending_for("claude:abc") == [pending]
    with pytest.raises(StoreError, match="already exists"):
        store.open_pending(pending)


def test_resolve_returns_to_working_and_refuses_twice(store: Store) -> None:
    pending = open_one(store)
    done = store.resolve_pending(pending.id, {"decision": "allow"})
    assert done.status == "answered" and done.answer == {"decision": "allow"}
    assert store.get_session("claude:abc").state == "working"  # type: ignore[union-attr]
    assert store.pending_for("claude:abc") == []
    with pytest.raises(StoreError, match="already"):
        store.resolve_pending(pending.id, None, "cancelled")
    with pytest.raises(StoreError, match="cannot resolve"):
        store.resolve_pending(pending.id, None, "open")
    with pytest.raises(StoreError, match="unknown"):
        store.resolve_pending("nope", None)


def test_open_pending_needs_session_and_open_status(store: Store) -> None:
    with pytest.raises(StoreError):
        store.open_pending(Pending.new("claude:ghost", "question"))
    store.upsert_session(make_session())
    with pytest.raises(StoreError):
        store.open_pending(Pending.new("claude:abc", "question", status="answered"))


def test_wait_for_answer_wakes_when_answered(store: Store) -> None:
    pending = open_one(store)
    timer = threading.Timer(
        0.1, store.resolve_pending, (pending.id, {"decision": "deny"})
    )
    timer.start()
    result = store.wait_for_answer(pending.id, 5)
    timer.join()
    assert result.status == "answered"
    assert result.answer == {"decision": "deny"}


def test_wait_for_answer_times_out_as_expired(store: Store) -> None:
    pending = open_one(store)
    assert store.wait_for_answer(pending.id, 0.05).status == "expired"
    assert store.wait_for_answer(pending.id, 1).status == "expired"


def test_expire_overdue_only_touches_past_deadlines(store: Store) -> None:
    store.upsert_session(make_session())
    past = store.open_pending(
        Pending.new(
            "claude:abc", "question", expires_at=utcnow() - timedelta(seconds=1)
        )
    )
    future = store.open_pending(
        Pending.new("claude:abc", "question", expires_at=utcnow() + timedelta(hours=1))
    )
    assert [p.id for p in store.expire_overdue()] == [past.id]
    assert store.get_pending(future.id).status == "open"  # type: ignore[union-attr]


def test_dismiss_cancels_open_pendings(store: Store) -> None:
    pending = open_one(store)
    store.dismiss("claude:abc")
    assert store.get_pending(pending.id).status == "cancelled"  # type: ignore[union-attr]


def test_prune_removes_old_sessions_with_their_rows(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    old = T0 - timedelta(days=400)
    with monkeypatch.context() as mp:
        mp.setattr(store_mod, "utcnow", lambda: old)
        store.upsert_session(make_session("old", started_at=old, updated_at=old))
        store.add_step(Step("claude:old", old, "tool", "x"))
        store.open_pending(Pending.new("claude:old", "question"))
    store.upsert_session(make_session("fresh", updated_at=utcnow()))
    assert store.prune(older_than_days=7) == 1
    assert store.get_session("claude:old") is None
    assert store.steps("claude:old") == []
    assert store.get_session("claude:fresh") is not None


# ---- notifications ----------------------------------------------------------------


def drain(q: queue.Queue) -> list[str]:
    out = []
    while not q.empty():
        out.append(q.get_nowait()["type"])
    return out


def test_changes_are_published_to_subscribers(store: Store) -> None:
    q = store.subscribe()
    pending = open_one(store)
    store.add_step(Step("claude:abc", T0, "tool", "x"))
    store.resolve_pending(pending.id, {"decision": "allow"})
    assert drain(q) == [
        "session",
        "pending",
        "session",
        "step",
        "session",
        "resolved",
        "session",
    ]
    store.unsubscribe(q)
    store.upsert_session(make_session(state="idle"))
    assert q.empty()


def test_unchanged_upsert_and_timestamp_bumps_stay_quiet(store: Store) -> None:
    store.upsert_session(make_session())
    q = store.subscribe()
    store.upsert_session(make_session())
    store.upsert_session(make_session(updated_at=T0 + timedelta(seconds=2)))
    assert drain(q) == []


def test_stalled_subscriber_is_dropped(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(store_mod, "SUBSCRIBER_QUEUE_MAX", 1)
    q = store.subscribe()
    store.upsert_session(make_session("a"))
    store.upsert_session(make_session("b"))
    assert not store.is_subscribed(q)


# ---- usage --------------------------------------------------------------------------


def make_usage(**over: object) -> Usage:
    fields: dict[str, object] = {
        "model": "claude-x",
        "input_tokens": 1000,
        "output_tokens": 200,
        "cache_read_tokens": 50,
        "cache_write_tokens": 5,
        "context_tokens": 50_000,
        "context_limit": 200_000,
        "cost_usd": 0.42,
        "plan_used_pct": 12.5,
        "plan_resets_at": T0 + timedelta(hours=5),
        "updated_at": T0,
    }
    fields.update(over)
    return Usage(**fields)  # type: ignore[arg-type]


def test_context_pct() -> None:
    assert context_pct(make_usage()) == 25.0
    assert context_pct(make_usage(context_limit=None)) is None
    assert context_pct(make_usage(context_limit=0)) is None
    assert context_pct(None) is None


@pytest.mark.parametrize(
    "over",
    [
        {"input_tokens": -1},
        {"output_tokens": 1.5},
        {"cost_usd": -0.1},
        {"context_tokens": True},
    ],
)
def test_usage_validation(over: dict) -> None:
    with pytest.raises(ModelError):
        make_usage(**over)


def test_usage_json_round_trip_and_context_pct() -> None:
    usage = make_usage()
    data = to_json(usage)
    assert data["context_pct"] == 25.0
    assert usage_from_json(data) == usage
    session = make_session(usage=usage)
    assert session_from_json(to_json(session)) == session


def test_update_usage_persists_and_publishes(store: Store) -> None:
    store.upsert_session(make_session())
    q = store.subscribe()
    store.update_usage("claude:abc", make_usage())
    event = q.get_nowait()
    assert event["type"] == "usage"
    assert event["data"]["session_id"] == "claude:abc"
    assert event["data"]["usage"]["input_tokens"] == 1000
    assert store.get_session("claude:abc").usage == make_usage()  # type: ignore[union-attr]
    # a later upsert without usage keeps it
    assert store.upsert_session(make_session(state="idle")).usage == make_usage()
    with pytest.raises(StoreError):
        store.update_usage("claude:ghost", make_usage())
