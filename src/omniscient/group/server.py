"""omnigroup daemon: a local HTTP + SSE API over the store (stdlib only).

Security: binds 127.0.0.1 only; every route but GET /health needs
`Authorization: Bearer <token>` (constant-time compare); the Host header must be a
loopback name (DNS rebinding); browser origins other than the Tauri widget are refused;
bodies are size-limited JSON objects; route ids are validated (no slashes, no dots-only).
"""

from __future__ import annotations

import hmac
import json
import logging
import os
import re
import secrets
import stat
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import parse_qs, unquote, urlsplit

from omniscient.group import api
from omniscient.group.api import ApiError
from omniscient.group.store import Store
from omniscient.home import Home

log = logging.getLogger(__name__)

BIND_HOST = "127.0.0.1"
DEFAULT_PORT = 7341
TOKEN_FILE = "group.token"
DB_FILE = "group.db"
TOKEN_MIN_LEN = 32
ALLOWED_ORIGINS = frozenset(
    {"http://tauri.localhost", "https://tauri.localhost", "tauri://localhost"}
)
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "[::1]"})
SOCKET_TIMEOUT_S = 30
HEARTBEAT_S = 15.0
JANITOR_S = 5.0
PRUNE_EVERY_S = 3600.0
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
    token: str
    collectors: tuple[Collector, ...] = ()
    translate: Callable[[str, dict[str, Any]], Any] | None = None
    actions: Any = None
    heartbeat_s: float = HEARTBEAT_S
    stopping: threading.Event = field(default_factory=threading.Event)
    # One hook_usage.UsageCursor per session (daemon memory only), under usage_lock.
    usage_cursors: dict[str, Any] = field(default_factory=dict)
    usage_lock: threading.Lock = field(default_factory=threading.Lock)


# ---- token ---------------------------------------------------------------------------


def token_path(home: Home) -> Path:
    return home.state / TOKEN_FILE


def db_path(home: Home) -> Path:
    return home.state / DB_FILE


def read_token(home: Home) -> str | None:
    """The bearer token, or None when the daemon never ran in this home."""
    path = token_path(home)
    try:
        token = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return None
    return token or None


def load_or_create_token(home: Home) -> str:
    """The token (created 0600 with secrets.token_urlsafe(32) on first use)."""
    path = token_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        mode = stat.S_IMODE(path.stat().st_mode)
        if mode & 0o077:
            log.warning("omnigroup: token file was mode %o, tightening to 600", mode)
            path.chmod(0o600)
        token = read_token(home)
        if token is None or len(token) < TOKEN_MIN_LEN:
            raise RuntimeError(f"jeton omnigroup invalide : supprime {path} et relance")
        return token
    token = secrets.token_urlsafe(32)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(token + "\n")
    return token


# ---- HTTP ------------------------------------------------------------------------------


class GroupServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, app: App, port: int) -> None:
        self.app = app
        self._workers: list[threading.Thread] = []
        super().__init__((BIND_HOST, port), Handler)

    @property
    def port(self) -> int:
        return int(self.server_address[1])

    def start_background(self) -> None:
        """Collectors, the janitor and the HTTP loop, each on a daemon thread."""
        for collector in self.app.collectors:
            self._spawn(
                f"collector-{collector.name}", _run_collector, collector, self.app
            )
        self._spawn("janitor", _janitor, self.app)
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


def _run_collector(collector: Collector, app: App) -> None:
    try:
        collector.run(app.store, app.stopping)
    except Exception:
        log.exception("omnigroup collector %s stopped", collector.name)


def _janitor(app: App) -> None:
    """Expire pendings whose hook gave up; prune old sessions at start, then hourly."""
    since_prune = PRUNE_EVERY_S
    while not app.stopping.is_set():
        try:
            app.store.expire_overdue()
            if since_prune >= PRUNE_EVERY_S:
                app.store.prune()
                since_prune = 0.0
        except Exception:
            log.exception("omnigroup janitor failed")
        app.stopping.wait(JANITOR_S)
        since_prune += JANITOR_S


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
            if route.name != "health":
                self._check_auth()
            if route.name == "stream":
                self._stream()
                return
            query = {k: v[-1] for k, v in parse_qs(url.query).items()}
            ids = {k: _route_id(v) for k, v in params.items()}
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

    def _check_auth(self) -> None:
        header = self.headers.get("Authorization") or ""
        scheme, _, given = header.partition(" ")
        expected = self.server.app.token.encode()
        if scheme.lower() != "bearer" or not hmac.compare_digest(
            given.strip().encode(), expected
        ):
            raise ApiError(401, "jeton manquant ou invalide")

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
        q = app.store.subscribe()
        try:
            self.send_response(200)
            self._common_headers("text/event-stream; charset=utf-8")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            self._write(": connecté\n\n")
            api.pump_events(app, q, self._write)
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            log.debug("omnigroup: stream client went away")
        finally:
            app.store.unsubscribe(q)
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


def default_collectors(home: Home) -> tuple[Collector, ...]:
    """herdr + AgentRun collectors (agent C's modules, imported lazily)."""
    from omniscient.group.collect_agentrun import AgentRunCollector
    from omniscient.group.collect_herdr import HerdrCollector

    return (HerdrCollector(), AgentRunCollector.for_home(home))


def create_server(
    home: Home,
    port: int = DEFAULT_PORT,
    collectors: Sequence[Collector] = (),
    *,
    translate: Callable[[str, dict[str, Any]], Any] | None = None,
    actions: Any = None,
    heartbeat_s: float = HEARTBEAT_S,
) -> GroupServer:
    """A bound (not yet serving) daemon; port 0 picks a free port."""
    app = App(
        home=home,
        store=Store(db_path(home)),
        token=load_or_create_token(home),
        collectors=tuple(collectors),
        translate=translate,
        actions=actions,
        heartbeat_s=heartbeat_s,
    )
    return GroupServer(app, port)


def serve(
    home: Home,
    port: int = DEFAULT_PORT,
    collectors: Sequence[Collector] | None = None,
) -> None:
    """Run the daemon in the foreground until Ctrl-C (`omniscient group serve`)."""
    chosen = default_collectors(home) if collectors is None else tuple(collectors)
    server = create_server(home, port, chosen)
    server.start_background()
    print(
        f"omnigroup écoute sur http://{BIND_HOST}:{server.port} (Ctrl-C pour arrêter)"
    )
    try:
        server.app.stopping.wait()
    except KeyboardInterrupt:
        print("\nArrêt d'omnigroup.")
    finally:
        server.stop()
        server.app.store.close()
