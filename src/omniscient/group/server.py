"""omnigroup daemon: a local HTTP + SSE API over the store (stdlib only).

Security: binds 127.0.0.1 only; each route names the credential it takes (api.Route.auth):
the hook credential only reaches POST /hook and, with its wait ticket, the wait on the
pending that call opened; a paired widget's token reaches the rest; presenting the
wrong one is a 403. The Host header must be a loopback name (DNS rebinding); browser
origins other than the Tauri widget are refused; bodies are size-limited JSON objects;
route ids are validated; at most MAX_CONNECTIONS requests and MAX_STREAMS streams at once.
"""

from __future__ import annotations

import json
import logging
import re
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Protocol
from urllib.parse import parse_qs, unquote, urlsplit

from omniscient.group import api, api_stream, workers
from omniscient.group.api import ApiError
from omniscient.group.credentials import (
    WidgetTokens,
    db_path,
    load_or_create_hook_token,
    same_secret,
    secure_dir,
)
from omniscient.group.gates import Presence, TicketBook
from omniscient.group.pairing import PairingDesk, show_code_on_windows
from omniscient.group.store import Store
from omniscient.home import Home

log = logging.getLogger(__name__)

BIND_HOST = "127.0.0.1"
DEFAULT_PORT = 7341
TICKET_HEADER = "X-Omnigroup-Ticket"
MAX_CONNECTIONS = 64
ALLOWED_ORIGINS = frozenset(
    {"http://tauri.localhost", "https://tauri.localhost", "tauri://localhost"}
)
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "[::1]"})
SOCKET_TIMEOUT_S = 30
HEARTBEAT_S = 15.0
SERVE_POLL_S = 0.1
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9:_.@-]{0,199}$")


class Collector(Protocol):
    name: str

    def run(self, store: Store, stop: threading.Event) -> None: ...

    def answer(self, pending: Any, answer: dict[str, Any]) -> tuple[bool, str]: ...


@dataclass(frozen=True)
class App:
    """Everything a request handler needs (fakes are injected in tests)."""

    home: Home
    store: Store
    hook_token: str
    widgets: WidgetTokens
    collectors: tuple[Collector, ...] = ()
    translate: Callable[[str, dict[str, Any]], Any] | None = None
    actions: Any = None
    heartbeat_s: float = HEARTBEAT_S
    stopping: threading.Event = field(default_factory=threading.Event)
    # One hook_usage.UsageCursor per session (daemon memory only), under usage_lock.
    usage_cursors: dict[str, Any] = field(default_factory=dict)
    usage_lock: threading.Lock = field(default_factory=threading.Lock)
    tickets: TicketBook = field(default_factory=TicketBook)
    presence: Presence = field(default_factory=Presence)
    pairing: PairingDesk = field(default_factory=PairingDesk)
    show_code: Callable[[str], bool] = show_code_on_windows
    allow_terminal_pairing: bool = False


# ---- HTTP ------------------------------------------------------------------------------


class GroupServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, app: App, port: int) -> None:
        self.app = app
        self._workers: list[threading.Thread] = []
        self._slots = threading.BoundedSemaphore(MAX_CONNECTIONS)
        super().__init__((BIND_HOST, port), Handler)

    def process_request(self, request: Any, client_address: Any) -> None:
        """At most MAX_CONNECTIONS requests at once; the next ones are closed (M10)."""
        if not self._slots.acquire(blocking=False):
            log.warning("omnigroup: too many connections, one refused")
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._slots.release()
            raise

    def process_request_thread(self, request: Any, client_address: Any) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()

    @property
    def port(self) -> int:
        return int(self.server_address[1])

    def start_background(self) -> None:
        """Collectors, the janitor and the HTTP loop, each on a daemon thread."""
        for collector in self.app.collectors:
            self._spawn(
                f"collector-{collector.name}",
                workers.run_collector,
                collector,
                self.app,
            )
        self._spawn("janitor", workers.janitor, self.app)
        self._spawn("http", self.serve_forever, SERVE_POLL_S)

    def _spawn(self, name: str, target: Callable[..., None], *args: Any) -> None:
        thread = threading.Thread(
            target=target, args=args, name=f"omnigroup-{name}", daemon=True
        )
        thread.start()
        self._workers.append(thread)

    def stop(self) -> None:
        self.app.stopping.set()
        self.shutdown()
        self.server_close()

    def close_all(self) -> None:
        """Stop serving, then close the databases."""
        self.stop()
        self.app.store.close()
        self.app.widgets.close()


class Handler(BaseHTTPRequestHandler):
    server: GroupServer
    timeout = SOCKET_TIMEOUT_S
    server_version = "omnigroup"
    sys_version = ""

    def log_message(self, format: str, *args: Any) -> None:
        log.debug("omnigroup %s - %s", self.address_string(), format % args)

    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_POST(self) -> None:
        self._dispatch("POST")

    def do_OPTIONS(self) -> None:
        try:
            self._guard()
        except ApiError as exc:
            self._send_json(exc.status, {"error": exc.message})
            return
        if self.headers.get("Origin") not in ALLOWED_ORIGINS:
            self._send_json(403, {"error": "origine refusée"})
            return
        self.send_response(204)
        self._cors_headers()
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type")
        self.send_header("Access-Control-Max-Age", "600")
        self.send_header("Content-Length", "0")
        self.end_headers()

    # ---- dispatch ----------------------------------------------------------------

    def _dispatch(self, method: str) -> None:
        try:
            self._guard()
            url = urlsplit(self.path)
            route, params = api.match(method, url.path)
            ids = {k: _route_id(v) for k, v in params.items()}
            self._authorize(route.auth, ids)
            if route.name == "stream":
                self._stream()
                return
            query = {k: v[-1] for k, v in parse_qs(url.query).items()}
            body = self._read_body(route.max_body) if method == "POST" else {}
            status, payload = route.handler(self.server.app, ids, query, body)
            self._send_json(status, payload)
        except ApiError as exc:
            self._send_json(exc.status, {"error": exc.message})
        except (BrokenPipeError, ConnectionResetError):
            log.debug("omnigroup: client went away")
        except Exception:
            log.exception("omnigroup: %s %s failed", method, self.path.split("?")[0])
            self._send_json(500, {"error": "erreur interne"})

    def _guard(self) -> None:
        host = (self.headers.get("Host") or "").strip().lower()
        name = host.rsplit(":", 1)[0] if not host.endswith("]") else host
        if name not in LOOPBACK_HOSTS:
            raise ApiError(421, "hôte refusé")
        origin = self.headers.get("Origin")
        if origin is not None and origin not in ALLOWED_ORIGINS:
            raise ApiError(403, "origine refusée")

    def _caller(self) -> str | None:
        """ "hook", "widget" or None, from `Authorization: Bearer <token>`."""
        header = self.headers.get("Authorization") or ""
        scheme, _, given = header.partition(" ")
        given = given.strip()
        if scheme.lower() != "bearer" or not given:
            return None
        app = self.server.app
        if same_secret(given, app.hook_token):
            return "hook"
        return "widget" if app.widgets.is_valid(given) else None

    def _authorize(self, auth: str, ids: dict[str, str]) -> None:
        if auth == "none":
            return
        caller = self._caller()
        if caller is None:
            raise ApiError(401, "jeton manquant ou invalide")
        wanted = "hook" if auth in ("hook", "ticket") else "widget"
        if caller != wanted:
            raise ApiError(403, "ce jeton ne donne pas accès à cette route")
        if auth == "ticket":
            ticket = (self.headers.get(TICKET_HEADER) or "").strip()
            if not self.server.app.tickets.redeem(ids.get("id", ""), ticket):
                raise ApiError(403, "ticket d'attente manquant ou invalide")

    def _read_body(self, limit: int) -> dict[str, Any]:
        if self.headers.get("Transfer-Encoding"):
            raise ApiError(411, "Content-Length requis")
        raw_length = self.headers.get("Content-Length") or "0"
        if not raw_length.isdigit():
            raise ApiError(400, "Content-Length invalide")
        length = int(raw_length)
        if length > limit:
            self.close_connection = True
            raise ApiError(413, "requête trop volumineuse")
        if length == 0:
            return {}
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if ctype != "application/json":
            raise ApiError(415, "Content-Type application/json requis")
        try:
            data = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ApiError(400, "JSON invalide") from exc
        if not isinstance(data, dict):
            raise ApiError(400, "le corps doit être un objet JSON")
        return data

    # ---- responses ---------------------------------------------------------------

    def _cors_headers(self) -> None:
        origin = self.headers.get("Origin")
        if origin in ALLOWED_ORIGINS:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")

    def _common_headers(self, ctype: str) -> None:
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self._cors_headers()

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        try:
            self.send_response(status)
            self._common_headers("application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            log.debug("omnigroup: client went away before the response")

    def _stream(self) -> None:
        """Server-sent events until the client leaves or the daemon stops."""
        app = self.server.app
        if not app.presence.enter():
            self._send_json(503, {"error": "trop de flux ouverts"})
            return
        q = app.store.subscribe()
        try:
            self.send_response(200)
            self._common_headers("text/event-stream; charset=utf-8")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            self._write(": connecté\n\n")
            api_stream.pump_events(app, q, self._write)
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            log.debug("omnigroup: stream client went away")
        finally:
            app.store.unsubscribe(q)
            app.presence.leave()
            self.close_connection = True

    def _write(self, text: str) -> None:
        self.wfile.write(text.encode("utf-8"))
        self.wfile.flush()


def _route_id(raw: str) -> str:
    value = unquote(raw)
    if not _ID.match(value) or set(value) <= {"."}:
        raise ApiError(400, "identifiant invalide")
    return value


# ---- entry points --------------------------------------------------------------------


def create_server(
    home: Home,
    port: int = DEFAULT_PORT,
    collectors: Sequence[Collector] = (),
    *,
    translate: Callable[[str, dict[str, Any]], Any] | None = None,
    actions: Any = None,
    heartbeat_s: float = HEARTBEAT_S,
    show_code: Callable[[str], bool] | None = None,
    allow_terminal_pairing: bool = False,
) -> GroupServer:
    """A bound (not yet serving) daemon; port 0 picks a free port."""
    secure_dir(home.state)
    app = App(
        home=home,
        hook_token=load_or_create_hook_token(home),
        store=Store(db_path(home)),
        widgets=WidgetTokens(db_path(home)),
        collectors=tuple(collectors),
        translate=translate,
        actions=actions,
        heartbeat_s=heartbeat_s,
        show_code=show_code or show_code_on_windows,
        allow_terminal_pairing=allow_terminal_pairing,
    )
    return GroupServer(app, port)


def serve(
    home: Home,
    port: int = DEFAULT_PORT,
    collectors: Sequence[Collector] | None = None,
    allow_terminal_pairing: bool = False,
) -> None:
    """Run the daemon in the foreground until Ctrl-C (`omniscient group serve`)."""
    chosen = (
        workers.default_collectors(home) if collectors is None else tuple(collectors)
    )
    server = create_server(
        home, port, chosen, allow_terminal_pairing=allow_terminal_pairing
    )
    server.start_background()
    print(
        f"omnigroup écoute sur http://{BIND_HOST}:{server.port} (Ctrl-C pour arrêter)"
    )
    try:
        server.app.stopping.wait()
    except KeyboardInterrupt:
        print("\nArrêt d'omnigroup.")
    finally:
        server.close_all()
