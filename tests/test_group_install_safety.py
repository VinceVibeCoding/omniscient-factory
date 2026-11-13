"""Installer safety (M9, --protect) and the jump / « lance toi-même » helpers (LOW)."""

from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest

from omniscient.group.actions import Env, ps_quote, safe_url, windows_command
from omniscient.group.hook_install import (
    apply,
    is_ours,
    is_status_command,
    plan,
    run_hooks_command,
    status,
)
from omniscient.group.hook_protect import deny_rules

CMD = "/usr/bin/omniscient-hook"
STATE = Path("/home/someone/.omniscient")


@pytest.mark.parametrize(
    ("command", "ours"),
    [
        ("/usr/bin/omniscient-hook --source claude", True),
        ("'/opt/my tools/omniscient-hook' --source claude --fast", True),
        ("/py/bin/python3 -m omniscient.group.hook_client --source copilot", True),
        ("/x/omniscient group hook --source claude", True),
        ("echo omniscient-hook", False),
        ("guard.sh # omniscient group hook", False),
        ("python -c 'import omniscient.group.hook_client'", False),
        ("/x/omniscient-hook-evil --source claude", False),
        ("unbalanced 'quote", False),
    ],
)
def test_our_entries_are_recognised_by_exact_argv(command: str, ours: bool) -> None:
    assert is_ours({"command": command}) is ours


def test_statusline_recognition_is_exact() -> None:
    assert is_status_command(f"{CMD} --statusline")
    assert is_status_command("/x/omniscient group statusline")
    assert not is_status_command("echo omniscient-hook --statusline")


def test_symlinked_settings_are_written_through(tmp_path: Path) -> None:
    real = tmp_path / "dotfiles" / "settings.json"
    real.parent.mkdir()
    real.write_text('{"model": "x"}\n', encoding="utf-8")
    real.chmod(0o640)
    link = tmp_path / "settings.json"
    link.symlink_to(real)
    saved = apply(plan("claude", "install", link, CMD))
    assert link.is_symlink()
    assert "PermissionRequest" in json.loads(real.read_text())["hooks"]
    assert saved is not None and saved.parent == real.parent
    assert stat.S_IMODE(real.stat().st_mode) == 0o640
    assert not [p for p in real.parent.iterdir() if p.name.endswith(".tmp")]


def test_protect_adds_then_uninstall_removes_only_our_deny_rules(
    tmp_path: Path,
) -> None:
    path = tmp_path / "settings.json"
    mine = {"permissions": {"deny": ["Read(./.env)"], "allow": ["Bash(ls)"]}}
    path.write_text(json.dumps(mine, indent=2) + "\n", encoding="utf-8")
    named = deny_rules(STATE, Path("/home/someone"))
    assert "Read(~/.omniscient/**)" in named
    assert "Bash(cat /home/someone/.omniscient/*)" in named
    assert "Read(//elsewhere/**)" in deny_rules(Path("/elsewhere"), Path("/h"))
    rules = deny_rules(STATE)
    out: list[str] = []
    args = {"path": path, "command": CMD, "say": out.append, "state_dir": STATE}
    run_hooks_command("install", "claude", True, protect=True, **args)
    data = json.loads(path.read_text())
    assert data["permissions"]["deny"][0] == "Read(./.env)"
    assert set(rules) <= set(data["permissions"]["deny"])
    assert data["permissions"]["allow"] == ["Bash(ls)"]
    assert status("claude", path, deny_rules(STATE)).protected
    run_hooks_command("uninstall", "claude", True, **args)
    assert json.loads(path.read_text())["permissions"] == mine["permissions"]


def test_protect_on_a_file_without_permissions_leaves_no_trace(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    path.write_text('{"model": "x"}\n', encoding="utf-8")
    rules = deny_rules(STATE)
    apply(plan("claude", "install", path, CMD, protect=rules))
    assert json.loads(path.read_text())["permissions"]["deny"] == rules
    apply(plan("claude", "uninstall", path, CMD, protect=rules))
    assert json.loads(path.read_text()) == {"model": "x", "hooks": {}}


@pytest.mark.parametrize(
    ("url", "ok"),
    [
        ("https://console.example.com/runs/r1", True),
        ("http://127.0.0.1:7340/runs/r1", True),
        ("http://127.0.0.1:7340/", True),
        ("http://127.0.0.1:7341/sessions", False),  # the daemon itself
        ("http://localhost:7340/runs", False),
        ("http://127.0.0.1:7340", False),
        ("https://user:pw@example.com/", False),
        ("https://example.com:99999/", False),
    ],
)
def test_jump_urls(url: str, ok: bool) -> None:
    assert safe_url(url) is ok


def test_powershell_quoting() -> None:
    assert ps_quote("it's $(calc)") == "'it''s $(calc)'"
    env = Env(wsl=True, environ={"WSL_DISTRO_NAME": "Ubuntu-24.04"})
    command = windows_command(["claude", "-p", "--resume", "id", "say 'hi' $x"], env)
    assert command == (
        "& 'wsl.exe' '-d' 'Ubuntu-24.04' '-e' 'claude' '-p' '--resume' 'id' "
        "'say ''hi'' $x'"
    )
    bad = Env(wsl=True, environ={"WSL_DISTRO_NAME": "-bad;name"})
    assert "'-d'" not in windows_command(["claude"], bad)
