"""Widget routes for the OSD: GET /osd, GET /config, PUT /config (widget token).

They read `app.osd` (an osd.OsdState created by the daemon). PUT /config publishes the
new effective view as an SSE `config` event so every connected widget follows at once.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from omniscient.group.config_store import ConfigError, update

if TYPE_CHECKING:
    from omniscient.group.server import App


def _api_error(status: int, message: str) -> Exception:
    from omniscient.group.api import ApiError  # api imports this module

    return ApiError(status, message)


def _osd(app: App) -> Any:
    state = getattr(app, "osd", None)
    if state is None:
        raise _api_error(503, "flux OSD indisponible")
    return state


def publish(app: App, kind: str, data: dict[str, Any]) -> None:
    store = app.store
    sender = getattr(store, "publish", None) or store._publish
    sender(kind, data)


def get_osd(app: App, ids: dict, query: dict, body: dict) -> tuple[int, dict]:
    return 200, _osd(app).snapshot()


def get_config(app: App, ids: dict, query: dict, body: dict) -> tuple[int, dict]:
    return 200, _osd(app).config_view()


def put_config(app: App, ids: dict, query: dict, body: dict) -> tuple[int, dict]:
    state = _osd(app)
    try:
        view = update(state.paths, body)
    except ConfigError as exc:
        raise _api_error(exc.status, exc.message) from exc
    publish(app, "config", view)
    return 200, view
