"""Pairing routes (see pairing.py): /pair/start, /pair/start-terminal, /pair/finish."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from omniscient.group.pairing import CODE_TTL_S, TERMINAL_HINT, PairError, valid_code

if TYPE_CHECKING:
    from omniscient.group.server import App

CLIENT_NAME_MAX = 64
DEFAULT_CLIENT = "widget"


def _api_error(exc: PairError) -> Exception:
    from omniscient.group.api import ApiError  # api imports this module

    return ApiError(exc.status, exc.message)


def start(app: App, ids: dict, query: dict, body: dict) -> tuple[int, dict]:
    """A code shown only on the Windows desktop; refused when Windows can't show it."""
    try:
        code = app.pairing.start()
    except PairError as exc:
        raise _api_error(exc) from exc
    if not app.show_code(code):
        app.pairing.cancel()
        raise _api_error(PairError(503, TERMINAL_HINT))
    return 200, {
        "ok": True,
        "message": "Code affiché sur le bureau Windows : tape-le dans le widget.",
        "expires_in": int(CODE_TTL_S),
    }


def start_terminal(app: App, ids: dict, query: dict, body: dict) -> tuple[int, dict]:
    """`omniscient group pair --show-code`: the code comes back to the terminal. Off
    unless the daemon runs with --allow-terminal-pairing (an agent with the hook
    credential could pair itself: weaker, documented)."""
    if not app.allow_terminal_pairing:
        raise _api_error(
            PairError(
                403,
                "Couplage par terminal désactivé : relance le démon avec "
                "--allow-terminal-pairing.",
            )
        )
    try:
        code = app.pairing.start()
    except PairError as exc:
        raise _api_error(exc) from exc
    return 200, {"code": code, "expires_in": int(CODE_TTL_S)}


def _client_name(raw: Any) -> str:
    if raw is None:
        return DEFAULT_CLIENT
    if (
        not isinstance(raw, str)
        or not raw.strip()
        or len(raw) > CLIENT_NAME_MAX
        or not raw.isprintable()
    ):
        raise _api_error(PairError(400, "client_name : 64 caractères imprimables max"))
    return raw.strip()


def finish(app: App, ids: dict, query: dict, body: dict) -> tuple[int, dict]:
    """The right code (five tries) mints a widget token, returned this once only."""
    code = body.get("code")
    if not valid_code(code):
        raise _api_error(PairError(400, "code : six chiffres attendus"))
    name = _client_name(body.get("client_name"))
    try:
        app.pairing.finish(code)  # type: ignore[arg-type]
    except PairError as exc:
        raise _api_error(exc) from exc
    return 200, {"ok": True, "token": app.widgets.mint(name)}
