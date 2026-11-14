"""Widget display config: config.json, settings.json overrides, and the CLI."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from omniscient import cli_group_config
from omniscient.group import config_store as cs

runner = CliRunner()


@pytest.fixture
def paths(tmp_path: Path) -> cs.ConfigPaths:
    return cs.ConfigPaths(
        tmp_path / "xdg/omnigroup/config.json", tmp_path / "claude/settings.json"
    )


def settings(paths: cs.ConfigPaths, env: dict) -> None:
    paths.settings.parent.mkdir(parents=True, exist_ok=True)
    paths.settings.write_text(json.dumps({"model": "opus", "env": env}))


def test_default_paths_follow_xdg_and_claude_config_dir(tmp_path: Path) -> None:
    p = cs.ConfigPaths.default(
        {
            "XDG_CONFIG_HOME": str(tmp_path / "x"),
            "CLAUDE_CONFIG_DIR": str(tmp_path / "c"),
        }
    )
    assert p.config == tmp_path / "x/omnigroup/config.json"
    assert p.settings == tmp_path / "c/settings.json"
    home = cs.ConfigPaths.default({})
    assert home.config == Path.home() / ".config/omnigroup/config.json"


def test_defaults_when_nothing_exists(paths: cs.ConfigPaths) -> None:
    view = cs.effective(paths)
    assert view == {
        "config": cs.DEFAULTS,
        "sources": {k: "default" for k in cs.DEFAULTS},
    }


def test_update_writes_privately_and_keeps_unknown_keys(paths: cs.ConfigPaths) -> None:
    paths.config.parent.mkdir(parents=True)
    paths.config.write_text(json.dumps({"future_key": 3, "theme": "neon"}))
    view = cs.update(paths, {"theme": "Terminal", "show_ticker": "false"})
    assert view["config"] == {
        "theme": "terminal",
        "osd_view": "bars",
        "show_ticker": False,
    }
    assert (
        view["sources"]["theme"] == "config"
        and view["sources"]["osd_view"] == "default"
    )
    stored = json.loads(paths.config.read_text())
    assert stored["future_key"] == 3
    assert oct(paths.config.stat().st_mode & 0o777) == "0o600"
    assert oct(paths.config.parent.stat().st_mode & 0o777) == "0o700"


@pytest.mark.parametrize(
    "patch",
    [
        {},
        {"theme": "neon"},
        {"osd_view": 3},
        {"show_ticker": "maybe"},
        {"colour": "red"},
    ],
)
def test_invalid_patches_are_400(paths: cs.ConfigPaths, patch: dict) -> None:
    with pytest.raises(cs.ConfigError) as err:
        cs.update(paths, patch)
    assert err.value.status == 400
    assert not paths.config.exists()


def test_settings_env_wins_and_locks(paths: cs.ConfigPaths) -> None:
    cs.update(paths, {"theme": "terminal", "osd_view": "gauge"})
    settings(
        paths,
        {
            "OMNIGROUP_THEME": "dashboard",
            "OMNIGROUP_SHOW_TICKER": "0",
            "OMNIGROUP_OSD_VIEW": "bogus",
        },
    )
    view = cs.effective(paths)
    assert view["config"] == {
        "theme": "dashboard",
        "osd_view": "gauge",
        "show_ticker": False,
    }
    assert view["sources"] == {
        "theme": "settings",
        "osd_view": "config",
        "show_ticker": "settings",
    }
    with pytest.raises(cs.ConfigError) as err:
        cs.update(paths, {"theme": "default"})
    assert err.value.status == 409
    cs.update(paths, {"osd_view": "bars"})  # unlocked keys still change


def test_corrupt_files_fall_back_to_defaults(paths: cs.ConfigPaths) -> None:
    paths.config.parent.mkdir(parents=True)
    paths.config.write_text("{nope")
    paths.settings.parent.mkdir(parents=True)
    paths.settings.write_text("[1, 2]")
    assert cs.effective(paths)["config"] == cs.DEFAULTS


def test_fingerprint_changes_with_files(paths: cs.ConfigPaths) -> None:
    before = cs.fingerprint(paths)
    assert before == (0.0, 0.0)
    cs.update(paths, {"theme": "terminal"})
    assert cs.fingerprint(paths) != before


# ---- CLI ------------------------------------------------------------------------------


@pytest.fixture
def cli_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    return tmp_path


def test_cli_set_then_get(cli_env: Path) -> None:
    app = cli_group_config.config_app
    ok = runner.invoke(app, ["set", "theme", "dashboard"])
    assert ok.exit_code == 0 and "theme = dashboard" in ok.output
    got = runner.invoke(app, ["get"])
    assert (
        "theme = dashboard" in got.output and "osd_view = bars (défaut)" in got.output
    )
    one = runner.invoke(app, ["get", "show_ticker", "--json"])
    assert json.loads(one.output) is True


def test_cli_errors_exit_1(cli_env: Path) -> None:
    app = cli_group_config.config_app
    bad = runner.invoke(app, ["set", "theme", "neon"])
    assert bad.exit_code == 1 and "valeur invalide" in bad.output
    unknown = runner.invoke(app, ["get", "colour"])
    assert unknown.exit_code == 1


def test_register_adds_config_under_group() -> None:
    import typer

    group = typer.Typer()
    cli_group_config.register(group)
    assert any(g.name == "config" for g in group.registered_groups)
