"""Widget pairing: code shown on Windows only, five tries, lock, hashed tokens (H1)."""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from pathlib import Path

import pytest
from typer.testing import CliRunner

from omniscient import cli
from omniscient.group.credentials import db_path, token_hash
from omniscient.group.pairing import (
    POWERSHELL_SCRIPT,
    PairError,
    PairingDesk,
    code_commands,
    show_code_on_windows,
)
from omniscient.home import Home
from tests.group_support import Daemon, daemon, start_daemon

__all__ = ["daemon"]  # the fixture
runner = CliRunner()


def test_pairing_mints_a_token_stored_only_as_a_hash(daemon: Daemon) -> None:
    status, body, _ = daemon.call("POST", "/pair/start", {}, auth=False)
    assert status == 200 and "bureau Windows" in body["message"]
    assert "code" not in body  # never in the HTTP reply
    [code] = daemon.shown_codes
    status, body, _ = daemon.call(
        "POST", "/pair/finish", {"code": code, "client_name": "PC"}, auth=False
    )
    assert status == 200
    token = body["token"]
    assert daemon.call("GET", "/sessions", auth=False, headers={"Authorization": f"Bearer {token}"})[0] == 200  # fmt: skip
    raw = Path(daemon.server.app.home.state / "group.db").read_bytes()
    assert token.encode() not in raw and token_hash(token).encode() in raw
    again = daemon.call("POST", "/pair/finish", {"code": code}, auth=False)
    assert again[0] == 409  # single use


def test_windows_unavailable_refuses_pairing_with_the_terminal_hint(
    tmp_path: Path,
) -> None:
    d = start_daemon(tmp_path, windows_ok=False)
    try:
        status, body, _ = d.call("POST", "/pair/start", {}, auth=False)
        assert status == 503 and "--show-code" in body["error"]
        code = d.shown_codes[0]
        finish = d.call("POST", "/pair/finish", {"code": code}, auth=False)
        assert finish[0] == 409  # the refused pairing was cancelled
    finally:
        d.server.close_all()


def test_five_wrong_codes_lock_pairing(daemon: Daemon) -> None:
    daemon.call("POST", "/pair/start", {}, auth=False)
    code = daemon.shown_codes[0]
    wrong = "000000" if code != "000000" else "111111"
    for left in (4, 3, 2, 1):
        status, body, _ = daemon.call("POST", "/pair/finish", {"code": wrong}, auth=False)  # fmt: skip
        assert status == 403 and f"{left} essai" in body["error"]
    assert daemon.call("POST", "/pair/finish", {"code": wrong}, auth=False)[0] == 429
    assert daemon.call("POST", "/pair/finish", {"code": code}, auth=False)[0] == 429
    assert daemon.call("POST", "/pair/start", {}, auth=False)[0] == 429


@pytest.mark.parametrize("body", [{"code": "12345"}, {"code": 123456}, {}])
def test_finish_validates_the_code(daemon: Daemon, body: dict) -> None:
    assert daemon.call("POST", "/pair/finish", body, auth=False)[0] == 400


def test_one_pairing_at_a_time_and_starts_are_rate_limited() -> None:
    now = [0.0]
    desk = PairingDesk(clock=lambda: now[0])
    desk.start()
    with pytest.raises(PairError) as busy:
        desk.start()
    assert busy.value.status == 409
    for _ in range(4):
        desk.cancel()
        desk.start()
    desk.cancel()
    with pytest.raises(PairError) as limited:
        desk.start()
    assert limited.value.status == 429
    now[0] += 600
    assert len(desk.start()) == 6


def test_expired_code_is_refused() -> None:
    now = [0.0]
    desk = PairingDesk(clock=lambda: now[0])
    code = desk.start()
    now[0] += 121
    with pytest.raises(PairError, match="Aucun couplage"):
        desk.finish(code)


def test_windows_commands_take_the_code_as_an_argument_only() -> None:
    seen: list[list[str]] = []

    def launcher(argv: Sequence[str]) -> bool:
        seen.append(list(argv))
        return argv[0] == "msg.exe"

    assert show_code_on_windows("042137", launcher)
    ps, msg = seen
    assert ps[:5] == ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", POWERSHELL_SCRIPT]  # fmt: skip
    assert ps[5] == "042137" and "042137" not in POWERSHELL_SCRIPT
    assert '"' not in POWERSHELL_SCRIPT  # single quotes only through WSL interop
    assert msg[:3] == ["msg.exe", "*", "/TIME:120"] and "042137" in msg[3]
    assert not show_code_on_windows("042137", lambda argv: False)
    with pytest.raises(ValueError):
        code_commands("12; calc")


def test_terminal_pairing_is_off_by_default(daemon: Daemon) -> None:
    status, body, _ = daemon.call("POST", "/pair/start-terminal", {})
    assert status == 403 and "--allow-terminal-pairing" in body["error"]


def test_terminal_pairing_needs_the_hook_credential(tmp_path: Path) -> None:
    d = start_daemon(tmp_path, allow_terminal_pairing=True)
    try:
        assert d.call("POST", "/pair/start-terminal", {}, auth="widget")[0] == 403
        status, body, _ = d.call("POST", "/pair/start-terminal", {})
        assert status == 200 and len(body["code"]) == 6
        assert d.shown_codes == []  # not shown on Windows
    finally:
        d.server.close_all()


def test_cli_pair_show_code_and_revoke(isolated_home: Path) -> None:
    d = start_daemon(isolated_home, allow_terminal_pairing=True)
    try:
        port = str(d.server.port)
        shown = runner.invoke(cli.app, ["group", "pair", "--show-code", "--port", port])
        assert shown.exit_code == 0, shown.output
        assert "Code de couplage" in shown.output
        revoked = runner.invoke(cli.app, ["group", "pair", "--revoke"])
        assert revoked.exit_code == 0 and "1 widget(s)" in revoked.output
        assert d.call("GET", "/sessions")[0] == 401  # the widget token is gone
    finally:
        d.server.close_all()
    db = sqlite3.connect(db_path(Home(isolated_home)))
    assert db.execute("SELECT COUNT(*) FROM widget_tokens").fetchone() == (0,)
    db.close()


def test_cli_pair_without_flags_explains(isolated_home: Path) -> None:
    result = runner.invoke(cli.app, ["group", "pair"])
    assert result.exit_code == 0 and "Demander un code" in result.output
