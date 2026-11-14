"""herdr collector, against a fake herdr CLI and a throwaway store."""

from __future__ import annotations

import json
import threading
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from omniscient.group.collect_herdr import (
    HerdrCollector,
    claude_link,
    herdr_usage,
    keys_for,
    parse_agents,
    parse_read,
    run_loop,
)
from omniscient.group.models import Session, Usage
from omniscient.group.store import Store
from omniscient.studio.herdr import HerdrError

NOW = datetime(2026, 1, 2, 12, 0, tzinfo=UTC)


@pytest.fixture
def store(tmp_path: Path) -> Store:
    return Store(tmp_path / "group.db")


# ---- herdr ---------------------------------------------------------------------------


def agent(pane: str, status: str, **extra: Any) -> dict[str, Any]:
    return {
        "pane_id": pane,
        "terminal_id": "t1",
        "workspace_id": "w1",
        "tab_id": "w1:t1",
        "focused": False,
        "revision": 1,
        "agent_status": status,
        "agent": "claude",
        "name": f"agent-{pane}",
        "cwd": "/work/demo",
        **extra,
    }


class FakeHerdr:
    """Answers `herdr agent list|get|read|send-keys` from in-memory agents."""

    def __init__(
        self, agents: list[dict[str, Any]], dialog: str = "Bash: ls\n❯ 1. Yes"
    ) -> None:
        self.agents = agents
        self.dialog = dialog
        self.calls: list[list[str]] = []

    def __call__(self, args: Sequence[str]) -> str:
        self.calls.append(list(args))
        sub = args[1]
        if sub == "list":
            return json.dumps(
                {"id": "x", "result": {"type": "agent_list", "agents": self.agents}}
            )
        if sub == "get":
            found = next(a for a in self.agents if a["pane_id"] == args[2])
            return json.dumps(
                {"id": "x", "result": {"type": "agent_info", "agent": found}}
            )
        if sub == "read":
            read = {"text": self.dialog, "pane_id": args[2]}
            return json.dumps(
                {"id": "x", "result": {"type": "pane_read", "read": read}}
            )
        if sub in ("send-keys", "focus", "prompt"):
            return json.dumps({"id": "x", "result": {"type": "ok"}})
        raise AssertionError(args)

    def sent_keys(self) -> list[list[str]]:
        return [c for c in self.calls if c[1] == "send-keys"]


def collector(fake: FakeHerdr) -> HerdrCollector:
    return HerdrCollector(runner=fake, clock=lambda: NOW)


def test_parse_agents_reads_the_envelope_and_skips_bad_panes() -> None:
    out = json.dumps(
        {"result": {"agents": [agent("w1:p1", "idle"), {"pane_id": "bad id!"}]}}
    )
    assert [a["pane_id"] for a in parse_agents(out)] == ["w1:p1"]


@pytest.mark.parametrize("out", ["not json", "[]", '{"result": {"agents": 3}}'])
def test_parse_agents_rejects_unexpected_shapes(out: str) -> None:
    with pytest.raises(HerdrError):
        parse_agents(out)


def test_parse_read_accepts_json_or_plain_text() -> None:
    assert parse_read(json.dumps({"result": {"read": {"text": "hi"}}})) == "hi"
    assert parse_read("plain dialog") == "plain dialog"


def test_idle_pane_becomes_a_herdr_session(store: Store) -> None:
    collector(FakeHerdr([agent("w1:p1", "idle")])).poll(store)
    session = store.get_session("herdr:w1:p1")
    assert session is not None
    assert (session.state, session.title, session.cwd) == (
        "idle",
        "agent-w1:p1",
        "/work/demo",
    )
    assert session.jump is not None and session.jump.kind == "herdr"
    assert store.steps("herdr:w1:p1")[-1].kind == "notice"


def test_blocked_pane_opens_one_permission_pending_with_the_dialog(
    store: Store,
) -> None:
    fake = FakeHerdr([agent("w1:p1", "blocked")])
    col = collector(fake)
    col.poll(store)
    col.poll(store)
    pendings = store.pending_for("herdr:w1:p1")
    assert len(pendings) == 1
    assert pendings[0].kind == "permission"
    assert pendings[0].options == ("allow", "deny")
    assert "Bash: ls" in pendings[0].payload["text"]
    assert ["agent", "read", "w1:p1", "--source", "detection"] in fake.calls


def test_unblocked_pane_cancels_its_pending(store: Store) -> None:
    fake = FakeHerdr([agent("w1:p1", "blocked")])
    col = collector(fake)
    col.poll(store)
    fake.agents = [agent("w1:p1", "working")]
    col.poll(store)
    assert store.pending_for("herdr:w1:p1") == []
    session = store.get_session("herdr:w1:p1")
    assert session is not None and session.state == "working"


def test_vanished_pane_ends_done(store: Store) -> None:
    fake = FakeHerdr([agent("w1:p1", "working")])
    col = collector(fake)
    col.poll(store)
    fake.agents = []
    col.poll(store)
    session = store.get_session("herdr:w1:p1")
    assert session is not None and session.state == "done"
    assert store.steps("herdr:w1:p1")[-1].kind == "stop"


def test_claude_pane_known_from_hooks_gains_a_herdr_jump(store: Store) -> None:
    uuid = "0b1c2d3e-aaaa-bbbb-cccc-000000000001"
    store.upsert_session(
        Session(
            id=f"claude:{uuid}",
            source="claude",
            native_id=uuid,
            title="hook session",
            cwd="/work/demo",
            state="working",
            started_at=NOW,
            updated_at=NOW,
        )
    )
    ref = {"source": "herdr", "agent": "claude", "kind": "id", "value": uuid}
    collector(FakeHerdr([agent("w1:p1", "blocked", agent_session=ref)])).poll(store)
    linked = store.get_session(f"claude:{uuid}")
    assert linked is not None and linked.jump is not None
    assert (linked.jump.kind, linked.jump.value) == ("herdr", "w1:p1")
    assert store.get_session("herdr:w1:p1") is None
    assert store.pending_for(f"claude:{uuid}") == []  # hooks own Claude's permissions


def test_claude_link_needs_an_id_reference() -> None:
    ref = {"source": "herdr", "agent": "claude", "kind": "path", "value": "/x.jsonl"}
    assert claude_link(agent("p", "idle", agent_session=ref)) is None
    assert claude_link(agent("p", "idle")) is None


def test_answer_sends_mapped_keys_when_still_blocked(store: Store) -> None:
    fake = FakeHerdr([agent("w1:p1", "blocked")])
    col = collector(fake)
    col.poll(store)
    pending = store.pending_for("herdr:w1:p1")[0]
    assert col.answer(pending, {"decision": "allow"})[0] is True
    assert col.answer(pending, {"decision": "deny"})[0] is True
    assert fake.sent_keys() == [
        ["agent", "send-keys", "w1:p1", "enter"],
        ["agent", "send-keys", "w1:p1", "esc"],
    ]


def test_answer_sends_nothing_once_the_pane_moved_on(store: Store) -> None:
    fake = FakeHerdr([agent("w1:p1", "blocked")])
    col = collector(fake)
    col.poll(store)
    pending = store.pending_for("herdr:w1:p1")[0]
    fake.agents = [agent("w1:p1", "working")]
    ok, message = col.answer(pending, {"decision": "allow"})
    assert not ok and "plus" in message
    assert fake.sent_keys() == []


def test_answer_refuses_unknown_decisions(store: Store) -> None:
    fake = FakeHerdr([agent("w1:p1", "blocked")])
    col = collector(fake)
    col.poll(store)
    pending = store.pending_for("herdr:w1:p1")[0]
    assert col.answer(pending, {"decision": "always"})[0] is False
    assert fake.sent_keys() == []


def test_keys_are_disabled_for_unverified_agents() -> None:
    assert keys_for("claude", "allow") == ("enter",)
    assert keys_for("someagent", "deny") is None
    assert keys_for("claude", "always") is None


def test_run_loop_survives_a_failing_poll() -> None:
    stop = threading.Event()
    calls: list[int] = []

    def poll() -> None:
        calls.append(1)
        if len(calls) == 1:
            raise HerdrError("boom")
        stop.set()

    run_loop("t", poll, stop, 0)
    assert len(calls) == 2


class _CountingStop(threading.Event):
    """Records each wait delay and stops after `limit` waits."""

    def __init__(self, limit: int) -> None:
        super().__init__()
        self.delays: list[float] = []
        self.limit = limit

    def wait(self, timeout: float | None = None) -> bool:
        self.delays.append(timeout or 0.0)
        if len(self.delays) >= self.limit:
            self.set()
        return self.is_set()


def test_run_loop_logs_a_repeated_failure_once_and_backs_off(caplog) -> None:
    stop = _CountingStop(limit=8)

    def poll() -> None:
        raise HerdrError("no herdr server is running")

    with caplog.at_level("INFO", logger="omniscient.group.collect_common"):
        run_loop("t", poll, stop, 2.0)
    tracebacks = [r for r in caplog.records if r.exc_info]
    assert len(tracebacks) == 1
    assert len(caplog.records) == 1
    assert stop.delays[:6] == [4.0, 8.0, 16.0, 32.0, 60.0, 60.0]
    assert max(stop.delays) == 60.0


def test_run_loop_logs_recovery_and_new_errors(caplog) -> None:
    stop = _CountingStop(limit=5)
    script = [HerdrError("down"), HerdrError("down"), None, HerdrError("other"), None]

    def poll() -> None:
        outcome = script.pop(0)
        if outcome is not None:
            raise outcome

    with caplog.at_level("INFO", logger="omniscient.group.collect_common"):
        run_loop("t", poll, stop, 1.0)
    messages = [r.getMessage() for r in caplog.records]
    assert sum("poll failed" in m for m in messages) == 2  # "down" once, "other" once
    assert sum("recovered" in m for m in messages) == 2
    assert stop.delays == [2.0, 4.0, 1.0, 2.0, 1.0]


# ---- usage ---------------------------------------------------------------------------

TOKENS = {"context_tokens": "45,000", "context_limit": "200000", "model": "opus"}


def test_herdr_usage_reads_known_display_tokens_only() -> None:
    usage = herdr_usage(agent("p", "idle", tokens=TOKENS), NOW)
    assert usage is not None
    assert (usage.context_tokens, usage.context_limit, usage.model) == (
        45000,
        200000,
        "opus",
    )
    assert herdr_usage(agent("p", "idle", tokens={"branch": "main"}), NOW) is None
    assert herdr_usage(agent("p", "idle"), NOW) is None


def test_herdr_pane_usage_is_stored(store: Store) -> None:
    collector(FakeHerdr([agent("w1:p1", "idle", tokens=TOKENS)])).poll(store)
    session = store.get_session("herdr:w1:p1")
    assert session is not None and session.usage is not None
    assert session.usage.context_tokens == 45000


def hook_session(store: Store, uuid: str, usage: Usage | None) -> None:
    store.upsert_session(
        Session(id=f"claude:{uuid}", source="claude", native_id=uuid, title="hook",
                cwd=None, state="working", started_at=NOW, updated_at=NOW)
    )  # fmt: skip
    if usage is not None:
        store.update_usage(f"claude:{uuid}", usage)


def linked_agent(uuid: str, tokens: dict[str, str]) -> dict[str, Any]:
    ref = {"source": "herdr", "agent": "claude", "kind": "id", "value": uuid}
    return agent("w1:p1", "working", agent_session=ref, tokens=tokens)


def test_linked_claude_session_keeps_hook_usage(store: Store) -> None:
    uuid = "0b1c2d3e-aaaa-bbbb-cccc-000000000002"
    rich = Usage(model="claude-opus", input_tokens=10, output_tokens=20, cache_read_tokens=5,
                 cache_write_tokens=1, context_tokens=90000, context_limit=200000,
                 cost_usd=0.5, plan_used_pct=12.0, plan_resets_at=None, updated_at=NOW)  # fmt: skip
    hook_session(store, uuid, rich)
    collector(FakeHerdr([linked_agent(uuid, TOKENS)])).poll(store)
    linked = store.get_session(f"claude:{uuid}")
    assert linked is not None and linked.usage == rich


def test_linked_claude_session_without_usage_gets_herdr_usage_and_updates(
    store: Store,
) -> None:
    uuid = "0b1c2d3e-aaaa-bbbb-cccc-000000000003"
    hook_session(store, uuid, None)
    fake = FakeHerdr([linked_agent(uuid, TOKENS)])
    col = collector(fake)
    col.poll(store)
    fake.agents = [linked_agent(uuid, {**TOKENS, "context_tokens": "50000"})]
    col.poll(store)
    linked = store.get_session(f"claude:{uuid}")
    assert linked is not None and linked.usage is not None
    assert linked.usage.context_tokens == 50000
