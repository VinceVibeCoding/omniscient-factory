"""omnigroup HTTP daemon: tokens, binding, CORS, request validation."""

from __future__ import annotations

import stat
from pathlib import Path

import pytest

from omniscient.group import credentials as cred
from omniscient.group import server as srv
from omniscient.home import Home
from tests.group_support import TAURI, Daemon, daemon

__all__ = ["daemon"]  # the fixture


# ---- token and binding --------------------------------------------------------------


def test_token_file_is_private_and_reused(tmp_path: Path) -> None:
    home = Home(tmp_path)
    token = cred.load_or_create_hook_token(home)
    path = cred.hook_token_path(home)
    assert len(token) >= cred.TOKEN_MIN_LEN
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    path.chmod(0o644)
    assert cred.load_or_create_hook_token(home) == token
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert cred.read_hook_token(home) == token


def test_short_token_file_is_refused(tmp_path: Path) -> None:
    home = Home(tmp_path)
    cred.hook_token_path(home).parent.mkdir(parents=True)
    cred.hook_token_path(home).write_text("short")
    with pytest.raises(RuntimeError, match="jeton"):
        cred.load_or_create_hook_token(home)


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
    conn.putheader("Authorization", f"Bearer {daemon.hook_token}")
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
