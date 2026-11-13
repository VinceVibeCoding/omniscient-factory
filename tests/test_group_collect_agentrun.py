"""AgentRun collector, against a fake API (GET/POST/PATCH recorded) and a throwaway store."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from omniscient.group.collect_agentrun import (
    AgentRunCollector,
    AgentRunHttpError,
    answer_text,
    cost_usage,
    is_relevant,
    list_runs,
    run_path,
    run_state,
)
from omniscient.group.models import Pending
from omniscient.group.store import Store

NOW = datetime(2026, 1, 2, 12, 0, tzinfo=UTC)


@pytest.fixture
def store(tmp_path: Path) -> Store:
    return Store(tmp_path / "group.db")


def run(
    run_id: str, status: str, state: str = "working", **extra: Any
) -> dict[str, Any]:
    return {
        "run_id": run_id,
        "agent_id": "builder",
        "status": status,
        "state": state,
        "title": f"build {run_id}",
        "created_at": "2026-01-02T11:00:00Z",
        "ended_at": None,
        "console_url": f"http://127.0.0.1:7340/runs/{run_id}",
        **extra,
    }


class FakeApi:
    def __init__(
        self, pages: list[dict[str, Any]], rows: list[dict[str, Any]] | None = None
    ):
        self.pages = pages
        self.rows = rows or []
        self.calls: list[tuple[str, str, Mapping[str, Any] | None]] = []

    def __call__(
        self, method: str, path: str, body: Mapping[str, Any] | None = None
    ) -> Any:
        self.calls.append((method, path, body))
        if method == "GET" and path.startswith("/runs?cursor="):
            return self.pages[int(path.rsplit("=", 1)[1])]
        if method == "GET" and path == "/runs":
            return self.pages[0]
        if "/timeline" in path:
            final = path.endswith("after=7")
            rows = [] if final else self.rows
            return {"rows": rows, "next_after": 7, "final": final, "has_more": False}
        return {"ok": True}

    def writes(self) -> list[tuple[str, str, Mapping[str, Any] | None]]:
        return [c for c in self.calls if c[0] != "GET"]


def ar_collector(api: FakeApi) -> AgentRunCollector:
    return AgentRunCollector(api=api, clock=lambda: NOW)


def test_list_runs_follows_next_cursor() -> None:
    api = FakeApi(
        [
            {"runs": [run("r1", "working")], "next_cursor": "1"},
            {"runs": [run("r2", "paused")], "next_cursor": None},
        ]
    )
    assert [r["run_id"] for r in list_runs(api)] == ["r1", "r2"]
    assert [c[1] for c in api.calls] == ["/runs", "/runs?cursor=1"]


def test_list_runs_rejects_a_bad_page() -> None:
    with pytest.raises(AgentRunHttpError):
        list_runs(FakeApi([{"oops": 1}]))


def test_relevance_keeps_live_and_recently_ended_runs() -> None:
    recent = (NOW - timedelta(minutes=10)).isoformat()
    old = (NOW - timedelta(hours=3)).isoformat()
    assert is_relevant(run("a", "input_required"), NOW)
    assert is_relevant(run("b", "completed", ended_at=recent), NOW)
    assert not is_relevant(run("c", "completed", ended_at=old), NOW)


def test_run_state_mapping() -> None:
    assert run_state(run("a", "working")) == "working"
    assert run_state(run("a", "input_required", "waiting")) == "waiting"
    assert run_state(run("a", "input_required", "idle")) == "idle"
    assert run_state(run("a", "failed", "idle")) == "error"
    assert run_state(run("a", "completed", "idle")) == "done"


def test_run_path_refuses_odd_ids() -> None:
    assert run_path("r_1-x", "/stop") == "/runs/r_1-x/stop"
    with pytest.raises(AgentRunHttpError):
        run_path("../agents", "")


def test_poll_creates_sessions_and_steps_from_the_timeline(store: Store) -> None:
    rows = [
        {"kind": "user_message", "text": "build it", "created_at": "2026-01-02T11:00:01Z"},
        {"kind": "thinking", "text": "hmm", "created_at": "2026-01-02T11:00:02Z"},
        {"kind": "tool_use", "tool_name": "bash", "summary": "Ran ls", "tool_input_preview": "ls",
         "created_at": "2026-01-02T11:00:03Z"},
    ]  # fmt: skip
    api = FakeApi([{"runs": [run("r1", "working")], "next_cursor": None}], rows)
    col = ar_collector(api)
    col.poll(store)
    session = store.get_session("agentrun:r1")
    assert session is not None and session.state == "working"
    assert session.jump is not None and session.jump.kind == "url"
    assert [s.kind for s in store.steps("agentrun:r1")] == ["prompt", "tool"]
    col.poll(store)
    assert "/runs/r1/timeline?after=7" in [c[1] for c in api.calls]
    assert api.writes() == []  # polling never writes to AgentRun


def test_question_pending_is_opened_once_and_answered_by_message(store: Store) -> None:
    pending = {
        "kind": "question",
        "question": "Which DB?",
        "asked_at": "2026-01-02T11:30:00Z",
    }
    api = FakeApi(
        [
            {
                "runs": [run("r1", "input_required", "waiting", pending=pending)],
                "next_cursor": None,
            }
        ]
    )
    col = ar_collector(api)
    col.poll(store)
    col.poll(store)
    opened = store.pending_for("agentrun:r1")
    assert len(opened) == 1 and opened[0].payload == {"question": "Which DB?"}
    ok, _ = col.answer(opened[0], {"answers": {"Which DB?": "sqlite"}})
    assert ok
    assert api.writes() == [("POST", "/runs/r1/messages", {"content": "sqlite"})]


def test_pending_cleared_on_agentrun_is_cancelled_here(store: Store) -> None:
    pending = {"kind": "question", "question": "Q?", "asked_at": "2026-01-02T11:30:00Z"}
    api = FakeApi(
        [
            {
                "runs": [run("r1", "input_required", "waiting", pending=pending)],
                "next_cursor": None,
            }
        ]
    )
    col = ar_collector(api)
    col.poll(store)
    api.pages = [{"runs": [run("r1", "working")], "next_cursor": None}]
    col.poll(store)
    assert store.pending_for("agentrun:r1") == []


def budget_pending() -> dict[str, Any]:
    return {"kind": "budget", "cap_usd": 1.0, "spent_usd": 0.98, "needs_usd": 0.05,
            "least_cap_usd": 1.04, "suggested_cap_usd": 2.0, "said": "Raise the cap",
            "at": "2026-01-02T11:40:00Z"}  # fmt: skip


def test_budget_raise_patches_the_suggested_cap(store: Store) -> None:
    api = FakeApi(
        [
            {
                "runs": [run("r1", "paused", "idle", pending=budget_pending())],
                "next_cursor": None,
            }
        ]
    )
    col = ar_collector(api)
    col.poll(store)
    opened = store.pending_for("agentrun:r1")[0]
    assert opened.kind == "budget" and opened.options == ("raise", "stop")
    assert col.answer(opened, {"choice": "raise"})[0]
    assert api.writes() == [("PATCH", "/runs/r1", {"max_cost_usd": 2.0})]


def test_budget_stop_posts_stop(store: Store) -> None:
    api = FakeApi(
        [
            {
                "runs": [run("r1", "paused", "idle", pending=budget_pending())],
                "next_cursor": None,
            }
        ]
    )
    col = ar_collector(api)
    col.poll(store)
    opened = store.pending_for("agentrun:r1")[0]
    assert col.answer(opened, {"choice": "stop"})[0]
    assert col.answer(opened, {"choice": "nope"})[0] is False
    assert api.writes() == [("POST", "/runs/r1/stop", None)]


def test_approval_is_external_and_only_points_to_the_console(store: Store) -> None:
    raw = {"kind": "approval", "id": "ap1", "tool": "deploy", "call_id": "c1",
           "args_preview": "{}", "requested_at": "2026-01-02T11:50:00Z"}  # fmt: skip
    api = FakeApi(
        [
            {
                "runs": [run("r1", "input_required", "waiting", pending=raw)],
                "next_cursor": None,
            }
        ]
    )
    col = ar_collector(api)
    col.poll(store)
    opened = store.pending_for("agentrun:r1")[0]
    assert opened.kind == "approval_external" and opened.tool == "deploy"
    ok, message = col.answer(opened, {"decision": "allow"})
    assert not ok and "console" in message
    assert api.writes() == []


def test_answer_reports_http_errors_in_french() -> None:
    def failing(method: str, path: str, body: Mapping[str, Any] | None = None) -> Any:
        raise AgentRunHttpError("AgentRun a répondu 409")

    col = AgentRunCollector(api=failing)
    pending = Pending.new("agentrun:r1", "question", payload={"question": "Q"})
    ok, message = col.answer(pending, {"message": "yes"})
    assert not ok and "409" in message


def test_answer_text_prefers_answers_then_message() -> None:
    assert answer_text({"answers": {"a": "x", "b": "y"}}) == "x\ny"
    assert answer_text({"message": "hello"}) == "hello"


# ---- usage ---------------------------------------------------------------------------


def test_cost_usage_maps_dollars_and_falls_back_to_so_far() -> None:
    usage = cost_usage({"usd": 0.42, "basis": "estimate"}, NOW)
    assert usage is not None and usage.cost_usd == 0.42 and usage.input_tokens == 0
    partial = cost_usage({"usd": None, "so_far_usd": 0.1, "basis": "charge"}, NOW)
    assert partial is not None and partial.cost_usd == 0.1
    assert cost_usage({"usd": None, "basis": "charge"}, NOW) is None
    assert cost_usage(None, NOW) is None


def test_run_cost_and_cap_reach_the_session(store: Store) -> None:
    cost = {"usd": 0.3, "basis": "estimate", "cap_usd": 2.5}
    api = FakeApi([{"runs": [run("r1", "working", cost=cost)], "next_cursor": None}])
    ar_collector(api).poll(store)
    session = store.get_session("agentrun:r1")
    assert session is not None and session.usage is not None
    assert session.usage.cost_usd == 0.3
    assert session.meta["cap_usd"] == "2.5"
