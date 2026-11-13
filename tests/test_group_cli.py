"""`omniscient group …` commands (no real ~/.claude, ~/.copilot, herdr or AgentRun)."""

from __future__ import annotations

import json
import socket
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from omniscient import cli, cli_group
from omniscient.group import server as srv
from omniscient.group.models import Pending, Session, utcnow
from omniscient.home import Home

runner = CliRunner()


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def test_hook_without_a_home_is_silent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OMNISCIENT_HOME", str(tmp_path / "nowhere"))
    event = json.dumps({"hook_event_name": "Stop", "session_id": "s"})
    result = runner.invoke(
        cli.app, ["group", "hook", "--source", "claude"], input=event
    )
    assert result.exit_code == 0 and result.output == ""


def test_hook_with_a_bad_source_still_exits_zero(isolated_home: Path) -> None:
    result = runner.invoke(cli.app, ["group", "hook", "--source", "vim"], input="{}")
    assert result.exit_code == 0


def test_token_is_created_once_and_printed(isolated_home: Path) -> None:
    first = runner.invoke(cli.app, ["group", "token"])
    second = runner.invoke(cli.app, ["group", "token"])
    assert first.exit_code == 0 and first.output == second.output
    stored = (isolated_home / ".omniscient" / "group.token").read_text().strip()
    assert first.output.strip() == stored and len(stored) >= 32


def test_status_without_a_daemon_says_how_to_start_it(isolated_home: Path) -> None:
    result = runner.invoke(cli.app, ["group", "status", "--port", str(free_port())])
    assert result.exit_code == 1
    assert "omniscient group serve" in result.output


def test_status_counts_sessions(isolated_home: Path) -> None:
    server = srv.create_server(Home(isolated_home), port=0)
    server.start_background()
    try:
        now = utcnow()
        server.app.store.upsert_session(
            Session("claude:a", "claude", "a", "demo", None, "working", now, now)
        )
        server.app.store.open_pending(Pending.new("claude:a", "permission"))
        result = runner.invoke(cli.app, ["group", "status", "--port", str(server.port)])
    finally:
        server.stop()
        server.app.store.close()
    assert result.exit_code == 0, result.output
    assert "répond sur" in result.output
    assert "1 session(s) : 1 en attente" in result.output
    assert "1 demande(s)" in result.output


def test_summary_without_sessions() -> None:
    assert cli_group.summary([]) == ["Aucune session suivie pour le moment."]


def test_serve_without_collectors(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[Any, ...]] = []
    monkeypatch.setattr(srv, "serve", lambda *a: calls.append(a))
    result = runner.invoke(
        cli.app, ["group", "serve", "--no-collectors", "--port", "7399"]
    )
    assert result.exit_code == 0, result.output
    assert calls[0][1:] == (7399, ())
    runner.invoke(cli.app, ["group", "serve"])
    assert calls[1][1:] == (7341, None)  # default collectors


def test_hooks_install_previews_then_writes(
    isolated_home: Path, tmp_path: Path
) -> None:
    path = tmp_path / "settings.json"
    path.write_text('{"model": "x"}\n', encoding="utf-8")
    args = ["group", "hooks", "install", "--target", "claude", "--path", str(path)]
    preview = runner.invoke(cli.app, args)
    assert preview.exit_code == 0 and "--write" in preview.output
    assert json.loads(path.read_text()) == {"model": "x"}
    written = runner.invoke(cli.app, [*args, "--write", "--statusline"])
    assert written.exit_code == 0, written.output
    data = json.loads(path.read_text())
    command = data["hooks"]["PermissionRequest"][0]["hooks"][0]["command"]
    assert "omniscient-hook" in command or "omniscient.group.hook_client" in command
    assert data["statusLine"]["command"].endswith("--statusline")
    status = runner.invoke(
        cli.app, ["group", "hooks", "status", "--target", "claude", "--path", str(path)]
    )
    assert "statusLine omnigroup" in status.output
    gone = runner.invoke(
        cli.app,
        [
            "group",
            "hooks",
            "uninstall",
            "--target",
            "claude",
            "--path",
            str(path),
            "--write",
        ],
    )
    assert gone.exit_code == 0
    assert json.loads(path.read_text()) == {"model": "x", "hooks": {}}


def test_hooks_errors_are_french_and_exit_1(
    isolated_home: Path, tmp_path: Path
) -> None:
    path = tmp_path / "broken.json"
    path.write_text("{not json", encoding="utf-8")
    args = ["group", "hooks", "install", "--target", "claude", "--path", str(path)]
    result = runner.invoke(cli.app, args)
    assert result.exit_code == 1 and "JSON invalide" in result.output
    both = runner.invoke(cli.app, ["group", "hooks", "status", "--path", str(path)])
    assert both.exit_code == 1 and "une seule cible" in both.output


def test_widget_explains_download_and_pairing() -> None:
    result = runner.invoke(cli.app, ["group", "widget"])
    assert result.exit_code == 0
    assert "VinceVibeCoding/omniscient-factory/releases" in result.output
    assert "omniscient group token" in result.output
