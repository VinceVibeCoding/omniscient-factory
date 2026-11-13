"""Shared fakes and a real-daemon harness for the omnigroup server tests."""

from __future__ import annotations

import http.client
import json
import threading
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from omniscient.group import server as srv
from omniscient.group.models import Pending, Session, Step, utcnow
from omniscient.home import Home

TAURI = "http://tauri.localhost"
HOOK_PATHS = frozenset({"/hook", "/pair/start-terminal"})


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
    hook_token: str
    widget_token: str
    herdr: FakeCollector
    actions: FakeActions
    shown_codes: list[str] = field(default_factory=list)

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
        auth: bool | str = True,
        headers: dict[str, str] | None = None,
        raw: bytes | None = None,
    ) -> tuple[int, dict[str, Any], http.client.HTTPResponse]:
        """`auth`: True picks the credential the route takes (hook for /hook, the
        widget token otherwise), "hook" / "widget" force one, False sends none."""
        hdrs = dict(headers or {})
        if auth is True:
            auth = "hook" if path.split("?")[0] in HOOK_PATHS else "widget"
        if auth:
            token = self.hook_token if auth == "hook" else self.widget_token
            hdrs.setdefault("Authorization", f"Bearer {token}")
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

    def wait(
        self, pid: str, ticket: str | None, timeout: str = "10"
    ) -> tuple[int, dict[str, Any], http.client.HTTPResponse]:
        headers = {"X-Omnigroup-Ticket": ticket} if ticket else {}
        return self.call(
            "GET",
            f"/pending/{pid}/wait?timeout={timeout}",
            auth="hook",
            headers=headers,
        )


def start_daemon(tmp_path: Path, **options: Any) -> Daemon:
    """A real daemon on an ephemeral port, one paired widget, a widget "connected"."""
    herdr = FakeCollector("herdr")
    actions = FakeActions()
    shown: list[str] = []
    windows_ok = options.pop("windows_ok", True)

    def show_code(code: str) -> bool:
        shown.append(code)
        return windows_ok

    server = srv.create_server(
        Home(tmp_path),
        port=0,
        collectors=[herdr],
        translate=fake_translate,
        actions=actions,
        heartbeat_s=0.2,
        show_code=show_code,
        **options,
    )
    server.start_background()
    assert herdr.ran.wait(2)
    server.app.presence.touch()  # as if a widget had just been connected
    widget = server.app.widgets.mint("test")
    return Daemon(server, server.app.hook_token, widget, herdr, actions, shown)


@pytest.fixture
def daemon(tmp_path: Path) -> Iterator[Daemon]:
    d = start_daemon(tmp_path)
    yield d
    d.server.close_all()


def post_hook(d: Daemon, native: str = "s1", **event: Any) -> dict[str, Any]:
    status, body, _ = d.call(
        "POST", "/hook", {"source": "claude", "event": {"session_id": native, **event}}
    )
    assert status == 200, body
    return body


def open_direct(daemon: Daemon, source: str, kind: str, **fields: Any) -> Pending:
    store = daemon.server.app.store
    now = utcnow()
    store.upsert_session(
        Session(f"{source}:p1", source, "p1", "pane", None, "working", now, now)  # type: ignore[arg-type]
    )
    return store.open_pending(Pending.new(f"{source}:p1", kind, **fields))  # type: ignore[arg-type]
