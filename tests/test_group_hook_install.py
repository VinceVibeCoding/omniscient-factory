"""omnigroup hook installer: merge, preview, backup, atomic write, uninstall, status."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from omniscient.group.hook_install import (
    HookInstallError,
    apply,
    base_command,
    claude_hooks,
    copilot_hooks,
    is_ours,
    is_status_command,
    plan,
    resolve_targets,
    run_hooks_command,
    status,
)
from omniscient.home import DEFAULT_HOME, Home

CMD = "/opt/tool/bin/omniscient group hook"
NOW = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
USER_SETTINGS = """{
    "model": "opus",
    "hooks": {
        "Stop": [{"hooks": [{"type": "command", "command": "notify-send fini"}]}],
        "PreToolUse": [
            {"matcher": "Bash", "hooks": [{"type": "command", "command": "guard.sh"}]}
        ]
    },
    "env": {"A": "1"}
}
"""


def settings(tmp_path: Path, text: str = USER_SETTINGS) -> Path:
    path = tmp_path / "settings.json"
    path.write_text(text, encoding="utf-8")
    return path


def test_claude_hook_layout() -> None:
    hooks = claude_hooks(CMD)
    assert hooks["PermissionRequest"] == [
        {
            "matcher": "*",
            "hooks": [
                {"type": "command", "command": f"{CMD} --source claude", "timeout": 110}
            ],
        }
    ]
    ask, star = hooks["PreToolUse"]
    assert ask["matcher"] == "^AskUserQuestion$" and ask["hooks"][0]["timeout"] == 110
    assert ask["hooks"][0]["command"] == f"{CMD} --source claude"
    assert star["matcher"] == "*" and star["hooks"][0]["timeout"] == 5
    assert star["hooks"][0]["command"].endswith("--fast")
    assert "matcher" not in hooks["Stop"][0]
    assert set(hooks) == {
        "SessionStart", "SessionEnd", "UserPromptSubmit", "PreToolUse",
        "PermissionRequest", "PostToolUse", "PostToolUseFailure", "Notification",
        "Stop", "SubagentStop",
    }  # fmt: skip


def test_copilot_hook_layout() -> None:
    hooks = copilot_hooks(CMD)
    assert hooks["permissionRequest"] == [
        {
            "type": "command",
            "bash": f"{CMD} --source copilot --event permissionRequest",
            "timeoutSec": 30,
        }
    ]
    assert hooks["preToolUse"][0]["bash"].endswith("--event preToolUse --fast")


def test_install_keeps_user_hooks_and_other_keys(tmp_path: Path) -> None:
    path = settings(tmp_path)
    p = plan("claude", "install", path, CMD)
    assert p.changed and p.after is not None
    after = json.loads(p.after)
    before = json.loads(USER_SETTINGS)
    assert after["model"] == "opus" and after["env"] == {"A": "1"}
    assert after["hooks"]["Stop"][0] == before["hooks"]["Stop"][0]
    assert after["hooks"]["PreToolUse"][0] == before["hooks"]["PreToolUse"][0]
    assert len(after["hooks"]["PreToolUse"]) == 3
    # outside the "hooks" value, the text is unchanged byte for byte
    assert p.after.startswith('{\n    "model": "opus",\n    "hooks": {\n        "Stop"')
    assert p.after.endswith('    },\n    "env": {"A": "1"}\n}\n')
    assert "+" in p.diff() and str(path) in p.diff()


def test_preview_writes_nothing(tmp_path: Path) -> None:
    path = settings(tmp_path)
    out: list[str] = []
    run_hooks_command("install", "claude", False, path, CMD, out.append)
    assert path.read_text(encoding="utf-8") == USER_SETTINGS
    assert "--write" in out[0]
    assert sorted(f.name for f in tmp_path.iterdir()) == ["settings.json"]


def test_write_backs_up_then_reinstall_is_a_no_op(tmp_path: Path) -> None:
    path = settings(tmp_path)
    path.chmod(0o600)
    saved = apply(plan("claude", "install", path, CMD), NOW)
    assert saved is not None and saved.read_text(encoding="utf-8") == USER_SETTINGS
    assert path.stat().st_mode & 0o777 == 0o600
    assert status("claude", path).missing == ()
    again = plan("claude", "install", path, CMD)
    assert not again.changed
    assert apply(again, NOW) is None


def test_backups_are_never_overwritten(tmp_path: Path) -> None:
    path = settings(tmp_path)
    first = apply(plan("claude", "install", path, CMD), NOW)
    second = apply(plan("claude", "uninstall", path, CMD), NOW)
    assert first != second and first is not None and second is not None
    assert first.exists() and second.exists()


def test_reinstall_with_a_new_command_replaces_ours(tmp_path: Path) -> None:
    path = settings(tmp_path)
    apply(plan("claude", "install", path, CMD), NOW)
    apply(plan("claude", "install", path, "/new/omniscient group hook"), NOW)
    text = path.read_text(encoding="utf-8")
    assert "/opt/tool" not in text and text.count("/new/omniscient") == 11


def test_uninstall_removes_only_ours(tmp_path: Path) -> None:
    path = settings(tmp_path)
    apply(plan("claude", "install", path, CMD), NOW)
    apply(plan("claude", "uninstall", path, CMD), NOW)
    assert json.loads(path.read_text(encoding="utf-8")) == json.loads(USER_SETTINGS)
    assert status("claude", path).installed == ()


def test_uninstall_splits_a_shared_group(tmp_path: Path) -> None:
    shared = {"hooks": {"Stop": [{"hooks": [
        {"type": "command", "command": "keep.sh"},
        {"type": "command", "command": f"{CMD} --source claude --fast"},
    ]}]}}  # fmt: skip
    path = settings(tmp_path, json.dumps(shared))
    p = plan("claude", "uninstall", path, CMD)
    assert p.after is not None
    assert json.loads(p.after)["hooks"]["Stop"] == [
        {"hooks": [{"type": "command", "command": "keep.sh"}]}
    ]


def test_settings_without_hooks_or_file(tmp_path: Path) -> None:
    path = settings(tmp_path, '{"model": "opus"}\n')
    p = plan("claude", "install", path, CMD)
    assert p.after is not None and json.loads(p.after)["model"] == "opus"
    missing = tmp_path / "new" / "settings.json"
    p = plan("claude", "install", missing, CMD)
    assert p.before is None and apply(p, NOW) is None and missing.is_file()
    assert not plan("claude", "uninstall", tmp_path / "nope.json", CMD).changed


def test_copilot_file_is_created_and_deleted(tmp_path: Path) -> None:
    path = tmp_path / "hooks" / "omnigroup.json"
    apply(plan("copilot", "install", path, CMD), NOW)
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["version"] == 1 and "permissionRequest" in data["hooks"]
    assert status("copilot", path).missing == ()
    saved = apply(plan("copilot", "uninstall", path, CMD), NOW)
    assert not path.exists() and saved is not None and saved.exists()


def test_copilot_file_with_user_entries_is_kept(tmp_path: Path) -> None:
    user = {"version": 1, "hooks": {"sessionStart": [{"type": "command", "bash": "x"}]}}
    path = settings(tmp_path, json.dumps(user))
    apply(plan("copilot", "install", path, CMD), NOW)
    apply(plan("copilot", "uninstall", path, CMD), NOW)
    assert json.loads(path.read_text(encoding="utf-8")) == user


@pytest.mark.parametrize("text", ["{not json", "[]", '{"hooks": []}'])
def test_invalid_settings_are_refused(tmp_path: Path, text: str) -> None:
    with pytest.raises(HookInstallError):
        plan("claude", "install", settings(tmp_path, text), CMD)


def test_status_lists_missing_events(tmp_path: Path) -> None:
    out: list[str] = []
    run_hooks_command("status", "claude", path=settings(tmp_path), say=out.append)
    assert "0/10" in out[0] and "PermissionRequest" in out[0]


def test_command_messages(tmp_path: Path) -> None:
    path = settings(tmp_path)
    out: list[str] = []
    run_hooks_command("install", "claude", True, path, CMD, out.append)
    run_hooks_command("install", "claude", True, path, CMD, out.append)
    assert "mis à jour" in out[0] and "sauvegarde" in out[0]
    assert "déjà à jour" in out[1]
    with pytest.raises(HookInstallError):
        run_hooks_command("explode", "claude", path=path, say=out.append)


def test_targets_and_path() -> None:
    assert resolve_targets("all", None) == ("claude", "copilot")
    with pytest.raises(HookInstallError):
        resolve_targets("all", Path("x.json"))
    with pytest.raises(HookInstallError):
        resolve_targets("vim", None)


def test_base_command_uses_the_light_hook_script(tmp_path: Path) -> None:
    def which(_name: str) -> str:
        return "/usr/bin/omniscient-hook"

    assert base_command(Home(DEFAULT_HOME), which) == "/usr/bin/omniscient-hook"
    custom = base_command(Home(tmp_path / "my home"), which)
    assert custom.startswith("/usr/bin/omniscient-hook --home '")
    fallback = base_command(None, lambda _n: None, "/py/bin/python")
    assert fallback == "/py/bin/python -m omniscient.group.hook_client"
    with pytest.raises(HookInstallError):
        base_command(None, lambda _n: None, "")


def test_script_commands_are_ours_and_statusline_recognised(tmp_path: Path) -> None:
    assert is_ours({"command": "/usr/bin/omniscient-hook --source claude --fast"})
    assert is_status_command("/usr/bin/omniscient-hook --statusline")
    assert is_status_command("/x/omniscient group statusline")
    assert not is_status_command("/usr/bin/other --statusline")
    path = tmp_path / "settings.json"
    out: list[str] = []
    cmd = "/usr/bin/omniscient-hook"
    run_hooks_command("install", "claude", True, path, cmd, out.append, statusline=True)
    data = json.loads(path.read_text())
    assert data["statusLine"]["command"] == "/usr/bin/omniscient-hook --statusline"
    assert status("claude", path).statusline


def test_is_ours() -> None:
    assert is_ours({"command": f"{CMD} --source claude"})
    assert is_ours({"bash": "python -m omniscient.group.hook_client --source copilot"})
    assert not is_ours({"command": "guard.sh"}) and not is_ours("x")


# ---------------------------------------------------------------- statusLine (opt-in)

LINE = "/opt/tool/bin/omniscient group statusline"


def test_statusline_is_opt_in(tmp_path: Path) -> None:
    path = settings(tmp_path)
    plain = plan("claude", "install", path, CMD)
    assert plain.after is not None and "statusLine" not in json.loads(plain.after)
    p = plan("claude", "install", path, CMD, statusline=LINE)
    assert p.after is not None
    assert json.loads(p.after)["statusLine"] == {
        "type": "command",
        "command": LINE,
        "padding": 0,
    }
    apply(p, NOW)
    assert status("claude", path).statusline


def test_foreign_statusline_needs_force(tmp_path: Path) -> None:
    mine = {"model": "opus", "statusLine": {"type": "command", "command": "ps1.sh"}}
    path = settings(tmp_path, json.dumps(mine, indent=2) + "\n")
    with pytest.raises(HookInstallError, match="--force"):
        plan("claude", "install", path, CMD, statusline=LINE)
    kept = plan("claude", "install", path, CMD)  # without --statusline: untouched
    assert kept.after is not None
    assert json.loads(kept.after)["statusLine"] == mine["statusLine"]
    forced = plan("claude", "install", path, CMD, statusline=LINE, force=True)
    assert forced.after is not None
    assert json.loads(forced.after)["statusLine"]["command"] == LINE


@pytest.mark.parametrize("position", ["first", "middle", "last"])
def test_uninstall_removes_our_statusline_only(tmp_path: Path, position: str) -> None:
    line = {"type": "command", "command": LINE}
    order = {
        "first": {"statusLine": line, "a": 1, "b": 2},
        "middle": {"a": 1, "statusLine": line, "b": 2},
        "last": {"a": 1, "b": 2, "statusLine": line},
    }[position]
    path = settings(tmp_path, json.dumps(order, indent=4) + "\n")
    p = plan("claude", "uninstall", path, CMD)
    assert p.after is not None
    assert json.loads(p.after) == {"a": 1, "b": 2, "hooks": {}}
    assert '    "a": 1,' in p.after  # the untouched members keep their formatting


def test_command_with_statusline(tmp_path: Path) -> None:
    path = settings(tmp_path)
    out: list[str] = []
    run_hooks_command("install", "claude", True, path, CMD, out.append, statusline=True)
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["statusLine"]["command"] == f"{CMD} --statusline"
    run_hooks_command("status", "claude", path=path, say=out.append)
    assert out[-1].endswith("statusLine omnigroup")
