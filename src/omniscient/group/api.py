"""omnigroup HTTP routes: validation and store calls, independent of the socket layer.

Each handler takes (app, ids, query, body) and returns (status, JSON object). The server
module owns transport concerns (auth, CORS, body limits); this one owns the API contract.
"""

from __future__ import annotations

import importlib
import json
import logging
import queue
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from importlib import metadata
from typing import TYPE_CHECKING, Any

from omniscient.group.models import ModelError, Pending, Session, to_json
from omniscient.group.store import StoreError

if TYPE_CHECKING:
    from omniscient.group.server import App

log = logging.getLogger(__name__)

HOOK_SOURCES = ("claude", "copilot")
COLLECTED_SOURCES = ("herdr", "agentrun")
MAX_WAIT_S = 120
DEFAULT_WAIT_S = 60
MAX_TEXT = 1000
MAX_ANSWER = 4000  # same bound as the widget (free-text answers to AgentRun runs)
MAX_PROMPT = 8000
MAX_ANSWERS = 20
PERMISSION_DECISIONS = ("allow", "deny", "always")
BUDGET_CHOICES = ("raise", "stop")
SMALL_BODY = 64 * 1024
HOOK_BODY = 1024 * 1024
STREAM_TICK_S = 0.5

Handler = Callable[["App", dict[str, str], dict[str, str], dict[str, Any]], tuple[int, dict[str, Any]]]  # fmt: skip


class ApiError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


@dataclass(frozen=True)
class Route:
    name: str
    method: str
    pattern: re.Pattern[str]
    handler: Handler
    max_body: int = SMALL_BODY


def match(method: str, path: str) -> tuple[Route, dict[str, str]]:
    """The route for (method, path) with its raw path parameters; 404/405 otherwise."""
    found_path = False
    for route in ROUTES:
        m = route.pattern.match(path)
        if m is None:
            continue
        found_path = True
        if route.method == method:
            return route, m.groupdict()
    raise ApiError(405 if found_path else 404, "route inconnue")


# ---- reads --------------------------------------------------------------------------


def version() -> str:
    try:
        return metadata.version("omniscient")
    except metadata.PackageNotFoundError:
        return "0.0.0"


def health(app: App, ids: dict, query: dict, body: dict) -> tuple[int, dict]:
    return 200, {"ok": True, "version": version()}


def _with_pending(app: App, session: Session) -> dict[str, Any]:
    data = to_json(session)
    data["pending"] = [to_json(p) for p in app.store.pending_for(session.id)]
    return data


def list_sessions(app: App, ids: dict, query: dict, body: dict) -> tuple[int, dict]:
    everything = query.get("dismissed") == "1"
    sessions = app.store.list_sessions(include_dismissed=everything)
    return 200, {"sessions": [_with_pending(app, s) for s in sessions]}


def _session(app: App, sid: str) -> Session:
    session = app.store.get_session(sid)
    if session is None:
        raise ApiError(404, "session inconnue")
    return session


def show_session(app: App, ids: dict, query: dict, body: dict) -> tuple[int, dict]:
    session = _session(app, ids["id"])
    limit = _int(query.get("limit"), 200, 1, 1000, "limit")
    data = _with_pending(app, session)
    data["steps"] = [to_json(s) for s in app.store.steps(session.id, limit)]
    return 200, data


USAGE_SUMS = ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens")  # fmt: skip


def usage_totals(app: App, ids: dict, query: dict, body: dict) -> tuple[int, dict]:
    """Token and cost totals across the visible sessions (for the widget's pill)."""
    usages = [s.usage for s in app.store.list_sessions() if s.usage is not None]
    totals: dict[str, Any] = {k: sum(getattr(u, k) for u in usages) for k in USAGE_SUMS}
    costs = [u.cost_usd for u in usages if u.cost_usd is not None]
    plans = [u for u in usages if u.plan_used_pct is not None]
    latest_plan = max(plans, key=lambda u: u.updated_at, default=None)
    totals.update(
        sessions=len(usages),
        cost_usd=round(sum(costs), 4) if costs else None,
        plan_used_pct=latest_plan.plan_used_pct if latest_plan else None,
        plan_resets_at=to_json(latest_plan)["plan_resets_at"] if latest_plan else None,
    )
    return 200, totals


def theme(app: App, ids: dict, query: dict, body: dict) -> tuple[int, dict]:
    from omniscient.theme import load_palette

    palette, warning = load_palette(app.home)
    return 200, {
        "name": palette.name,
        "label": palette.label,
        "dark": palette.dark,
        "colors": palette.colors(),
        "ansi": list(palette.ansi),
        "warning": warning,
    }


def _int(raw: str | None, default: int, low: int, high: int, name: str) -> int:
    if raw is None:
        return default
    if not raw.isdigit():
        raise ApiError(400, f"{name} doit être un entier")
    return max(low, min(int(raw), high))


# ---- hooks and pendings -------------------------------------------------------------


def hook(app: App, ids: dict, query: dict, body: dict) -> tuple[int, dict]:
    source, event = body.get("source"), body.get("event")
    if source not in HOOK_SOURCES:
        raise ApiError(400, "source doit être claude ou copilot")
    if not isinstance(event, dict):
        raise ApiError(400, "event doit être un objet JSON")
    usage = importlib.import_module("omniscient.group.hook_usage")
    if usage.is_status_event(event):  # a statusLine refresh: usage only
        _track_usage(app, source, event, _hooks().session_key(source, event))
        return 200, {"pending_id": None, "wait_s": None}
    translate = app.translate or _hooks().translate
    try:
        tr = translate(source, event)
    except (ModelError, ValueError, KeyError, TypeError) as exc:
        raise ApiError(400, f"événement refusé : {exc}") from exc
    app.store.upsert_session(tr.session)
    if tr.step is not None:
        app.store.add_step(tr.step)
    _track_usage(app, source, event, tr.session.id)
    if tr.pending is None:
        return 200, {"pending_id": None, "wait_s": None}
    if tr.pending.session_id != tr.session.id:
        raise ApiError(400, "pending d'une autre session")
    app.store.open_pending(tr.pending)
    return 200, {"pending_id": tr.pending.id, "wait_s": tr.wait_s}


def _hooks() -> Any:
    return importlib.import_module("omniscient.group.hooks")


def _track_usage(app: App, source: str, event: dict[str, Any], sid: str) -> None:
    """Fold the event into the session's usage cursor; store the usage when known."""
    usage_mod = importlib.import_module("omniscient.group.hook_usage")
    with app.usage_lock:
        usage, cursor = usage_mod.translate_usage(
            source, event, app.usage_cursors.get(sid)
        )
        if event.get("hook_event_name") == "SessionEnd":
            app.usage_cursors.pop(sid, None)
        else:
            app.usage_cursors[sid] = cursor
    if usage is None or app.store.get_session(sid) is None:
        return  # nothing new, or a statusLine for a session no hook reported yet
    app.store.update_usage(sid, usage)


def _pending(app: App, pid: str) -> Pending:
    found = app.store.get_pending(pid)
    if found is None:
        raise ApiError(404, "demande inconnue")
    return found


def wait_pending(app: App, ids: dict, query: dict, body: dict) -> tuple[int, dict]:
    pending = _pending(app, ids["id"])
    if not pending.is_open:
        return 200, to_json(pending)
    timeout = _int(query.get("timeout"), DEFAULT_WAIT_S, 0, MAX_WAIT_S, "timeout")
    return 200, to_json(app.store.wait_for_answer(pending.id, timeout))


def _text(value: Any, name: str, limit: int = MAX_TEXT) -> str:
    if not isinstance(value, str) or len(value) > limit:
        raise ApiError(400, f"{name} : texte de {limit} caractères au plus attendu")
    return value


def _answer_text(value: Any) -> str:
    """One answer: multi-select labels arrive already joined with ", " (as the widget does)."""
    text = _text(value, "réponse", MAX_ANSWER)
    if not text.strip():
        raise ApiError(400, "réponse vide")
    return text


def validate_answer(pending: Pending, body: dict[str, Any]) -> dict[str, Any]:
    """The answer body checked against the pending's kind (and its options)."""
    if pending.kind == "permission":
        allowed = pending.options or PERMISSION_DECISIONS
        if body.get("decision") not in allowed:
            raise ApiError(400, f"decision doit être parmi : {', '.join(allowed)}")
        answer: dict[str, Any] = {"decision": body["decision"]}
        if "message" in body:
            answer["message"] = _text(body["message"], "message")
        return answer
    if pending.kind == "question":
        answers = body.get("answers")
        if not isinstance(answers, dict) or not 0 < len(answers) <= MAX_ANSWERS:
            raise ApiError(400, "answers doit associer chaque question à une réponse")
        return {"answers": {_text(q, "question"): _answer_text(a) for q, a in answers.items()}}  # fmt: skip
    if pending.kind == "budget":
        if body.get("choice") not in BUDGET_CHOICES:
            raise ApiError(400, "choice doit être raise ou stop")
        return {"choice": body["choice"]}
    raise ApiError(
        409, "Cette approbation se fait dans la console : utilise « Revenir »."
    )


def answer_pending(app: App, ids: dict, query: dict, body: dict) -> tuple[int, dict]:
    pending = _pending(app, ids["id"])
    if not pending.is_open:
        raise ApiError(409, f"demande déjà {pending.status}")
    answer = validate_answer(pending, body)
    session = _session(app, pending.session_id)
    message = "Réponse envoyée."
    if session.source in COLLECTED_SOURCES:
        ok, message = _ask_collector(app, session.source, pending, answer)
        if not ok:
            return 502, {"ok": False, "message": message}
    try:
        resolved = app.store.resolve_pending(pending.id, answer, "answered")
    except StoreError as exc:
        raise ApiError(409, "demande déjà résolue") from exc
    return 200, {"ok": True, "message": message, "pending": to_json(resolved)}


def _ask_collector(
    app: App, source: str, pending: Pending, answer: dict[str, Any]
) -> tuple[bool, str]:
    collector = next((c for c in app.collectors if c.name == source), None)
    if collector is None:
        raise ApiError(503, f"collecteur {source} inactif")
    try:
        ok, message = collector.answer(pending, answer)
    except Exception:
        log.exception("omnigroup: collector %s failed to answer", source)
        return False, f"Le collecteur {source} n'a pas pu transmettre la réponse."
    return bool(ok), str(message)


# ---- session actions ------------------------------------------------------------------


def dismiss(app: App, ids: dict, query: dict, body: dict) -> tuple[int, dict]:
    _session(app, ids["id"])
    return 200, {"ok": True, "session": to_json(app.store.dismiss(ids["id"]))}


def _actions(app: App) -> Any:
    return app.actions or importlib.import_module("omniscient.group.actions")


def _result(value: Any) -> dict[str, Any]:
    """Normalise an action's result ((ok, message), dict or object) to the API shape."""
    if isinstance(value, tuple) and len(value) == 2:
        ok, message = value
    elif isinstance(value, dict):
        ok, message = value.get("ok"), value.get("message", "")
    else:
        ok, message = value.ok, getattr(value, "message", "")
    return {"ok": bool(ok), "message": str(message)}


def jump(app: App, ids: dict, query: dict, body: dict) -> tuple[int, dict]:
    session = _session(app, ids["id"])
    return 200, _result(_actions(app).jump(session))


def prompt(app: App, ids: dict, query: dict, body: dict) -> tuple[int, dict]:
    session = _session(app, ids["id"])
    text = _text(body.get("text"), "text", MAX_PROMPT).strip()
    if not text:
        raise ApiError(400, "text ne doit pas être vide")
    return 200, _result(_actions(app).prompt(session, text))


# ---- stream ---------------------------------------------------------------------------


def stream(app: App, ids: dict, query: dict, body: dict) -> tuple[int, dict]:
    raise AssertionError("the stream route is served by the handler itself")


def sse_event(event: dict[str, Any]) -> str:
    data = json.dumps(event["data"], ensure_ascii=False)  # one line: newlines escaped
    return f"event: {event['type']}\ndata: {data}\n\n"


def pump_events(
    app: App, q: queue.Queue[dict[str, Any]], write: Callable[[str], None]
) -> None:
    """Write store events as SSE, with a heartbeat comment, until stop or drop."""
    last_beat = time.monotonic()
    while not app.stopping.is_set():
        try:
            write(sse_event(q.get(timeout=STREAM_TICK_S)))
            continue
        except queue.Empty:
            pass
        if not app.store.is_subscribed(q):
            return  # dropped as a stalled reader
        if time.monotonic() - last_beat >= app.heartbeat_s:
            write(": heartbeat\n\n")
            last_beat = time.monotonic()


_SEG = r"(?P<id>[^/]+)"
ROUTES: tuple[Route, ...] = tuple(
    Route(name, method, re.compile(f"^{path}$"), handler, limit)
    for name, method, path, handler, limit in (
        ("health", "GET", "/health", health, SMALL_BODY),
        ("sessions", "GET", "/sessions", list_sessions, SMALL_BODY),
        ("session", "GET", f"/sessions/{_SEG}", show_session, SMALL_BODY),
        ("stream", "GET", "/stream", stream, SMALL_BODY),
        ("hook", "POST", "/hook", hook, HOOK_BODY),
        ("wait", "GET", f"/pending/{_SEG}/wait", wait_pending, SMALL_BODY),
        ("answer", "POST", f"/pending/{_SEG}/answer", answer_pending, SMALL_BODY),
        ("dismiss", "POST", f"/sessions/{_SEG}/dismiss", dismiss, SMALL_BODY),
        ("jump", "POST", f"/sessions/{_SEG}/jump", jump, SMALL_BODY),
        ("prompt", "POST", f"/sessions/{_SEG}/prompt", prompt, SMALL_BODY),
        ("theme", "GET", "/theme", theme, SMALL_BODY),
        ("usage", "GET", "/usage", usage_totals, SMALL_BODY),
    )
)
