"""Chat routes for the widget (contract §4), widget-token auth like every other route.

Mode A, the daemon's own headless chats: GET /projects, GET/POST /chats,
GET /chats/{id}, POST /chats/{id}/messages, POST /chats/{id}/stop.
Mode B, a session already running elsewhere: GET /sessions/{id}/transcript (sending
reuses POST /sessions/{id}/prompt). Handlers take (app, ids, query, body) like api.py;
`app.chat` is the daemon's ChatManager (None: chat disabled → 503).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from omniscient.group.chat import ChatError, ChatManager
from omniscient.group.transcript_view import session_transcript as read_transcript

if TYPE_CHECKING:
    from omniscient.group.server import App

TRANSCRIPT_DEFAULT = 50
TRANSCRIPT_MAX = 200


def _api_error(status: int, message: str) -> Exception:
    from omniscient.group.api import ApiError  # api imports this module

    return ApiError(status, message)


def _manager(app: App) -> ChatManager:
    manager = getattr(app, "chat", None)
    if manager is None:
        raise _api_error(503, "Chat désactivé sur ce démon.")
    return manager


def _guard(work: Any) -> Any:
    try:
        return work()
    except ChatError as exc:
        raise _api_error(exc.status, exc.message) from exc


def list_projects(app: App, ids: dict, query: dict, body: dict) -> tuple[int, dict]:
    refs = _manager(app).projects()
    return 200, {
        "projects": [{"name": r.name, "path_label": r.path_label} for r in refs]
    }


def list_chats(app: App, ids: dict, query: dict, body: dict) -> tuple[int, dict]:
    return 200, {"chats": _manager(app).list()}


def create_chat(app: App, ids: dict, query: dict, body: dict) -> tuple[int, dict]:
    manager = _manager(app)
    chat_id = _guard(lambda: manager.create(body.get("project"), body.get("text")))
    return 202, {"chat_id": chat_id}


def get_chat(app: App, ids: dict, query: dict, body: dict) -> tuple[int, dict]:
    manager = _manager(app)
    _, detail = _guard(lambda: manager.get(ids["id"]))
    return 200, detail


def send_chat(app: App, ids: dict, query: dict, body: dict) -> tuple[int, dict]:
    manager = _manager(app)
    _guard(lambda: manager.send(ids["id"], body.get("text")))
    return 202, {"ok": True}


def stop_chat(app: App, ids: dict, query: dict, body: dict) -> tuple[int, dict]:
    manager = _manager(app)
    stopped = _guard(lambda: manager.stop(ids["id"]))
    message = "Arrêt demandé." if stopped else "Aucun tour en cours."
    return 200, {"ok": bool(stopped), "message": message}


def _limit(raw: str | None) -> int:
    if raw is None:
        return TRANSCRIPT_DEFAULT
    if not raw.isdigit():
        raise _api_error(400, "limit doit être un entier")
    return max(1, min(int(raw), TRANSCRIPT_MAX))


def session_transcript(
    app: App, ids: dict, query: dict, body: dict
) -> tuple[int, dict]:
    session = app.store.get_session(ids["id"])
    if session is None:
        raise _api_error(404, "session inconnue")
    cursor = getattr(app, "usage_cursors", {}).get(session.id)
    known = getattr(cursor, "path", None) or None
    view = read_transcript(
        session,
        claude_dir=getattr(app, "claude_dir", None),
        limit=_limit(query.get("limit")),
        known_path=known,
    )
    return 200, view
