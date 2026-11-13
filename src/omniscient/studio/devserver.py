"""A tiny live-reload server for a prototype's web/ folder (the studio UI pane).

Bound to 127.0.0.1 only. Serves regular files under web_dir and nothing else: every
request path is resolved and must stay inside web_dir (no `..`, no symlink escape, no
directory listing). HTML pages get a script that polls `/__omniscient_version` (the
newest mtime under web_dir) every second and reloads the page when it changes.
"""

from __future__ import annotations

import errno
import hashlib
import mimetypes
import shutil
import subprocess
import threading
from collections.abc import Callable
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import ClassVar
from urllib.parse import unquote, urlsplit

HOST = "127.0.0.1"
BASE_PORT = 8700
PORT_SPAN = 200
VERSION_PATH = "/__omniscient_version"
INDEX = "index.html"
RELOAD_SCRIPT = (
    "<script>(()=>{let v=null;setInterval(async()=>{try{"
    f"const r=await fetch('{VERSION_PATH}',{{cache:'no-store'}});"
    "const t=await r.text();if(v!==null&&t!==v)location.reload();v=t;"
    "}catch(e){}},1000);})();</script>"
).encode()


class DevServerError(Exception):
    """The server could not start (bad folder, no port)."""


def default_port(stem: str) -> int:
    """A stable per-idea port in 8700..8899, so the browser tab survives restarts."""
    digest = hashlib.sha256(stem.encode("utf-8")).hexdigest()
    return BASE_PORT + int(digest[:8], 16) % PORT_SPAN


def site_version(web_dir: Path) -> str:
    """Newest mtime (ns) of the regular files under web_dir, as text."""
    newest = 0
    for path in web_dir.rglob("*"):
        if path.is_symlink() or not path.is_file():
            continue
        newest = max(newest, path.stat().st_mtime_ns)
    return str(newest)


def inject_reload(html: bytes) -> bytes:
    """Put the reload script before </body> (or at the end when there is none)."""
    lower = html.lower()
    at = lower.rfind(b"</body>")
    if at < 0:
        return html + RELOAD_SCRIPT
    return html[:at] + RELOAD_SCRIPT + html[at:]


def resolve_request(web_dir: Path, raw_path: str) -> Path | None:
    """The file a URL path names inside web_dir, or None (outside, missing, not a file)."""
    root = web_dir.resolve()
    rel = unquote(urlsplit(raw_path).path).lstrip("/")
    if "\x00" in rel or "\\" in rel:
        return None
    try:
        target = (root / rel).resolve()
    except (OSError, ValueError):
        return None
    if target != root and root not in target.parents:
        return None
    if target.is_dir():
        target = target / INDEX
    return target if target.is_file() else None


class _Handler(BaseHTTPRequestHandler):
    web_dir: ClassVar[Path]
    server_version = "omniscient-dev"
    sys_version = ""

    def do_GET(self) -> None:
        self._serve(send_body=True)

    def do_HEAD(self) -> None:
        self._serve(send_body=False)

    def _serve(self, send_body: bool) -> None:
        if urlsplit(self.path).path == VERSION_PATH:
            self._reply(site_version(self.web_dir).encode(), "text/plain", send_body)
            return
        target = resolve_request(self.web_dir, self.path)
        try:
            body = target.read_bytes() if target is not None else None
        except (
            OSError
        ):  # vanished or unreadable mid-request (the builder rewrites files)
            body = None
        if target is None or body is None:
            self._reply(b"introuvable", "text/plain", send_body, HTTPStatus.NOT_FOUND)
            return
        kind = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        if kind == "text/html":
            body = inject_reload(body)
        self._reply(body, kind, send_body)

    def _reply(
        self,
        body: bytes,
        kind: str,
        send_body: bool,
        status: HTTPStatus = HTTPStatus.OK,
    ) -> None:
        self.send_response(status)
        charset = "; charset=utf-8" if kind.startswith("text/") else ""
        self.send_header("Content-Type", kind + charset)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if send_body:
            self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        """Silent: stderr would scribble over the Textual pane."""


class DevServer:
    """Serve web_dir on 127.0.0.1:port (or a free port when that one is taken)."""

    def __init__(self, web_dir: Path, port: int | None = None) -> None:
        if not web_dir.is_dir():
            raise DevServerError(f"pas de dossier web : {web_dir}")
        self.web_dir = web_dir.resolve()
        self.port = port
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def url(self) -> str:
        if self._server is None:
            raise DevServerError("le serveur n'est pas démarré")
        return f"http://localhost:{self._server.server_address[1]}/"

    @property
    def running(self) -> bool:
        return self._server is not None

    def start(self) -> str:
        """Start serving in a daemon thread; returns the URL. Idempotent."""
        if self._server is not None:
            return self.url
        handler = type("Handler", (_Handler,), {"web_dir": self.web_dir})
        self._server = _bind(handler, self.port)
        self._server.daemon_threads = True
        self._thread = threading.Thread(
            target=self._server.serve_forever, name="omniscient-devserver", daemon=True
        )
        self._thread.start()
        return self.url

    def stop(self) -> None:
        if self._server is None:
            return
        self._server.shutdown()
        self._server.server_close()
        self._server = None
        if self._thread is not None:
            self._thread.join(timeout=2)
            self._thread = None


def _bind(handler: type[_Handler], port: int | None) -> ThreadingHTTPServer:
    """Bind the wanted port; when it is in use, fall back to a free one."""
    if port is not None:
        try:
            return ThreadingHTTPServer((HOST, port), handler)
        except OSError as exc:
            if exc.errno != errno.EADDRINUSE:
                raise DevServerError(f"port {port} : {exc}") from exc
    try:
        return ThreadingHTTPServer((HOST, 0), handler)
    except OSError as exc:
        raise DevServerError(f"aucun port libre : {exc}") from exc


def open_in_browser(
    url: str,
    which: Callable[[str], str | None] = shutil.which,
    spawn: Callable[[list[str]], object] | None = None,
) -> bool:
    """Open url in the Windows browser (WSL) or the desktop one; False when impossible."""
    run = spawn or _spawn_detached
    for opener in ("explorer.exe", "xdg-open"):
        exe = which(opener)
        if exe is None:
            continue
        try:
            run([exe, url])
        except OSError:
            continue
        return True
    return False


def _spawn_detached(argv: list[str]) -> object:
    return subprocess.Popen(
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
