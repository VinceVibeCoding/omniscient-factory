"""omnigroup HTTP daemon: a real server on an ephemeral port, fakes for hooks/collectors."""

from __future__ import annotations

import http.client
import json
import stat
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from omniscient.group import server as srv
from omniscient.group.models import Pending, Session, Step, utcnow
from omniscient.home import Home

TAURI = "http://tauri.localhost"


@dataclass(frozen=True)
class FakeTranslation:
    session: Session
    step: Step | None
    pending: Pending | None
    wait_s: int | None


def fake_translate(source: str, event: dict[str, Any]) -> FakeTranslation:
    if "boom" in event:
        raise ValueError("événement illisible")
    native = event["session_id"]
    now = utcnow()
    session = Session(
        id=f"{source}:{native}",
        source=source,  # type: ignore[arg-type]
        native_id=native,
        title="Démo",
        cwd="/tmp/demo",
        state="working",
        started_at=now,
        updated_at=now,
    )
    step = Step(session.id, now, "tool", event.get("summary", "Bash ls"))
    pending = None
    if event.get("ask"):
        pending = Pending.new(
            session.id, "permission", tool="Bash", options=("allow", "deny", "always")
        )
    return FakeTranslation(session, step, pending, 100 if pending else None)


@dataclass
class FakeCollector:
    name: str
    ok: bool = True
    calls: list[tuple[str, dict]] = field(default_factory=list)
    ran: threading.Event = field(default_factory=threading.Event)

    def run(self, store: Any, stop: threading.Event) -> None:
        self.ran.set()
        stop.wait()

    def answer(self, pending: Pending, answer: dict) -> tuple[bool, str]:
        self.calls.append((pending.id, answer))
        return self.ok, "envoyé" if self.ok else "herdr ne répond pas"


class FakeActions:
    def __init__(self) -> None:
        self.prompts: list[str] = []

    def jump(self, session: Session) -> tuple[bool, str]:
        return True, f"Ouverture de {session.title}"

    def prompt(self, session: Session, text: str) -> dict[str, Any]:
        self.prompts.append(text)
        return {"ok": False, "message": "Lance : claude -p --resume"}


@dataclass
class Daemon:
    server: srv.GroupServer
    token: str
    herdr: FakeCollector
    actions: FakeActions

    def conn(self, timeout: float = 5) -> http.client.HTTPConnection:
        return http.client.HTTPConnection(
            "127.0.0.1", self.server.port, timeout=timeout
        )

    def call(
        self,
        method: str,
        path: str,
        body: Any = None,
        *,
        auth: bool = True,
        headers: dict[str, str] | None = None,
        raw: bytes | None = None,
    ) -> tuple[int, dict[str, Any], http.client.HTTPResponse]:
        hdrs = dict(headers or {})
        if auth:
            hdrs.setdefault("Authorization", f"Bearer {self.token}")
        data = raw
        if body is not None:
            data = json.dumps(body).encode()
            hdrs.setdefault("Content-Type", "application/json")
        conn = self.conn()
        conn.request(method, path, body=data, headers=hdrs)
        resp = conn.getresponse()
        text = resp.read()
        conn.close()
        return resp.status, json.loads(text) if text else {}, resp


@pytest.fixture
def daemon(tmp_path: Path) -> Iterator[Daemon]:
    herdr = FakeCollector("herdr")
    actions = FakeActions()
    server = srv.create_server(
        Home(tmp_path),
        port=0,
        collectors=[herdr],
        translate=fake_translate,
        actions=actions,
        heartbeat_s=0.2,
    )
    server.start_background()
    assert herdr.ran.wait(2)
    yield Daemon(server, server.app.token, herdr, actions)
    server.stop()
    server.app.store.close()


def post_hook(d: Daemon, native: str = "s1", **event: Any) -> dict[str, Any]:
    status, body, _ = d.call(
        "POST", "/hook", {"source": "claude", "event": {"session_id": native, **event}}
    )
    assert status == 200, body
    return body


# ---- token and binding --------------------------------------------------------------


def test_token_file_is_private_and_reused(tmp_path: Path) -> None:
    home = Home(tmp_path)
    token = srv.load_or_create_token(home)
    path = srv.token_path(home)
    assert len(token) >= srv.TOKEN_MIN_LEN
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    path.chmod(0o644)
    assert srv.load_or_create_token(home) == token
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert srv.read_token(home) == token


def test_short_token_file_is_refused(tmp_path: Path) -> None:
    home = Home(tmp_path)
    srv.token_path(home).parent.mkdir(parents=True)
    srv.token_path(home).write_text("short")
    with pytest.raises(RuntimeError, match="jeton"):
        srv.load_or_create_token(home)


def test_binds_loopback_only(daemon: Daemon) -> None:
    assert daemon.server.server_address[0] == "127.0.0.1"


def test_health_needs_no_token(daemon: Daemon) -> None:
    status, body, _ = daemon.call("GET", "/health", auth=False)
    assert status == 200 and body["ok"] is True and "version" in body


@pytest.mark.parametrize("header", [None, "Bearer wrong", "Basic abc", "Bearer "])
def test_other_routes_need_the_token(daemon: Daemon, header: str | None) -> None:
    headers = {"Authorization": header} if header else {}
    status, body, _ = daemon.call("GET", "/sessions", auth=False, headers=headers)
    assert status == 401 and "jeton" in body["error"]


# ---- CORS / host ----------------------------------------------------------------------


def test_foreign_host_is_refused(daemon: Daemon) -> None:
    status, _, _ = daemon.call("GET", "/health", headers={"Host": "evil.example:7341"})
    assert status == 421


def test_foreign_origin_is_refused(daemon: Daemon) -> None:
    status, _, resp = daemon.call(
        "GET", "/sessions", headers={"Origin": "https://evil.example"}
    )
    assert status == 403
    assert resp.getheader("Access-Control-Allow-Origin") is None


def test_tauri_origin_is_allowed(daemon: Daemon) -> None:
    status, _, resp = daemon.call("GET", "/sessions", headers={"Origin": TAURI})
    assert status == 200
    assert resp.getheader("Access-Control-Allow-Origin") == TAURI


def test_preflight(daemon: Daemon) -> None:
    status, _, resp = daemon.call(
        "OPTIONS", "/hook", auth=False, headers={"Origin": TAURI}
    )
    assert status == 204
    assert "Authorization" in resp.getheader("Access-Control-Allow-Headers", "")
    status, _, _ = daemon.call(
        "OPTIONS", "/hook", auth=False, headers={"Origin": "null"}
    )
    assert status == 403


# ---- request validation -----------------------------------------------------------


def test_body_limits_and_json_checks(daemon: Daemon) -> None:
    conn = daemon.conn()
    conn.putrequest("POST", "/hook")
    conn.putheader("Authorization", f"Bearer {daemon.token}")
    conn.putheader("Content-Type", "application/json")
    conn.putheader("Content-Length", str(srv.api.HOOK_BODY + 1))
    conn.endheaders()  # the body is never sent: the limit applies before reading
    assert conn.getresponse().status == 413
    conn.close()
    status, _, _ = daemon.call(
        "POST", "/hook", raw=b"{}", headers={"Content-Type": "text/plain"}
    )
    assert status == 415
    status, body, _ = daemon.call(
        "POST", "/hook", raw=b"{nope", headers={"Content-Type": "application/json"}
    )
    assert status == 400 and body["error"] == "JSON invalide"
    status, _, _ = daemon.call("POST", "/hook", [1, 2])
    assert status == 400


def test_chunked_bodies_are_refused(daemon: Daemon) -> None:
    status, _, _ = daemon.call(
        "POST",
        "/hook",
        raw=b"2\r\n{}\r\n0\r\n\r\n",
        headers={"Transfer-Encoding": "chunked", "Content-Type": "application/json"},
    )
    assert status == 411


@pytest.mark.parametrize(
    "path", ["/sessions/..", "/sessions/a%2Fb", "/sessions/%2e%2e"]
)
def test_bad_ids_are_refused(daemon: Daemon, path: str) -> None:
    status, _, _ = daemon.call("GET", path)
    assert status == 400


def test_unknown_routes_and_methods(daemon: Daemon) -> None:
    assert daemon.call("GET", "/../etc/passwd")[0] == 404
    assert daemon.call("GET", "/nope")[0] == 404
    assert daemon.call("POST", "/sessions", {})[0] == 405
    assert daemon.call("GET", "/sessions/claude:missing")[0] == 404


def test_hook_validates_source_event_and_translation(daemon: Daemon) -> None:
    assert daemon.call("POST", "/hook", {"source": "x", "event": {}})[0] == 400
    assert daemon.call("POST", "/hook", {"source": "claude", "event": "x"})[0] == 400
    status, body, _ = daemon.call(
        "POST", "/hook", {"source": "claude", "event": {"boom": 1}}
    )
    assert status == 400 and "illisible" in body["error"]


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
    pid = reply["pending_id"]
    assert reply["wait_s"] == 100
    _, listed, _ = daemon.call("GET", "/sessions")
    assert listed["sessions"][0]["state"] == "waiting"
    assert listed["sessions"][0]["pending"][0]["id"] == pid
    result: dict[str, Any] = {}

    def wait() -> None:
        result["value"] = daemon.call("GET", f"/pending/{pid}/wait?timeout=10")

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


def test_wait_times_out_as_expired(daemon: Daemon) -> None:
    pid = post_hook(daemon, ask=True)["pending_id"]
    status, body, _ = daemon.call("GET", f"/pending/{pid}/wait?timeout=0")
    assert status == 200 and body["status"] == "expired"
    assert daemon.call("GET", f"/pending/{pid}/wait?timeout=abc")[0] == 200  # closed
    assert daemon.call("GET", "/pending/nope/wait")[0] == 404


def test_wait_rejects_bad_timeout(daemon: Daemon) -> None:
    pid = post_hook(daemon, ask=True)["pending_id"]
    assert daemon.call("GET", f"/pending/{pid}/wait?timeout=-1")[0] == 400


def test_answer_is_validated_against_the_kind(daemon: Daemon) -> None:
    pid = post_hook(daemon, ask=True)["pending_id"]
    assert daemon.call("POST", f"/pending/{pid}/answer", {"decision": "yes"})[0] == 400
    assert daemon.call("POST", f"/pending/{pid}/answer", {"choice": "stop"})[0] == 400
    status, _, _ = daemon.call(
        "POST", f"/pending/{pid}/answer", {"decision": "deny", "message": "x" * 2000}
    )
    assert status == 400


def open_direct(daemon: Daemon, source: str, kind: str, **fields: Any) -> Pending:
    store = daemon.server.app.store
    now = utcnow()
    store.upsert_session(
        Session(f"{source}:p1", source, "p1", "pane", None, "working", now, now)  # type: ignore[arg-type]
    )
    return store.open_pending(Pending.new(f"{source}:p1", kind, **fields))  # type: ignore[arg-type]


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
    conn.request("GET", "/stream", headers={"Authorization": f"Bearer {daemon.token}"})
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
