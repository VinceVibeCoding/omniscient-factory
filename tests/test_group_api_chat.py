"""api_chat handlers: status codes and shapes (contract §4), against a fake App."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from omniscient.group import api_chat
from omniscient.group.api import ApiError
from omniscient.group.chat import ChatManager
from omniscient.group.chat_runner import RunnerEvent, TurnHandle, TurnOutcome, TurnSpec
from omniscient.group.models import Session, utcnow
from omniscient.home import Home

SID = "0b6c7c1e-1111-4222-8333-444455556666"


def runner(spec: TurnSpec, on_event: Any, handle: TurnHandle) -> TurnOutcome:
    on_event(RunnerEvent("text", text="ok"))
    return TurnOutcome(True, "sess-9", 0.01)


class FakeStore:
    def __init__(self, sessions: dict[str, Session]) -> None:
        self.sessions = sessions

    def get_session(self, sid: str) -> Session | None:
        return self.sessions.get(sid)


@pytest.fixture
def app(isolated_home: Path, tmp_path: Path) -> SimpleNamespace:
    home = Home(isolated_home)
    now = utcnow()
    claude = Session(
        id=f"claude:{SID}", source="claude", native_id=SID, title="t", cwd=None,
        state="idle", started_at=now, updated_at=now,
    )  # fmt: skip
    folder = tmp_path / "claude" / "projects" / "p"
    folder.mkdir(parents=True)
    (folder / f"{SID}.jsonl").write_text(
        json.dumps(
            {
                "type": "user",
                "timestamp": "2026-11-14T10:00:00Z",
                "message": {"content": "salut"},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    return SimpleNamespace(
        chat=ChatManager(home, lambda k, d: None, runner=runner),
        store=FakeStore({claude.id: claude}),
        usage_cursors={},
        claude_dir=tmp_path / "claude",
    )


def call(
    handler: Any,
    app: Any,
    ids: dict | None = None,
    query: dict | None = None,
    body: dict | None = None,
) -> tuple[int, dict]:
    return handler(app, ids or {}, query or {}, body or {})


def test_projects_and_full_chat_round_trip(app: SimpleNamespace) -> None:
    status, data = call(api_chat.list_projects, app)
    assert status == 200
    name = data["projects"][0]["name"]
    assert set(data["projects"][0]) == {"name", "path_label"}
    status, created = call(
        api_chat.create_chat, app, body={"project": name, "text": "hello"}
    )
    assert status == 202
    chat_id = created["chat_id"]
    app.chat.join(chat_id, 5)
    status, listed = call(api_chat.list_chats, app)
    assert listed["chats"][0]["chat_id"] == chat_id
    status, detail = call(api_chat.get_chat, app, {"id": chat_id})
    assert [m["role"] for m in detail["messages"]] == ["user", "assistant"]
    status, sent = call(
        api_chat.send_chat, app, {"id": chat_id}, body={"text": "encore"}
    )
    assert status == 202 and sent["ok"] is True
    app.chat.join(chat_id, 5)
    status, stopped = call(api_chat.stop_chat, app, {"id": chat_id})
    assert status == 200 and stopped["ok"] is False


def test_errors_map_to_api_errors(app: SimpleNamespace) -> None:
    with pytest.raises(ApiError) as missing:
        call(api_chat.create_chat, app, body={"project": "nope", "text": "x"})
    assert missing.value.status == 404
    with pytest.raises(ApiError) as bad:
        call(api_chat.get_chat, app, {"id": "not-a-uuid"})
    assert bad.value.status == 404
    no_chat = SimpleNamespace(chat=None)
    with pytest.raises(ApiError) as off:
        call(api_chat.list_chats, no_chat)
    assert off.value.status == 503


def test_session_transcript(app: SimpleNamespace) -> None:
    status, view = call(
        api_chat.session_transcript, app, {"id": f"claude:{SID}"}, {"limit": "5"}
    )
    assert status == 200
    assert view["messages"][0]["text"] == "salut" and view["can_prompt"] is False
    with pytest.raises(ApiError) as unknown:
        call(api_chat.session_transcript, app, {"id": "claude:other"})
    assert unknown.value.status == 404
    with pytest.raises(ApiError):
        call(
            api_chat.session_transcript, app, {"id": f"claude:{SID}"}, {"limit": "abc"}
        )
