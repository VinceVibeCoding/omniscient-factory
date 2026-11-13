"""Split credentials (H1), daemon proof (M5), widget presence (M8), limits (M10),
claim-first answers (H2) and truncated previews (M1), against a real daemon."""

from __future__ import annotations

import hashlib
import hmac
import http.client
import threading
import time
from pathlib import Path

import pytest

from omniscient.group import server as srv
from omniscient.group.credentials import token_hash
from omniscient.group.gates import Presence
from tests.group_support import Daemon, daemon, open_direct, post_hook, start_daemon

__all__ = ["daemon"]  # the fixture

WIDGET_ROUTES = [
    ("GET", "/sessions", None),
    ("GET", "/sessions/claude:s1", None),
    ("GET", "/stream", None),
    ("GET", "/usage", None),
    ("GET", "/theme", None),
    ("POST", "/pending/p1/answer", {"decision": "allow"}),
    ("POST", "/sessions/claude:s1/prompt", {"text": "x"}),
    ("POST", "/sessions/claude:s1/dismiss", {}),
    ("POST", "/sessions/claude:s1/jump", {}),
]


@pytest.mark.parametrize(("method", "path", "body"), WIDGET_ROUTES)
def test_hook_credential_is_refused_on_widget_routes(
    daemon: Daemon, method: str, path: str, body: dict | None
) -> None:
    status, reply, _ = daemon.call(method, path, body, auth="hook")
    assert status == 403 and "jeton" in reply["error"]


def test_widget_token_is_refused_on_the_hook_routes(daemon: Daemon) -> None:
    event = {"source": "claude", "event": {"session_id": "s1"}}
    assert daemon.call("POST", "/hook", event, auth="widget")[0] == 403
    reply = post_hook(daemon, ask=True)
    status, _, _ = daemon.call(
        "GET",
        f"/pending/{reply['pending_id']}/wait?timeout=1",
        auth="widget",
        headers={"X-Omnigroup-Ticket": reply["wait_ticket"]},
    )
    assert status == 403


def test_wait_needs_the_ticket_of_that_pending_once(daemon: Daemon) -> None:
    first = post_hook(daemon, native="a", ask=True)
    second = post_hook(daemon, native="b", ask=True)
    assert daemon.wait(first["pending_id"], None)[0] == 403
    assert daemon.wait(first["pending_id"], second["wait_ticket"])[0] == 403
    daemon.call("POST", f"/pending/{first['pending_id']}/answer", {"decision": "deny"})
    ok = daemon.wait(first["pending_id"], first["wait_ticket"])
    assert ok[0] == 200 and ok[1]["status"] == "answered"
    assert daemon.wait(first["pending_id"], first["wait_ticket"])[0] == 403  # used


def test_health_proves_the_daemon_knows_the_widget_token(daemon: Daemon) -> None:
    nonce = "n" * 24
    status, body, _ = daemon.call("GET", f"/health?nonce={nonce}", auth=False)
    key = bytes.fromhex(token_hash(daemon.widget_token))
    expected = hmac.new(key, nonce.encode(), hashlib.sha256).hexdigest()
    assert status == 200 and body["proofs"] == [expected]
    assert daemon.call("GET", "/health?nonce=short", auth=False)[0] == 400
    assert "proofs" not in daemon.call("GET", "/health", auth=False)[1]


def test_no_widget_connected_means_no_pending(daemon: Daemon) -> None:
    clock = [1000.0]
    presence = Presence(clock=lambda: clock[0])
    object.__setattr__(daemon.server.app, "presence", presence)
    assert post_hook(daemon, ask=True) == {"pending_id": None, "wait_s": 0}
    presence.enter()
    presence.leave()  # a widget was connected moments ago
    assert post_hook(daemon, ask=True)["wait_s"] == 100
    clock[0] += 31
    assert post_hook(daemon, ask=True)["wait_s"] == 0


def test_stream_count_is_capped(tmp_path: Path) -> None:
    d = start_daemon(tmp_path)
    object.__setattr__(d.server.app, "presence", Presence(max_streams=1))
    try:
        conn = d.conn()
        auth = {"Authorization": f"Bearer {d.widget_token}"}
        conn.request("GET", "/stream", headers=auth)
        assert conn.getresponse().status == 200
        assert d.call("GET", "/stream")[0] == 503
        conn.close()
    finally:
        d.server.close_all()


def test_connections_are_capped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(srv, "MAX_CONNECTIONS", 1)
    d = start_daemon(tmp_path)
    try:
        conn = d.conn()
        conn.request(
            "GET", "/stream", headers={"Authorization": f"Bearer {d.widget_token}"}
        )
        assert conn.getresponse().status == 200  # holds the only slot
        with pytest.raises((http.client.HTTPException, ConnectionError)):
            d.call("GET", "/health", auth=False)
        conn.close()
    finally:
        d.server.close_all()


class SlowCollector:
    name = "herdr"

    def __init__(self) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()
        self.calls = 0

    def run(self, store: object, stop: threading.Event) -> None:
        stop.wait()

    def answer(self, pending: object, answer: dict) -> tuple[bool, str]:
        self.calls += 1
        self.entered.set()
        self.release.wait(5)
        return True, "envoyé"


def test_a_second_concurrent_answer_is_refused_before_the_collector(
    daemon: Daemon,
) -> None:
    slow = SlowCollector()
    object.__setattr__(daemon.server.app, "collectors", (slow,))
    pending = open_direct(daemon, "herdr", "permission", options=("allow", "deny"))
    path = f"/pending/{pending.id}/answer"
    results: list[int] = []
    first = threading.Thread(
        target=lambda: results.append(
            daemon.call("POST", path, {"decision": "allow"})[0]
        )
    )
    first.start()
    assert slow.entered.wait(5)
    assert daemon.call("POST", path, {"decision": "deny"})[0] == 409
    slow.release.set()
    first.join(5)
    assert results == [200] and slow.calls == 1


def test_a_crashing_collector_releases_the_claim(daemon: Daemon) -> None:
    class Boom(SlowCollector):
        def answer(self, pending: object, answer: dict) -> tuple[bool, str]:
            raise RuntimeError("boom")

    object.__setattr__(daemon.server.app, "collectors", (Boom(),))
    pending = open_direct(daemon, "herdr", "permission", options=("allow", "deny"))
    status, _, _ = daemon.call(
        "POST", f"/pending/{pending.id}/answer", {"decision": "allow"}
    )
    assert status == 502
    assert daemon.server.app.store.get_pending(pending.id).status == "open"  # type: ignore[union-attr]


def test_truncated_preview_can_only_be_denied(daemon: Daemon) -> None:
    pending = open_direct(
        daemon,
        "claude",
        "permission",
        payload={"tool_input": "x" * 10, "truncated": 70000},
        options=("allow", "deny"),
    )
    path = f"/pending/{pending.id}/answer"
    status, body, _ = daemon.call("POST", path, {"decision": "allow"})
    assert status == 409 and "terminal" in body["error"]
    assert daemon.call("POST", path, {"decision": "deny"})[0] == 200


def test_widget_pending_lists_are_capped(daemon: Daemon) -> None:
    for _ in range(25):
        open_direct(daemon, "claude", "question")
        time.sleep(0.001)
    sessions = daemon.call("GET", "/sessions")[1]["sessions"]
    assert len(sessions[0]["pending"]) == 20
