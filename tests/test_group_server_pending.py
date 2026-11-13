"""omnigroup HTTP daemon: hook -> pending -> answer, session actions, SSE, usage."""

from __future__ import annotations

import http.client
import json
import threading
import time
from datetime import timedelta
from typing import Any

import pytest

from omniscient.group.models import Session, utcnow
from tests.group_support import Daemon, daemon, open_direct, post_hook

__all__ = ["daemon"]  # the fixture


# ---- hook -> pending -> answer --------------------------------------------------------


def test_tracking_hook_creates_session_and_step(daemon: Daemon) -> None:
    assert post_hook(daemon, summary="Read README") == {
        "pending_id": None,
        "wait_s": None,
    }
    status, body, _ = daemon.call("GET", "/sessions")
    assert status == 200
    [session] = body["sessions"]
    assert session["id"] == "claude:s1" and session["pending"] == []
    status, detail, _ = daemon.call("GET", "/sessions/claude:s1")
    assert [s["summary"] for s in detail["steps"]] == ["Read README"]
    assert detail["last_step"] == "Read README"


def test_permission_round_trip_through_long_poll(daemon: Daemon) -> None:
    reply = post_hook(daemon, ask=True)
    pid, ticket = reply["pending_id"], reply["wait_ticket"]
    assert reply["wait_s"] == 100
    _, listed, _ = daemon.call("GET", "/sessions")
    assert listed["sessions"][0]["state"] == "waiting"
    assert listed["sessions"][0]["pending"][0]["id"] == pid
    result: dict[str, Any] = {}

    def wait() -> None:
        result["value"] = daemon.wait(pid, ticket)

    waiter = threading.Thread(target=wait)
    waiter.start()
    time.sleep(0.2)
    status, body, _ = daemon.call(
        "POST", f"/pending/{pid}/answer", {"decision": "always"}
    )
    assert status == 200 and body["ok"] is True
    waiter.join(5)
    status, pending, _ = result["value"]
    assert status == 200
    assert pending["status"] == "answered" and pending["answer"] == {
        "decision": "always"
    }
    status, _, _ = daemon.call("POST", f"/pending/{pid}/answer", {"decision": "deny"})
    assert status == 409


def test_wait_times_out_as_expired_after_at_least_a_second(daemon: Daemon) -> None:
    reply = post_hook(daemon, ask=True)
    started = time.monotonic()
    status, body, _ = daemon.wait(reply["pending_id"], reply["wait_ticket"], "0")
    assert status == 200 and body["status"] == "expired"
    assert time.monotonic() - started >= 0.9  # timeout=0 is raised to 1 s (M10)


def test_wait_rejects_bad_timeout(daemon: Daemon) -> None:
    reply = post_hook(daemon, ask=True)
    assert daemon.wait(reply["pending_id"], reply["wait_ticket"], "-1")[0] == 400


def test_answer_is_validated_against_the_kind(daemon: Daemon) -> None:
    pid = post_hook(daemon, ask=True)["pending_id"]
    assert daemon.call("POST", f"/pending/{pid}/answer", {"decision": "yes"})[0] == 400
    assert daemon.call("POST", f"/pending/{pid}/answer", {"choice": "stop"})[0] == 400
    status, _, _ = daemon.call(
        "POST", f"/pending/{pid}/answer", {"decision": "deny", "message": "x" * 2000}
    )
    assert status == 400


def test_question_and_budget_answers(daemon: Daemon) -> None:
    q = open_direct(daemon, "claude", "question", payload={"questions": []})
    assert daemon.call("POST", f"/pending/{q.id}/answer", {"answers": {}})[0] == 400
    status, body, _ = daemon.call(
        "POST", f"/pending/{q.id}/answer", {"answers": {"Quelle base ?": "SQLite"}}
    )
    assert (
        status == 200
        and body["pending"]["answer"]["answers"]["Quelle base ?"] == "SQLite"
    )
    ext = open_direct(daemon, "claude", "approval_external")
    status, body, _ = daemon.call("POST", f"/pending/{ext.id}/answer", {})
    assert status == 409 and "console" in body["error"]


def test_collector_owned_pending_goes_through_the_collector(daemon: Daemon) -> None:
    pending = open_direct(daemon, "herdr", "permission", options=("allow", "deny"))
    assert (
        daemon.call("POST", f"/pending/{pending.id}/answer", {"decision": "always"})[0]
        == 400
    )
    daemon.herdr.ok = False
    status, body, _ = daemon.call(
        "POST", f"/pending/{pending.id}/answer", {"decision": "allow"}
    )
    assert status == 502 and body == {"ok": False, "message": "herdr ne répond pas"}
    assert daemon.server.app.store.get_pending(pending.id).is_open  # type: ignore[union-attr]
    daemon.herdr.ok = True
    status, body, _ = daemon.call(
        "POST", f"/pending/{pending.id}/answer", {"decision": "allow"}
    )
    assert status == 200 and body["message"] == "envoyé"
    assert daemon.herdr.calls[-1] == (pending.id, {"decision": "allow"})


def test_missing_collector_is_reported(daemon: Daemon) -> None:
    pending = open_direct(daemon, "agentrun", "budget", options=("raise", "stop"))
    status, body, _ = daemon.call(
        "POST", f"/pending/{pending.id}/answer", {"choice": "stop"}
    )
    assert status == 503 and "agentrun" in body["error"]


def test_janitor_expires_overdue_pendings(
    daemon: Daemon, monkeypatch: pytest.MonkeyPatch
) -> None:
    pending = open_direct(
        daemon, "claude", "question", expires_at=utcnow() - timedelta(seconds=1)
    )
    expired = daemon.server.app.store.expire_overdue()
    assert [p.id for p in expired] == [pending.id]


# ---- session actions ------------------------------------------------------------------


def test_dismiss_jump_and_prompt(daemon: Daemon) -> None:
    post_hook(daemon)
    status, body, _ = daemon.call("POST", "/sessions/claude:s1/jump")
    assert body == {"ok": True, "message": "Ouverture de Démo"}
    assert daemon.call("POST", "/sessions/claude:s1/prompt", {"text": "  "})[0] == 400
    status, body, _ = daemon.call(
        "POST", "/sessions/claude:s1/prompt", {"text": "continue"}
    )
    assert (
        status == 200 and body["ok"] is False and daemon.actions.prompts == ["continue"]
    )
    status, body, _ = daemon.call("POST", "/sessions/claude:s1/dismiss")
    assert status == 200 and body["session"]["dismissed"] is True
    assert daemon.call("GET", "/sessions")[1]["sessions"] == []
    assert len(daemon.call("GET", "/sessions?dismissed=1")[1]["sessions"]) == 1
    assert daemon.call("POST", "/sessions/claude:nope/dismiss")[0] == 404


def test_theme_is_served(daemon: Daemon) -> None:
    status, body, _ = daemon.call("GET", "/theme")
    assert status == 200
    assert body["colors"]["background"].startswith("#")
    assert len(body["ansi"]) == 16


def test_internal_errors_do_not_leak(daemon: Daemon) -> None:
    daemon.actions.jump = lambda session: 1 / 0  # type: ignore[method-assign]
    post_hook(daemon)
    status, body, _ = daemon.call("POST", "/sessions/claude:s1/jump")
    assert status == 500 and body == {"error": "erreur interne"}


# ---- SSE --------------------------------------------------------------------------------


def read_events(resp: http.client.HTTPResponse, want: int) -> list[str]:
    lines: list[str] = []
    while len([x for x in lines if x.startswith(("event:", ": heartbeat"))]) < want:
        line = resp.fp.readline().decode().rstrip("\n")
        if line:
            lines.append(line)
    return lines


def test_stream_pushes_events_and_heartbeats(daemon: Daemon) -> None:
    conn = daemon.conn()
    conn.request(
        "GET", "/stream", headers={"Authorization": f"Bearer {daemon.widget_token}"}
    )
    resp = conn.getresponse()
    assert resp.status == 200
    assert resp.getheader("Content-Type").startswith("text/event-stream")
    assert resp.fp.readline().startswith(b": ")
    post_hook(daemon)
    lines = read_events(resp, 2)
    assert lines[0] == "event: session"
    assert json.loads(lines[1].removeprefix("data: "))["id"] == "claude:s1"
    assert ": heartbeat" in read_events(resp, 4)
    resp.close()
    conn.close()
    store = daemon.server.app.store
    deadline = time.monotonic() + 5
    while store._subscribers and time.monotonic() < deadline:
        store.upsert_session(  # a write makes the server notice the closed socket
            Session(
                "claude:x",
                "claude",
                "x",
                str(time.time()),
                None,
                "idle",
                utcnow(),
                utcnow(),
            )
        )
        time.sleep(0.1)
    assert store._subscribers == []


def test_stream_needs_the_token(daemon: Daemon) -> None:
    assert daemon.call("GET", "/stream", auth=False)[0] == 401


def test_usage_is_listed_and_totalled(daemon: Daemon) -> None:
    from omniscient.group.models import Usage

    post_hook(daemon, native="a")
    post_hook(daemon, native="b")
    store = daemon.server.app.store
    now = utcnow()
    for sid, cost in (("claude:a", 0.5), ("claude:b", None)):
        store.update_usage(
            sid, Usage("m", 100, 10, 0, 0, 1000, 4000, cost, 30.0, None, now)
        )
    _, listed, _ = daemon.call("GET", "/sessions")
    assert listed["sessions"][0]["usage"]["context_pct"] == 25.0
    _, detail, _ = daemon.call("GET", "/sessions/claude:a")
    assert detail["usage"]["cost_usd"] == 0.5
    status, totals, _ = daemon.call("GET", "/usage")
    assert status == 200
    assert totals["input_tokens"] == 200 and totals["output_tokens"] == 20
    assert totals["sessions"] == 2 and totals["cost_usd"] == 0.5
    assert totals["plan_used_pct"] == 30.0
    daemon.call("POST", "/sessions/claude:a/dismiss")
    assert daemon.call("GET", "/usage")[1]["cost_usd"] is None
