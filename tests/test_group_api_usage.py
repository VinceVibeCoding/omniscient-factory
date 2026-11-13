"""omnigroup API: usage from hooks, store merge rule, collector answers, answer formats.

A real daemon on an ephemeral port with the real hooks.translate (no fake translation),
fake collectors, and transcripts written in tmp folders.
"""

from __future__ import annotations

import http.client
import json
import threading
from collections.abc import Iterator
from dataclasses import dataclass, field, replace
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from omniscient.group import server as srv
from omniscient.group.collect_agentrun import AgentRunCollector, answer_text
from omniscient.group.hooks import decision_output
from omniscient.group.models import Pending, Session, utcnow
from omniscient.group.store import Store
from omniscient.home import Home


@dataclass
class FakeCollector:
    name: str
    calls: list[tuple[str, dict]] = field(default_factory=list)

    def run(self, store: Any, stop: threading.Event) -> None:
        stop.wait()

    def answer(self, pending: Pending, answer: dict) -> tuple[bool, str]:
        self.calls.append((pending.id, answer))
        return True, "transmis"


@pytest.fixture
def server(tmp_path: Path) -> Iterator[srv.GroupServer]:
    collectors = [FakeCollector("herdr"), FakeCollector("agentrun")]
    srv_ = srv.create_server(Home(tmp_path), port=0, collectors=collectors)
    srv_.start_background()
    yield srv_
    srv_.stop()
    srv_.app.store.close()


def call(
    s: srv.GroupServer, method: str, path: str, body: Any = None
) -> tuple[int, Any]:
    conn = http.client.HTTPConnection("127.0.0.1", s.port, timeout=5)
    headers = {"Authorization": f"Bearer {s.app.token}"}
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    conn.request(method, path, body=data, headers=headers)
    resp = conn.getresponse()
    text = resp.read()
    conn.close()
    return resp.status, json.loads(text) if text else {}


def hook(s: srv.GroupServer, **event: Any) -> dict[str, Any]:
    status, body = call(s, "POST", "/hook", {"source": "claude", "event": event})
    assert status == 200, body
    return body


def reply(mid: str, inp: int, out: int, read: int = 0) -> str:
    usage = {"input_tokens": inp, "output_tokens": out, "cache_read_input_tokens": read}
    msg = {"id": mid, "model": "claude-test", "usage": usage}
    return json.dumps({"type": "assistant", "message": msg}) + "\n"


# ---- 1. usage from hook events ------------------------------------------------------


def test_stop_events_fold_the_transcript_with_one_cursor_per_session(
    server: srv.GroupServer, tmp_path: Path
) -> None:
    transcript = tmp_path / "t.jsonl"
    transcript.write_text(reply("m1", 100, 10, 50), encoding="utf-8")
    base = {"session_id": "u1", "transcript_path": str(transcript), "cwd": "/w/demo"}
    hook(server, hook_event_name="Stop", **base)
    usage = server.app.store.get_session("claude:u1").usage  # type: ignore[union-attr]
    assert usage is not None and usage.input_tokens == 100
    assert usage.context_tokens == 150
    with transcript.open("a", encoding="utf-8") as fh:
        fh.write(reply("m2", 20, 5))
    hook(server, hook_event_name="Stop", **base)
    _, listed = call(server, "GET", "/sessions")
    got = listed["sessions"][0]["usage"]
    assert got["input_tokens"] == 120 and got["output_tokens"] == 15  # each byte once
    assert set(server.app.usage_cursors) == {"claude:u1"}


def test_status_event_updates_usage_only(server: srv.GroupServer) -> None:
    hook(server, hook_event_name="UserPromptSubmit", session_id="u2", prompt="salut")
    store = server.app.store
    before = store.get_session("claude:u2")
    steps_before = len(store.steps("claude:u2"))
    status_line = {
        "hook_event_name": "StatusLine",
        "session_id": "u2",
        "model": {"id": "claude-test"},
        "context_window": {"context_window_size": 1000, "total_input_tokens": 250},
        "cost": {"total_cost_usd": 0.25},
        "rate_limits": {"five_hour": {"used_percentage": 40}},
    }
    assert hook(server, **status_line) == {"pending_id": None, "wait_s": None}
    after = store.get_session("claude:u2")
    assert after is not None and before is not None
    assert after.state == before.state and after.last_step == before.last_step
    assert len(store.steps("claude:u2")) == steps_before
    assert after.usage is not None and after.usage.cost_usd == 0.25
    assert after.usage.context_limit == 1000 and after.usage.plan_used_pct == 40.0


def test_status_event_for_an_unknown_session_creates_nothing(
    server: srv.GroupServer,
) -> None:
    hook(
        server,
        hook_event_name="StatusLine",
        session_id="ghost",
        cost={"total_cost_usd": 1},
    )
    assert server.app.store.get_session("claude:ghost") is None


def test_session_end_forgets_the_cursor(server: srv.GroupServer) -> None:
    hook(server, hook_event_name="SessionStart", session_id="u3")
    assert "claude:u3" in server.app.usage_cursors
    hook(server, hook_event_name="SessionEnd", session_id="u3")
    assert "claude:u3" not in server.app.usage_cursors


def test_upsert_keeps_started_at_and_dismissed(tmp_path: Path) -> None:
    store = Store(tmp_path / "g.db")
    t0 = utcnow() - timedelta(hours=1)
    first = Session("claude:m", "claude", "m", "demo", None, "idle", t0, t0)
    store.upsert_session(first)
    store.dismiss("claude:m")
    later = utcnow()
    same_state = replace(first, started_at=later, updated_at=later)
    kept = store.upsert_session(same_state)
    assert kept.started_at == t0 and kept.dismissed  # same state: stays crossed off
    back = store.upsert_session(replace(same_state, state="working"))
    assert back.started_at == t0 and not back.dismissed  # new state: back in the list
    store.close()


# ---- 2. collector-owned pendings are resolved in the store ---------------------------


def open_pending(s: srv.GroupServer, source: str, kind: str, **fields: Any) -> Pending:
    now = utcnow()
    s.app.store.upsert_session(
        Session(f"{source}:x", source, "x", "run", None, "working", now, now)  # type: ignore[arg-type]
    )
    return s.app.store.open_pending(Pending.new(f"{source}:x", kind, **fields))  # type: ignore[arg-type]


def test_collector_answer_resolves_the_pending(server: srv.GroupServer) -> None:
    p = open_pending(server, "agentrun", "question", payload={"question": "Nom ?"})
    status, body = call(
        server, "POST", f"/pending/{p.id}/answer", {"answers": {"Nom ?": "Ada"}}
    )
    assert status == 200 and body["message"] == "transmis"
    stored = server.app.store.get_pending(p.id)
    assert stored is not None and stored.status == "answered"
    assert stored.answer == {"answers": {"Nom ?": "Ada"}}
    assert server.app.store.get_session("agentrun:x").state == "working"  # type: ignore[union-attr]


# ---- 3. answer formats agree across widget, API, hooks and collectors ----------------


def test_answer_values_must_be_non_blank_text(server: srv.GroupServer) -> None:
    p = open_pending(server, "claude", "question", payload={"questions": []})
    for bad in ({"q": "  "}, {"q": ["a", "b"]}, {"q": "x" * 4001}):
        assert (
            call(server, "POST", f"/pending/{p.id}/answer", {"answers": bad})[0] == 400
        )
    long_text = "x" * 4000  # the widget's own bound for free text
    assert (
        call(server, "POST", f"/pending/{p.id}/answer", {"answers": {"q": long_text}})[
            0
        ]
        == 200
    )


def test_multi_select_labels_joined_reach_claude_unchanged() -> None:
    questions = [
        {
            "question": "Quoi ?",
            "multiSelect": True,
            "options": [{"label": "A"}, {"label": "B"}],
        }
    ]
    event = {
        "hook_event_name": "PreToolUse",
        "tool_name": "AskUserQuestion",
        "tool_input": {"questions": questions},
    }
    out = decision_output("claude", event, {"answers": {"Quoi ?": "A, B"}})
    assert out is not None
    updated = out["hookSpecificOutput"]["updatedInput"]
    assert updated == {"questions": questions, "answers": {"Quoi ?": "A, B"}}


def test_agentrun_free_text_answer_is_posted_as_content() -> None:
    sent: list[tuple[str, str, Any]] = []

    def api(method: str, path: str, body: Any = None) -> Any:
        sent.append((method, path, body))
        return {}

    collector = AgentRunCollector(api=api)
    pending = Pending.new(
        "agentrun:r1", "question", payload={"question": "Quelle base ?"}
    )
    assert answer_text({"answers": {"Quelle base ?": "SQLite"}}) == "SQLite"
    ok, _ = collector.answer(pending, {"answers": {"Quelle base ?": "SQLite"}})
    assert ok and sent[-1][2] == {"content": "SQLite"}
    assert sent[-1][0] == "POST" and sent[-1][1].endswith("/messages")
